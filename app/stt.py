"""STT: WAV-Bytes -> Text via Whisper-OpenAI-API (whisper.cpp Server auf .31)."""
import logging

from . import config, http

log = logging.getLogger("stt")

# Mindestlänge: bei 16 kHz / 16 bit mono = 1 s = 32 KB.
# Alles darunter ist stumm/leer und sollte nicht an den Upstream gesendet werden.
_MIN_WAV_BYTES = 16_000  # ~0,5 s


def transcribe(wav_data: bytes) -> str:
    if len(wav_data) < _MIN_WAV_BYTES:
        log.warning("STT: WAV zu kurz (%d Bytes), ignoriere", len(wav_data))
        return ""
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
