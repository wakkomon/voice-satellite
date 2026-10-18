"""Master-Konfiguration (2.0.0alpha).

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

# ── Raspi-Manager ─────────────────────────────────────────────
# Auto-Discovery-IP-Range (wenn SATELLITES_FILE nicht gesetzt)
SATELLITE_NETWORK  = _env("SATELLITE_NETWORK", "192.168.1.0/24")
# Heartbeat-Intervall (s)
SATELLITE_HEARTBEAT_SEC = _env_int("SATELLITE_HEARTBEAT_SEC", "60")
# Maximale verpasste Heartbeats bevor ein Satellite als tot gilt
SATELLITE_MAX_MISSED = _env_int("SATELLITE_MAX_MISSED", "5")
