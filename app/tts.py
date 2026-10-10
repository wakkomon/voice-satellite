"""TTS: Text -> Audio über die Piper-API (.33:5000), direkt nach aplay gestreamt."""
import logging

from . import config, http

log = logging.getLogger("tts")


def synthesize(text, retries: int = 2) -> bytes:
    """Piper/OpenAI-kompatibler /v1/audio/speech; sammelt Streaming-WAV.

    Bei leerer oder fehlgeschlagener Ausgabe bis zu `retries`-mal neu
    versuchen (Default 2 -> 3 Gesamtversuche): Piper fällt gelegentlich
    durch (leeres Stream, Reset) und liefert sonst Stille."""
    payload = {
        "model": config.TTS_MODEL,
        "input": text,
        "voice": config.TTS_VOICE,
        "response_format": "wav",
        "stream": True,
    }
    if config.TTS_LANGUAGE:
        payload["language"] = config.TTS_LANGUAGE
    for attempt in range(1, retries + 2):
        chunks = []
        try:
            for chunk in http.post_stream(config.TTS_URL, payload,
                                          token=config.TTS_TOKEN,
                                          timeout=config.TTS_TIMEOUT):
                chunks.append(chunk)
            wav = b"".join(chunks)
            if len(wav) > 44:   # >44: mehr als nur WAV-Header
                if attempt > 1:
                    log.info("TTS-Versuch %d erfolgreich", attempt)
                return wav
            log.warning("TTS lieferte leere Ausgabe (Versuch %d/%d)",
                        attempt, retries + 1)
        except Exception as e:
            log.warning("TTS-Versuch %d/%d fehlgeschlagen: %s",
                        attempt, retries + 1, e)
    log.error("TTS nach %d Versuchen ohne Audio", retries + 1)
    return b""
