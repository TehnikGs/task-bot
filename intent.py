"""Понимание смысла команды: расшифровка речи -> намерение + текст задачи.

Если задан LLM_API_KEY — используем бесплатную модель (Groq Llama).
Если ключа нет — работает запасной разбор по ключевым словам.
"""
import json

import httpx

import config

INTENTS = {
    "create_task",
    "list_in_progress",
    "list_done",
    "list_new",
    "list_all",
    "unknown",
}

SYSTEM_PROMPT = (
    "Ты — ассистент, который превращает реплики начальника в команды трекера задач. "
    "На вход приходит расшифровка голосового сообщения на русском языке. "
    "Определи намерение и верни СТРОГО JSON без пояснений вида:\n"
    '{"intent": "...", "task_text": "..."}\n\n'
    "Возможные intent:\n"
    "- create_task — начальник ставит/поручает новую задачу. В task_text помести суть задачи "
    "чистым текстом (без слов «поставь задачу», «надо», «нужно» и т.п.), с заглавной буквы.\n"
    "- list_in_progress — просит показать задачи, которые сейчас в работе.\n"
    "- list_done — просит показать выполненные/завершённые задачи.\n"
    "- list_new — просит показать задачи, которые ещё не приняты в работу.\n"
    "- list_all — просит показать все задачи / общий статус.\n"
    "- unknown — непонятно.\n"
    "Для всех list_* и unknown поле task_text — пустая строка."
)


def _heuristic(text: str) -> dict:
    t = text.lower()
    if any(k in t for k in ("в работе", "в работу", "что делаешь", "чем занят")):
        return {"intent": "list_in_progress", "task_text": ""}
    if any(k in t for k in ("заверш", "выполнен", "готов", "сделал", "сделан")):
        return {"intent": "list_done", "task_text": ""}
    if any(k in t for k in ("не принят", "не принял", "новые задач", "не взял", "ожида")):
        return {"intent": "list_new", "task_text": ""}
    if any(k in t for k in ("все задачи", "весь список", "общий статус", "список задач", "покажи задачи")):
        return {"intent": "list_all", "task_text": ""}

    # иначе считаем это новой задачей и вычищаем слова-триггеры
    task = text
    for trig in (
        "поставь задачу", "поставить задачу", "новая задача", "поручаю",
        "задача", "надо", "нужно", "сделай",
    ):
        idx = task.lower().find(trig)
        if idx != -1:
            task = task[idx + len(trig):].strip(" :,-—")
            break
    task = task.strip()
    if task:
        return {"intent": "create_task", "task_text": task[:1].upper() + task[1:]}
    return {"intent": "unknown", "task_text": ""}


async def parse_intent(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {"intent": "unknown", "task_text": ""}
    if not config.LLM_API_KEY:
        return _heuristic(text)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{config.LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
                json={
                    "model": config.LLM_MODEL,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            data = json.loads(content)
            intent = data.get("intent", "unknown")
            if intent not in INTENTS:
                intent = "unknown"
            return {"intent": intent, "task_text": (data.get("task_text") or "").strip()}
    except Exception:
        # любая ошибка сети/модели -> не падаем, разбираем по правилам
        return _heuristic(text)
