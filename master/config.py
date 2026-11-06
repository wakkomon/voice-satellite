"""Master-Konfiguration

Nur die Variablen, die der Master wirklich braucht.
Keine Voice/LLM/Display-Configs.
"""
from __future__ import annotations
import os


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: str) -> int:
    return int(os.environ.get(key, default))


# ── Server ─────────────────────────────────────────────────────
API_HOST        = _env("API_HOST", "0.0.0.0")
API_PORT        = _env_int("API_PORT", "8791")
LOG_LEVEL       = _env("LOG_LEVEL", "INFO")

# ── Fileserver-Index ──────────────────────────────────────────
MUSIC_ROOT      = _env("MUSIC_ROOT", "/m")  # Container-mountpunkt (wie /m auf Satelliten)
CACHE_DIR       = _env("CACHE_DIR", "/data/vs-index")  # Index-Cache (persistiertes JSON)

# ── Chat-WebUI ────────────────────────────────────────────────
# Chat-Eintraege (Satelliten-Turne + Web-Chat) + TTS-Aufnahmen
CHAT_DIR        = _env("CHAT_DIR", os.path.join(CACHE_DIR, "chat"))
# LLM/TTS fuer die Web-Chat-Frage-Antwort (eigene Werte, Satellite unangetastet)
CHAT_MODEL      = _env("CHAT_MODEL", "")          # leer -> LLM_MODEL-Default unten
CHAT_TTS_MODEL  = _env("CHAT_TTS_MODEL", "")      # leer -> TTS_MODEL-Default unten
CHAT_TTS_VOICE  = _env("CHAT_TTS_VOICE", "")      # leer -> TTS_VOICE-Default unten

# Gedächtnis des Web-Chat-LLMs (analog Satellite app/memory.py):
# Kurzzeit = letzte N Chat-Eintraege pro Chat-Key (mac/"web") aus dem JSONL,
# Langzeit = extrahierte Fakten in CHAT_MEMORY_DB (sqlite) im Systemprompt.
CHAT_MEMORY_ENABLED  = _env("CHAT_MEMORY_ENABLED", "1") in ("1", "true", "yes", "on")
CHAT_MEMORY_FACTS_ENABLED = _env("CHAT_MEMORY_FACTS", "1") in ("1", "true", "yes", "on")
CHAT_MEMORY_DB       = _env("CHAT_MEMORY_DB", os.path.join(CHAT_DIR, "memory.db"))
CHAT_MAX_HISTORY     = _env_int("CHAT_MAX_HISTORY", "20")   # Nachrichten im Prompt
CHAT_MAX_FACTS       = _env_int("CHAT_MAX_FACTS", "100")    # Fakten im Systemprompt

# ── Raspi-Manager ─────────────────────────────────────────────
# (Auto-Discovery entfernt – Satelliten melden sich aktiv per Heartbeat an)
# Heartbeat-Intervall (s)
SATELLITE_HEARTBEAT_SEC = _env_int("SATELLITE_HEARTBEAT_SEC", "60")
# Maximale verpasste Heartbeats bevor ein Satellite als tot gilt
SATELLITE_MAX_MISSED = _env_int("SATELLITE_MAX_MISSED", "5")

# ── Backend-URLs (nur Master-intern, nie an Satelliten senden) ──
# Fallback auf die allgemeinen URLs (LLM_URL/TTS_URL/STT_URL), damit ein
# veralteter Default nicht stumm in einen Connect-Timeout laeuft.
TTS_BACKEND_URL     = _env("TTS_BACKEND_URL", "") or _env("TTS_URL", "http://192.168.1.31:7811/v1/audio/speech")
STT_BACKEND_URL     = _env("STT_BACKEND_URL", "") or _env("STT_URL", "http://192.168.1.31:9000/v1/audio/transcriptions")
LLM_BACKEND_URL     = _env("LLM_BACKEND_URL", "") or _env("LLM_URL", "http://192.168.1.33:8642/v1/chat/completions")
