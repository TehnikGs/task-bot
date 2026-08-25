"""Телеграм-бот «Задачник».

Начальник голосом (или текстом) ставит задачи и спрашивает статус.
Сотрудник кнопками принимает / завершает задачи.
"""
import asyncio
import base64
import html
import logging
import os
import tempfile
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from aiohttp import ClientError, web

import config
import db
import intent as intent_mod
import stt

logging.basicConfig(level=logging.DEBUG if config.DEBUG else logging.INFO)
log = logging.getLogger("task-bot")

STATUS_LABEL = {
    db.STATUS_NEW: "🆕 Не принята",
    db.STATUS_IN_PROGRESS: "🔧 В работе",
    db.STATUS_DONE: "✅ Завершена",
    db.STATUS_REJECTED: "🚫 Отклонена",
}

class _Socks5RdnsSession(AiohttpSession):
    """SOCKS5-сессия с удалённым разрешением имён (rdns=True).

    Нужна для серверов в РФ: api.telegram.org там заблокирован/отравлен в DNS,
    поэтому имя должно разрешаться на стороне прокси, а не локально на сервере.
    """

    def __init__(self, host, port, username, password, **kwargs):
        super().__init__(**kwargs)
        from aiohttp_socks import ProxyConnector, ProxyType

        self._connector_type = ProxyConnector
        self._connector_init = dict(
            proxy_type=ProxyType.SOCKS5,
            host=host,
            port=int(port),
            username=username,
            password=password,
            rdns=True,
        )


class RelaySession(AiohttpSession):
    """Все запросы к Telegram идут через Google Apps Script relay (бесплатная замена прокси).

    Сервер в РФ шлёт POST на RELAY_URL: {secret, method, params}; Google дергает
    метод Telegram и возвращает ответ. Так исходящие запросы не идут напрямую в Telegram.
    """

    async def make_request(self, bot, method, timeout=None):
        session = await self.create_session()
        files: dict = {}
        params: dict = {}
        for key, value in method.model_dump(warnings=False).items():
            prepared = self.prepare_value(value, bot=bot, files=files)
            if not prepared:
                continue
            params[key] = prepared
        payload = {
            "secret": config.RELAY_SECRET,
            "method": method.__api_method__,
            "params": params,
        }
        try:
            async with session.post(
                config.RELAY_URL,
                json=payload,
                timeout=self.timeout if timeout is None else timeout,
            ) as resp:
                raw = await resp.text()
                status = resp.status
        except asyncio.TimeoutError as e:
            raise TelegramNetworkError(method=method, message="Relay timeout") from e
        except ClientError as e:
            raise TelegramNetworkError(method=method, message=f"{type(e).__name__}: {e}") from e
        response = self.check_response(bot=bot, method=method, status_code=status, content=raw)
        return response.result


def _build_session():
    """Сессия для Telegram. Приоритет: релей → SOCKS5-прокси → прямое соединение."""
    if config.RELAY_URL:
        return RelaySession()
    proxy = config.TELEGRAM_PROXY
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if parsed.scheme.lower().startswith("socks5"):
        return _Socks5RdnsSession(
            parsed.hostname, parsed.port, parsed.username, parsed.password
        )
    # http/https-прокси — стандартный путь aiogram
    return AiohttpSession(proxy=proxy)


async def relay_download(file_path: str) -> bytes:
    """Скачать файл (голосовое) через релей — сервер в РФ не достаёт file-сервер Telegram."""
    import httpx

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            config.RELAY_URL,
            json={"secret": config.RELAY_SECRET, "method": "downloadFile", "file_path": file_path},
        )
        r.raise_for_status()
        return base64.b64decode(r.json()["base64"])


bot = Bot(
    config.BOT_TOKEN,
    session=_build_session(),
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())


class Comment(StatesGroup):
    waiting = State()


# ---------- вспомогательные ----------
def esc(s) -> str:
    """Экранировать текст для HTML-разметки Telegram."""
    return html.escape(str(s), quote=False)


def card_text(task: dict) -> str:
    return (
        f"📋 <b>Задача #{task['id']}</b>\n"
        f"<blockquote>{esc(task['text'])}</blockquote>\n"
        f"Статус: {STATUS_LABEL.get(task['status'], task['status'])}"
    )


def card_kb(task: dict):
    s = task["status"]
    rows = []
    if s == db.STATUS_NEW:
        rows.append([
            InlineKeyboardButton(text="✅ Принять", callback_data=f"accept:{task['id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{task['id']}"),
        ])
    elif s == db.STATUS_IN_PROGRESS:
        rows.append([InlineKeyboardButton(text="🏁 Завершить", callback_data=f"done:{task['id']}")])
    elif s == db.STATUS_DONE:
        rows.append([InlineKeyboardButton(text="↩️ Вернуть в работу", callback_data=f"reopen:{task['id']}")])
    if s != db.STATUS_REJECTED:
        rows.append([InlineKeyboardButton(
            text="💬 Комментарий/вопрос", callback_data=f"comment:{task['id']}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def is_boss(uid: int) -> bool:
    # пока BOSS_ID не задан (режим настройки) — пускаем всех
    return config.BOSS_ID == 0 or uid == config.BOSS_ID


def is_employee(uid: int) -> bool:
    return config.EMPLOYEE_ID == 0 or uid in (config.EMPLOYEE_ID, config.BOSS_ID)


def _targets(message: Message):
    """Куда отправлять ответ. Если пишут в личке — дублируем и в группу."""
    origin = message.chat.id
    chats = [origin]
    if config.GROUP_ID and config.GROUP_ID != origin:
        chats.append(config.GROUP_ID)
    return chats


# ---------- служебные команды ----------
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Я бот-задачник.\n\n"
        "• Начальник голосом ставит задачи и спрашивает статус.\n"
        "• Сотрудник кнопками принимает и завершает задачи.\n\n"
        f"Твой Telegram ID: <code>{message.from_user.id}</code>\n"
        f"ID этого чата: <code>{message.chat.id}</code>"
    )


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(
        f"Твой ID: <code>{message.from_user.id}</code>\n"
        f"ID чата: <code>{message.chat.id}</code>"
    )


@dp.message(Command("tasks"))
async def cmd_tasks(message: Message):
    await send_to([message.chat.id], format_all_tasks())


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    # Сброс всех задач. Только из личного чата (защита от случайного сброса в группе).
    if message.chat.type != "private":
        await message.answer("Команду /reset можно выполнить только в личке с ботом.")
        return
    deleted = await delete_all_cards()
    n = db.clear_tasks()
    await message.answer(f"🧹 Сброшено задач: {n}. Карточек удалено из чатов: {deleted}.")


# ---------- комментарий/вопрос по задаче ----------
@dp.message(Comment.waiting, F.text)
async def on_comment_text(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    task_id = data.get("task_id")
    group_chat = data.get("group_chat") or message.chat.id
    name = message.from_user.full_name
    comment = message.text
    # убрать «сырое» сообщение пользователя (бот — админ); не критично, если не выйдет
    try:
        await message.delete()
    except Exception:
        pass
    await bot.send_message(
        group_chat,
        f"💬 <b>Комментарий к задаче #{task_id}</b> от {esc(name)}:\n"
        f"<blockquote>{esc(comment)}</blockquote>",
    )


# ---------- голос и текст от начальника ----------
@dp.message(F.voice)
async def on_voice(message: Message):
    if not is_boss(message.from_user.id):
        return
    note = await message.answer("🎧 Слушаю…")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        if config.RELAY_URL:
            # через релей: сперва узнаём file_path, потом качаем байты через Google
            f_info = await bot.get_file(message.voice.file_id)
            with open(tmp_path, "wb") as out:
                out.write(await relay_download(f_info.file_path))
        else:
            await bot.download(message.voice, destination=tmp_path)
        text = await stt.transcribe(tmp_path)
    except Exception:
        log.exception("STT failed")
        await note.edit_text("⚠️ Не удалось распознать голос. Проверь настройки распознавания (ключ/баланс).")
        return
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if not text:
        await note.edit_text("🤷 Ничего не расслышал, повтори, пожалуйста.")
        return
    await note.edit_text(f"📝 «{esc(text)}»")
    await handle_command(message, text)


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    if not is_boss(message.from_user.id):
        return
    if message.chat.type != "private":
        # В группе пропускаем совсем короткие реплики («да», «ок», «понял»),
        # остальное отдаём ИИ — он сам отличит поручение от обсуждения.
        if len(message.text.split()) < 2:
            return
    await handle_command(message, message.text)


async def handle_command(message: Message, text: str):
    result = await intent_mod.parse_intent(text)
    intent = result["intent"]

    if intent == "create_task":
        task_text = result["task_text"] or text
        # Карточка с кнопками — в группу (там её принимает сотрудник),
        # иначе в тот же чат, где прозвучала команда.
        target_chat = config.GROUP_ID or message.chat.id
        task_id = db.add_task(task_text, target_chat)
        task = db.get_task(task_id)
        sent = await bot.send_message(target_chat, card_text(task), reply_markup=card_kb(task))
        db.set_card(task_id, sent.message_id)
        # если команда из лички — продублировать подтверждение начальнику в личку
        if target_chat != message.chat.id:
            await message.answer(
                f"✅ Задача #{task_id} создана и отправлена в группу:\n"
                f"<blockquote>{esc(task_text)}</blockquote>"
            )
    elif intent == "list_in_progress":
        await send_to(_targets(message), _fmt_group("🔧 В работе", db.list_by_status(db.STATUS_IN_PROGRESS)))
    elif intent == "list_done":
        await send_to(_targets(message), _fmt_group("✅ Завершённые", db.list_by_status(db.STATUS_DONE)))
    elif intent == "list_new":
        await send_to(_targets(message), _fmt_group("🆕 Не принятые", db.list_by_status(db.STATUS_NEW)))
    elif intent == "list_all":
        await send_to(_targets(message), format_all_tasks())
    else:
        # «Не понял» отвечаем только в личке — в группе молчим, чтобы не спамить.
        if message.chat.type == "private":
            await message.answer(
                "🤔 Не понял команду. Например, можно так:\n"
                "• «Поставь задачу убрать цех к вечеру»\n"
                "• «Что сейчас в работе?»\n"
                "• «Какие задачи завершены?»\n"
                "• «Что ещё не принято?»"
            )


def _fmt_group(title: str, tasks) -> str:
    """Заголовок жирным + раскрывающийся список задач (можно свернуть/развернуть)."""
    if tasks:
        body = "\n".join(f"#{t['id']} — {esc(t['text'])}" for t in tasks)
        return f"<b>{title} — {len(tasks)}</b>\n<blockquote expandable>{body}</blockquote>"
    return f"<b>{title} — 0</b>\n<i>пусто</i>"


def format_all_tasks() -> str:
    """Одно сообщение: В работе / Завершённые / Не принятые — каждая секция раскрывается."""
    return (
        "📋 <b>Задачи</b>\n\n"
        + _fmt_group("🔧 В работе", db.list_by_status(db.STATUS_IN_PROGRESS))
        + "\n\n"
        + _fmt_group("✅ Завершённые", db.list_by_status(db.STATUS_DONE))
        + "\n\n"
        + _fmt_group("🆕 Не принятые", db.list_by_status(db.STATUS_NEW))
    )


async def send_to(chat_ids, text: str):
    for cid in chat_ids:
        try:
            await bot.send_message(cid, text)
        except Exception:
            log.exception("send_to -> %s failed", cid)


async def delete_all_cards() -> int:
    """Удалить из чатов карточки всех задач (по возможности). Возвращает число удалённых."""
    count = 0
    for t in db.all_tasks():
        if t.get("chat_id") and t.get("message_id"):
            try:
                await bot.delete_message(t["chat_id"], t["message_id"])
                count += 1
            except Exception:
                pass
    return count


# ---------- кнопки сотрудника ----------
@dp.callback_query(F.data.startswith(("accept:", "reject:", "done:", "reopen:")))
async def on_action(cb: CallbackQuery):
    action, _, sid = cb.data.partition(":")
    task_id = int(sid)
    if not is_employee(cb.from_user.id):
        await cb.answer("Менять статус может только исполнитель.", show_alert=True)
        return
    task = db.get_task(task_id)
    if not task:
        await cb.answer("Задача не найдена.", show_alert=True)
        return

    new_status = {
        "accept": db.STATUS_IN_PROGRESS,
        "reject": db.STATUS_REJECTED,
        "done": db.STATUS_DONE,
        "reopen": db.STATUS_IN_PROGRESS,
    }[action]
    db.set_status(task_id, new_status)
    task = db.get_task(task_id)
    await cb.message.edit_text(card_text(task), reply_markup=card_kb(task))
    # оповещение в группе о действии исполнителя
    who = esc(cb.from_user.full_name)
    notif = {
        "accept": f"✅ <b>{who}</b> принял(а) задачу #{task_id} в работу",
        "done": f"🏁 <b>{who}</b> завершил(а) задачу #{task_id}",
        "reject": f"🚫 <b>{who}</b> отклонил(а) задачу #{task_id}",
        "reopen": f"↩️ <b>{who}</b> вернул(а) задачу #{task_id} в работу",
    }[action]
    try:
        await bot.send_message(task.get("chat_id") or cb.message.chat.id, notif)
    except Exception:
        log.exception("notif send failed")
    await cb.answer({
        "accept": "Принято в работу ✅",
        "reject": "Отклонено 🚫",
        "done": "Завершено 🏁",
        "reopen": "Возвращено в работу ↩️",
    }[action])


@dp.callback_query(F.data.startswith("comment:"))
async def on_comment_btn(cb: CallbackQuery, state: FSMContext):
    task_id = int(cb.data.split(":", 1)[1])
    task = db.get_task(task_id)
    if not task:
        await cb.answer("Задача не найдена.", show_alert=True)
        return
    await state.set_state(Comment.waiting)
    await state.update_data(
        task_id=task_id, group_chat=task.get("chat_id") or cb.message.chat.id
    )
    await cb.message.answer(
        f"💬 Напишите комментарий или вопрос по задаче #{task_id} следующим сообщением."
    )
    await cb.answer()


# ---------- HTTP API для программы начальника ----------
def _api_authorized(request) -> bool:
    return bool(config.API_KEY) and request.headers.get("X-API-Key") == config.API_KEY


def _task_json(t: dict) -> dict:
    return {
        "id": t["id"],
        "text": t["text"],
        "status": t["status"],
        "status_label": STATUS_LABEL.get(t["status"], t["status"]),
        "created_at": t["created_at"],
        "accepted_at": t["accepted_at"],
        "done_at": t["done_at"],
    }


@web.middleware
async def _auth_mw(request, handler):
    # /webhook и /api/health не требуют X-API-Key (у /webhook своя проверка секрета)
    if request.path in ("/api/health", "/webhook"):
        return await handler(request)
    if not _api_authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def api_health(request):
    return web.json_response({"ok": True})


async def api_list(request):
    status = request.query.get("status")
    tasks = db.list_by_status(status) if status else db.list_active()
    return web.json_response({"tasks": [_task_json(t) for t in tasks]})


async def api_get(request):
    t = db.get_task(int(request.match_info["id"]))
    if not t:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(_task_json(t))


async def api_create(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    text = (data.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "field 'text' required"}, status=400)
    target = config.GROUP_ID or 0
    task_id = db.add_task(text, target)
    task = db.get_task(task_id)
    if target:
        try:
            sent = await bot.send_message(target, card_text(task), reply_markup=card_kb(task))
            db.set_card(task_id, sent.message_id)
            task = db.get_task(task_id)
        except Exception:
            log.exception("api_create: не удалось отправить карточку в группу")
    return web.json_response(_task_json(task), status=201)


async def api_set_status(request):
    t = db.get_task(int(request.match_info["id"]))
    if not t:
        return web.json_response({"error": "not found"}, status=404)
    try:
        data = await request.json()
    except Exception:
        data = {}
    status = (data.get("status") or "").strip()
    valid = (db.STATUS_NEW, db.STATUS_IN_PROGRESS, db.STATUS_DONE, db.STATUS_REJECTED)
    if status not in valid:
        return web.json_response({"error": f"status must be one of {valid}"}, status=400)
    db.set_status(t["id"], status)
    t = db.get_task(t["id"])
    if t.get("chat_id") and t.get("message_id"):
        try:
            await bot.edit_message_text(
                card_text(t), chat_id=t["chat_id"], message_id=t["message_id"],
                reply_markup=card_kb(t),
            )
        except Exception:
            pass
    return web.json_response(_task_json(t))


async def api_reset(request):
    deleted = await delete_all_cards()
    n = db.clear_tasks()
    return web.json_response({"reset": True, "removed_tasks": n, "deleted_cards": deleted})


# ---------- приём апдейтов Telegram (вебхук через релей) ----------
_seen_updates: set = set()


async def webhook_handler(request):
    """Апдейты Telegram, пересланные Google-релеем на /webhook."""
    if config.WEBHOOK_SECRET and request.headers.get("X-Relay-Secret") != config.WEBHOOK_SECRET:
        return web.Response(status=403)
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400)
    uid = data.get("update_id")
    if uid is not None:
        if uid in _seen_updates:
            return web.Response()  # дубликат (Telegram мог повторить доставку)
        _seen_updates.add(uid)
        if len(_seen_updates) > 2000:
            _seen_updates.clear()
    try:
        update = Update.model_validate(data, context={"bot": bot})
    except Exception:
        log.exception("Некорректный апдейт")
        return web.Response()
    asyncio.create_task(dp.feed_update(bot, update))
    return web.Response()


async def start_web():
    """Веб-сервер: HTTP API (если задан ключ) и/или приём вебхука (если задан релей)."""
    app = web.Application(middlewares=[_auth_mw])
    if config.API_KEY:
        app.add_routes([
            web.get("/api/health", api_health),
            web.get("/api/tasks", api_list),
            web.get("/api/tasks/{id}", api_get),
            web.post("/api/tasks", api_create),
            web.post("/api/tasks/{id}/status", api_set_status),
            web.post("/api/reset", api_reset),
        ])
    if config.RELAY_URL:
        app.add_routes([web.post("/webhook", webhook_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.API_HOST, config.API_PORT)
    await site.start()
    log.info(
        "Веб-сервер %s:%s (API=%s, webhook=%s)",
        config.API_HOST, config.API_PORT, bool(config.API_KEY), bool(config.RELAY_URL),
    )
    return runner


async def relay_poll_loop():
    """Короткий опрос Telegram через релей (timeout=0) с паузой между запросами.

    Длинный long-poll через Google Apps Script нестабилен (Relay timeout),
    а короткий возвращается мгновенно и работает надёжно.
    """
    log.info("Опрос через релей: короткий, пауза ~%sс", config.POLL_INTERVAL)
    offset = None
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=0)
            for u in updates:
                offset = u.update_id + 1
                asyncio.create_task(dp.feed_update(bot, u))
        except Exception as e:
            log.warning("Ошибка опроса: %s: %s", type(e).__name__, str(e)[:120])
        await asyncio.sleep(config.POLL_INTERVAL)


async def main():
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Заполни .env (см. .env.example).")
    db.init_db()
    await start_web()
    log.info(
        "Бот запущен. Режим=%s, STT=%s, понимание=%s",
        "релей+опрос" if config.RELAY_URL else "опрос",
        config.STT_BACKEND,
        config.LLM_MODEL if config.LLM_API_KEY else "по правилам (без ключа)",
    )
    if config.RELAY_URL:
        # Чистим backlog и опрашиваем коротким опросом через релей.
        await bot.delete_webhook(drop_pending_updates=True)
        await relay_poll_loop()
    else:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
