"""Телеграм-бот «Задачник».

Начальник голосом (или текстом) ставит задачи и спрашивает статус.
Сотрудник кнопками принимает / завершает задачи.
"""
import asyncio
import logging
import os
import tempfile
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import web

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


def _build_session():
    """Сессия для Telegram. Для SOCKS5 включаем удалённый DNS (обход блокировки в РФ)."""
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


bot = Bot(
    config.BOT_TOKEN,
    session=_build_session(),
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


# ---------- вспомогательные ----------
def card_text(task: dict) -> str:
    return (
        f"📋 <b>Задача #{task['id']}</b>\n"
        f"«{task['text']}»\n\n"
        f"Статус: {STATUS_LABEL.get(task['status'], task['status'])}"
    )


def card_kb(task: dict):
    s = task["status"]
    if s == db.STATUS_NEW:
        rows = [[
            InlineKeyboardButton(text="✅ Принять", callback_data=f"accept:{task['id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{task['id']}"),
        ]]
    elif s == db.STATUS_IN_PROGRESS:
        rows = [[InlineKeyboardButton(text="🏁 Завершить", callback_data=f"done:{task['id']}")]]
    elif s == db.STATUS_DONE:
        rows = [[InlineKeyboardButton(text="↩️ Вернуть в работу", callback_data=f"reopen:{task['id']}")]]
    else:
        rows = []
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
    await send_list([message.chat.id], db.list_active(), "📋 Все задачи")


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    # Сброс всех задач. Только из личного чата (защита от случайного сброса в группе).
    if message.chat.type != "private":
        await message.answer("Команду /reset можно выполнить только в личке с ботом.")
        return
    deleted = await delete_all_cards()
    n = db.clear_tasks()
    await message.answer(f"🧹 Сброшено задач: {n}. Карточек удалено из чатов: {deleted}.")


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
    await note.edit_text(f"📝 «{text}»")
    await handle_command(message, text)


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    # текст работает так же, как голос — удобно для тестов без микрофона
    if not is_boss(message.from_user.id):
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
                f"✅ Задача #{task_id} создана и отправлена в группу:\n«{task_text}»"
            )
    elif intent == "list_in_progress":
        await send_list(_targets(message), db.list_by_status(db.STATUS_IN_PROGRESS), "🔧 В работе")
    elif intent == "list_done":
        await send_list(_targets(message), db.list_by_status(db.STATUS_DONE), "✅ Завершённые")
    elif intent == "list_new":
        await send_list(_targets(message), db.list_by_status(db.STATUS_NEW), "🆕 Не приняты")
    elif intent == "list_all":
        await send_list(_targets(message), db.list_active(), "📋 Все задачи")
    else:
        await message.answer(
            "🤔 Не понял команду. Например, можно так:\n"
            "• «Поставь задачу убрать цех к вечеру»\n"
            "• «Что сейчас в работе?»\n"
            "• «Какие задачи завершены?»\n"
            "• «Что ещё не принято?»"
        )


async def send_list(chat_ids, tasks, title: str):
    if tasks:
        lines = [f"<b>{title}</b>\n"]
        for t in tasks:
            lines.append(f"#{t['id']} {STATUS_LABEL.get(t['status'], '')} — {t['text']}")
        text = "\n".join(lines)
    else:
        text = f"<b>{title}</b>\n\nПусто."
    for cid in chat_ids:
        try:
            await bot.send_message(cid, text)
        except Exception:
            log.exception("send_list -> %s failed", cid)


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
    await cb.answer({
        "accept": "Принято в работу ✅",
        "reject": "Отклонено 🚫",
        "done": "Завершено 🏁",
        "reopen": "Возвращено в работу ↩️",
    }[action])


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
    if request.path == "/api/health":
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


async def start_api():
    app = web.Application(middlewares=[_auth_mw])
    app.add_routes([
        web.get("/api/health", api_health),
        web.get("/api/tasks", api_list),
        web.get("/api/tasks/{id}", api_get),
        web.post("/api/tasks", api_create),
        web.post("/api/tasks/{id}/status", api_set_status),
        web.post("/api/reset", api_reset),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.API_HOST, config.API_PORT)
    await site.start()
    log.info("HTTP API слушает %s:%s", config.API_HOST, config.API_PORT)
    return runner


async def main():
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Заполни .env (см. .env.example).")
    db.init_db()
    if config.API_KEY:
        await start_api()
    else:
        log.info("HTTP API выключен (API_KEY не задан)")
    log.info(
        "Бот запущен. STT=%s, понимание=%s",
        config.STT_BACKEND,
        config.LLM_MODEL if config.LLM_API_KEY else "по правилам (без ключа)",
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
