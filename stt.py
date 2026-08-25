"""Распознавание речи (Speech-To-Text).

Два режима (config.STT_BACKEND):
  groq  — бесплатный облачный Whisper (ничего тяжёлого на сервере). По умолчанию.
  local — faster-whisper прямо на сервере (совсем без оплаты, но нужнее ресурсы).
"""
import asyncio
import json

import httpx

import config

_local_model = None


async def transcribe_via_relay(file_path: str) -> str:
    """Распознать голос через релей: Google сам качает файл из Telegram и шлёт в Groq.

    Нужно, потому что сервер в РФ не достаёт ни file-сервер Telegram, ни Groq (гео-блок).
    """
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(
            config.RELAY_URL,
            follow_redirects=True,
            json={"secret": config.RELAY_SECRET, "method": "groqVoice",
                  "key": config.STT_API_KEY, "model": config.STT_MODEL, "file_path": file_path},
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"relay groqVoice status {data.get('status')}")
        return (json.loads(data["body"]).get("text") or "").strip()


async def transcribe(audio_path: str) -> str:
    if config.STT_BACKEND == "local":
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _transcribe_local, audio_path)
    return await _transcribe_groq(audio_path)


async def _transcribe_groq(audio_path: str) -> str:
    with open(audio_path, "rb") as f:
        files = {"file": (audio_path, f, "audio/ogg")}
        data = {"model": config.STT_MODEL, "language": "ru"}
        async with httpx.AsyncClient(timeout=60, proxy=config.LLM_PROXY or None) as client:
            resp = await client.post(
                f"{config.STT_BASE_URL}/audio/transcriptions",
                headers={"Authorization": f"Bearer {config.STT_API_KEY}"},
                files=files,
                data=data,
            )
            resp.raise_for_status()
            return (resp.json().get("text") or "").strip()


def _transcribe_local(audio_path: str) -> str:
    global _local_model
    from faster_whisper import WhisperModel  # импорт по требованию

    if _local_model is None:
        _local_model = WhisperModel(
            config.STT_LOCAL_MODEL, device="cpu", compute_type="int8"
        )
    segments, _ = _local_model.transcribe(audio_path, language="ru")
    return " ".join(seg.text for seg in segments).strip()
