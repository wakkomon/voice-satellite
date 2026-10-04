"""TTS: Text -> Audio über die Piper-API (.33:5000), direkt nach aplay gestreamt."""
import logging

from . import config, http

log = logging.getLogger("tts")


def synthesize(text) -> bytes:
    """Piper/OpenAI-kompatibler /v1/audio/speech; sammelt Streaming-WAV."""
    payload = {
        "model": config.TTS_MODEL,
        "input": text,
        "voice": config.TTS_VOICE,
        "response_format": "wav",
        "stream": True,
    }
    chunks = []
    try:
        for chunk in http.post_stream(config.TTS_URL, payload, token=config.TTS_TOKEN,
                                      timeout=config.TTS_TIMEOUT):
            chunks.append(chunk)
    except Exception as e:
        log.error("TTS fehlgeschlagen: %s", e)
        return b""
    return b"".join(chunks)
