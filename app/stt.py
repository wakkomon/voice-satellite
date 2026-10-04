"""STT: WAV-Bytes -> Text via Whisper-OpenAI-API (whisper.cpp Server auf .31)."""
import logging

from . import config, http

log = logging.getLogger("stt")


def transcribe(wav_data: bytes) -> str:
    try:
        data = http.post_multipart(
            config.STT_URL,
            filename="audio.wav",
            content_type="audio/wav",
            body=wav_data,
            fields={"model": config.STT_MODEL, "language": config.STT_LANGUAGE},
            timeout=config.STT_TIMEOUT,
        )
        return (data.get("text") or "").strip()
    except Exception as e:
        log.error("STT fehlgeschlagen: %s", e)
        return ""
