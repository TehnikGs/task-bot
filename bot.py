"""Телеграм-бот «Задачник».

Начальник голосом (или текстом) ставит задачи и спрашивает статус.
Сотрудник кнопками принимает / завершает задачи.
"""
import asyncio
import logging
import os
import tempfile

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

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

bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
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
    await send_list(message, db.list_active(), "📋 Все задачи")


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
        # Куда класть карточку: если задана группа (GROUP_ID) — туда,
        # иначе в тот же чат, где прозвучала команда.
        target_chat = config.GROUP_ID or message.chat.id
        task_id = db.add_task(task_text, target_chat)
        task = db.get_task(task_id)
        sent = await bot.send_message(target_chat, card_text(task), reply_markup=card_kb(task))
        db.set_card(task_id, sent.message_id)
        if target_chat != message.chat.id:
            await message.answer(f"✅ Задача #{task_id} отправлена в группу.")
    elif intent == "list_in_progress":
        await send_list(message, db.list_by_status(db.STATUS_IN_PROGRESS), "🔧 В работе")
    elif intent == "list_done":
        await send_list(message, db.list_by_status(db.STATUS_DONE), "✅ Завершённые")
    elif intent == "list_new":
        await send_list(message, db.list_by_status(db.STATUS_NEW), "🆕 Не приняты")
    elif intent == "list_all":
        await send_list(message, db.list_active(), "📋 Все задачи")
    else:
        await message.answer(
            "🤔 Не понял команду. Например, можно так:\n"
            "• «Поставь задачу убрать цех к вечеру»\n"
            "• «Что сейчас в работе?»\n"
            "• «Какие задачи завершены?»\n"
            "• «Что ещё не принято?»"
        )


async def send_list(message: Message, tasks, title: str):
    if not tasks:
        await message.answer(f"<b>{title}</b>\n\nПусто.")
        return
    lines = [f"<b>{title}</b>\n"]
    for t in tasks:
        lines.append(f"#{t['id']} {STATUS_LABEL.get(t['status'], '')} — {t['text']}")
    await message.answer("\n".join(lines))


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


async def main():
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Заполни .env (см. .env.example).")
    db.init_db()
    log.info(
        "Бот запущен. STT=%s, понимание=%s",
        config.STT_BACKEND,
        config.LLM_MODEL if config.LLM_API_KEY else "по правилам (без ключа)",
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
