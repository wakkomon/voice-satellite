"""Zentrale Konfigurationsverwaltung (2.0.0alpha).

Speichert die zentrale Konfiguration (TTS/STT/LLM-URLs, Modelle, o.Ä.)
einmalig am Master – nicht mehr ENV pro Satellite. Die Satelliten ziehen
die gültigen Werte per Heartbeat (GET /api/config/apply) und wenden sie
ohne Neustart an (app/config.apply_overrides).

Speicherformat (JSON, atomar via tmp+rename):
    {
      "version": 12,
      "updated_at": 1734500000.0,
      "global":    { "TTS_URL": "http://192.168.1.24:8791/proxy/tts", ... },
      "satellites": { "<mac>": { "LLM_MODEL": "qwen2.5", ... } }
    }

Für einen Satelliten greifen die globalen Werte, die von den
satelliten-spezifischen Overrides überschrieben werden.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger("master.satconfig")

# Pfad über ENV konfigurierbar (Default: neben diesem Modul).
CONFIG_DIR = os.environ.get("VS_CONFIG_DIR", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
CONFIG_PATH = os.path.join(CONFIG_DIR, "satellites.json")

# Schlüssel, die in der zentralen UI editiert werden dürfen (weiche Keys).
# Müssen exakt app/config.py::_OVERWRITABLE abdecken (sonst ignoriert der
# Satellite sie). Secrets (Tokens) werden NICHT hier gespeichert – sie bleiben
# ENV am Master und werden vom Proxy injiziert (siehe master/proxy.py).
EDITABLE_KEYS = (
    "TTS_URL", "TTS_MODEL", "TTS_VOICE", "TTS_LANGUAGE",
    "STT_URL", "STT_MODEL", "STT_LANGUAGE",
    "LLM_URL", "LLM_MODEL", "LLM_HAS_TOOLS", "LLM_ENABLE_THINKING",
    "OPENHAB_URL", "MUSIC_ROOT", "BRIDGE_HOST", "BRIDGE_PORT",
)


class ConfigStore:
    """Thread-sichere Persistenz der zentralen Satelliten-Konfiguration."""

    def __init__(self, path: str = CONFIG_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._version = 0
        self._global: dict = {}
        self._sats: dict[str, dict] = {}
        self._load()

    # ── Persistenz ───────────────────────────────────────────

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            self._version = int(raw.get("version", 0))
            self._global = raw.get("global") or {}
            self._sats = raw.get("satellites") or {}
            log.info("SatConfig geladen: version=%d, %d globale, %d sat-Key(s)",
                     self._version, len(self._global), len(self._sats))
        except FileNotFoundError:
            log.info("Kein Config-Store unter %s – leere Konfiguration", self._path)
        except Exception:
            log.exception("Config-Store konnte nicht geladen werden (neu gestartet)")

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({
                "version": self._version,
                "updated_at": time.time(),
                "global": self._global,
                "satellites": self._sats,
            }, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    # ── Lese-Zugriffe ────────────────────────────────────────

    def version(self) -> int:
        with self._lock:
            return self._version

    def get_effective(self, mac: str | None = None) -> dict:
        """Gültige Konfiguration (global + satellite-Override), nach
        Standard-Format normalisiert (dict[str, str|int|bool])."""
        with self._lock:
            merged = dict(self._global)
            if mac:
                merged.update(self._sats.get(mac.lower(), {}))
            return merged

    def snapshot(self) -> dict:
        """Vollständiger Stand für die UI (inkl. Version)."""
        with self._lock:
            return {
                "version": self._version,
                "global": dict(self._global),
                "satellites": {k: dict(v) for k, v in self._sats.items()},
            }

    # ── Schreib-Zugriffe (bump + persist + Notify-Callback) ──

    def _set(self, mutator) -> None:
        """mutator(store) unter dem Lock; danach Version bumpen + speichern.

        Der Callback (on_change) wird AUSSERHALB des Locks aufgerufen,
        damit das SSE-Broadcast / Push nicht blockiert.
        """
        fired = False
        with self._lock:
            mutator()
            self._version += 1
            self._save()
            fired = True
        if fired and self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                log.debug("Config-Change-Callback fehlgeschlagen", exc_info=True)

    _on_change = None  # Callable[[], None] – gesetzt von api.py

    def set_change_callback(self, cb) -> None:
        self._on_change = cb

    def set_global(self, overrides: dict) -> int:
        """Globale Schlüssel setzen (nur EDITABLE_KEYS)."""
        def _m() -> None:
            for k, v in (overrides or {}).items():
                if k in EDITABLE_KEYS:
                    self._global[k] = v
        self._set(_m)
        return self._version

    def set_satellite(self, mac: str, overrides: dict) -> int:
        """Satelliten-spezifische Overrides setzen (nur EDITABLE_KEYS)."""
        mac = (mac or "").lower()
        if not mac:
            raise ValueError("mac fehlt")
        def _m() -> None:
            sat = self._sats.setdefault(mac, {})
            for k, v in (overrides or {}).items():
                if k in EDITABLE_KEYS:
                    sat[k] = v
        self._set(_m)
        return self._version

    def clear_satellite(self, mac: str) -> int:
        mac = (mac or "").lower()
        def _m() -> None:
            self._sats.pop(mac, None)
        self._set(_m)
        return self._version


# Prozessweites Singleton (von api/proxy/manager genutzt).
store = ConfigStore()
