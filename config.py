"""Конфигурация бота. Все значения читаются из .env (см. .env.example)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int = 0) -> int:
    val = (os.getenv(name) or "").strip()
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# --- Telegram ---
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
BOSS_ID = _int("BOSS_ID")          # Telegram ID начальника (кто ставит задачи)
EMPLOYEE_ID = _int("EMPLOYEE_ID")  # Telegram ID сотрудника (кто выполняет)
GROUP_ID = _int("GROUP_ID")        # ID группы (пока не используется для фильтра, на будущее)

# --- LLM (понимание смысла). По умолчанию Groq, бесплатный ключ:
#     https://console.groq.com/keys  ---
LLM_API_KEY = (os.getenv("LLM_API_KEY") or "").strip()
LLM_BASE_URL = (os.getenv("LLM_BASE_URL") or "https://api.groq.com/openai/v1").strip().rstrip("/")
LLM_MODEL = (os.getenv("LLM_MODEL") or "llama-3.3-70b-versatile").strip()

# --- STT (распознавание речи). backend: groq | local ---
STT_BACKEND = (os.getenv("STT_BACKEND") or "groq").strip().lower()
STT_API_KEY = (os.getenv("STT_API_KEY") or "").strip() or LLM_API_KEY
STT_BASE_URL = (os.getenv("STT_BASE_URL") or "https://api.groq.com/openai/v1").strip().rstrip("/")
STT_MODEL = (os.getenv("STT_MODEL") or "whisper-large-v3").strip()
STT_LOCAL_MODEL = (os.getenv("STT_LOCAL_MODEL") or "small").strip()  # для faster-whisper

# --- Прокси (для сервера в РФ, если Telegram/Groq заблокированы) ---
# Примеры: socks5://user:pass@1.2.3.4:1080  или  http://1.2.3.4:3128
TELEGRAM_PROXY = (os.getenv("TELEGRAM_PROXY") or "").strip()
# Прокси для запросов к Groq (распознавание + понимание). Если пусто — берём тот же, что для Telegram.
LLM_PROXY = (os.getenv("LLM_PROXY") or "").strip() or TELEGRAM_PROXY

# --- HTTP API (для внешней программы начальника) ---
# Если API_KEY пуст — API не запускается. Запросы требуют заголовок X-API-Key.
API_KEY = (os.getenv("API_KEY") or "").strip()
API_HOST = (os.getenv("API_HOST") or "0.0.0.0").strip()
API_PORT = _int("API_PORT", 8081)

# --- Прочее ---
DB_PATH = (os.getenv("DB_PATH") or "tasks.db").strip()
DEBUG = (os.getenv("DEBUG") or "").strip().lower() in ("1", "true", "yes")
