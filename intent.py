"""Понимание смысла команды: расшифровка речи -> намерение + текст задачи.

Если задан LLM_API_KEY — используем бесплатную модель (Groq Llama).
Вызов Groq идёт через релей (если задан RELAY_URL) — обход гео-блокировки РФ.
Если ключа нет / ИИ недоступен — работает запасной разбор по ключевым словам.
"""
import json
import re

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
    "Ты — ассистент трекера задач рабочей группы. На вход — ОДНО сообщение "
    "(расшифровка голоса или текст) на русском. Определи намерение и верни СТРОГО JSON "
    'без пояснений: {"intent": "...", "task_text": "..."}\n\n'
    "Возможные intent:\n"
    "- create_task — это ПОРУЧЕНИЕ выполнить работу (императив: «настроить…», «реализовать…», "
    "«сделать…», «добавить…», «проверить…», «смержить…», «исправить…», «подготовить…»). "
    "В task_text — суть задачи чистым текстом, с заглавной буквы, без служебных слов "
    "(«поставь задачу», «задача», «надо», «нужно»).\n"
    "- list_in_progress — просят показать задачи в работе.\n"
    "- list_done — просят показать завершённые задачи.\n"
    "- list_new — просят показать ещё не принятые задачи.\n"
    "- list_all — просят показать все задачи / общий статус.\n"
    "- unknown — обычная переписка: ответ, вопрос, обсуждение, реплика о себе.\n\n"
    "ВАЖНО: create_task — ТОЛЬКО если это поручение что-то СДЕЛАТЬ. Если человек просто "
    "комментирует, отвечает, спрашивает или говорит о себе — это unknown, НЕ задача.\n"
    "Примеры unknown: «да», «понял», «ок», «можно завтра», «а я ещё не сделал PR», "
    "«это не срочно», «у вас включён режим отпуск?», «я посмотрю почему не реагирует».\n"
    "Примеры create_task: «настроить экспорт в Excel», «смержить PR 142», "
    "«добавить наименования ТМС в WMS», «реализовать показ партий по GUID».\n\n"
    "Для всех list_* и unknown поле task_text — пустая строка."
)


def _heuristic(text: str) -> dict:
    """Запасной разбор без ИИ. Создаёт задачу только при явном слове-триггере."""
    t = text.lower()
    if any(k in t for k in ("в работе", "в работу", "что делаешь", "чем занят")):
        return {"intent": "list_in_progress", "task_text": ""}
    if any(k in t for k in ("заверш", "выполнен", "готов", "сделал", "сделан")):
        return {"intent": "list_done", "task_text": ""}
    if any(k in t for k in ("не принят", "не принял", "новые задач", "не взял", "ожида")):
        return {"intent": "list_new", "task_text": ""}
    if any(k in t for k in ("все задачи", "весь список", "общий статус", "список задач", "покажи задачи")):
        return {"intent": "list_all", "task_text": ""}

    task = text
    matched = False
    for trig in ("поставь задачу", "поставить задачу", "новая задача", "поручаю",
                 "задача", "надо ", "нужно ", "сделай "):
        idx = task.lower().find(trig)
        if idx != -1:
            task = task[idx + len(trig):].strip(" :,-—")
            matched = True
            break
    task = task.strip()
    if matched and task:
        return {"intent": "create_task", "task_text": task[:1].upper() + task[1:]}
    return {"intent": "unknown", "task_text": ""}


async def _chat(payload: dict) -> str:
    """Вызов Groq chat: через релей (обход гео-блокировки) или напрямую."""
    if config.RELAY_URL:
        async with httpx.AsyncClient(timeout=40) as client:
            r = await client.post(
                config.RELAY_URL,
                follow_redirects=True,
                json={"secret": config.RELAY_SECRET, "method": "groqChat",
                      "key": config.LLM_API_KEY, "payload": payload},
            )
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError(f"relay groqChat status {data.get('status')}")
            body = json.loads(data["body"])
            return body["choices"][0]["message"]["content"]
    async with httpx.AsyncClient(timeout=30, proxy=config.LLM_PROXY or None) as client:
        resp = await client.post(
            f"{config.LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


# Явная команда: сообщение НАЧИНАЕТСЯ с этих слов → это всегда задача (без ИИ).
_EXPLICIT_TASK = re.compile(
    r"^\s*(?:поставь(?:те)?\s+задачу|новая\s+задача|задача|задание|поручаю)\b\s*[:\-—–.,]?\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)


async def parse_intent(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {"intent": "unknown", "task_text": ""}
    m = _EXPLICIT_TASK.match(text)
    if m and m.group(1).strip():
        body = m.group(1).strip()
        return {"intent": "create_task", "task_text": body[:1].upper() + body[1:]}
    if not config.LLM_API_KEY:
        return _heuristic(text)
    payload = {
        "model": config.LLM_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }
    try:
        content = await _chat(payload)
        data = json.loads(content)
        intent = data.get("intent", "unknown")
        if intent not in INTENTS:
            intent = "unknown"
        return {"intent": intent, "task_text": (data.get("task_text") or "").strip()}
    except Exception:
        return _heuristic(text)
