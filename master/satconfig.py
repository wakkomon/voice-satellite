"""Zentrale Konfigurationsverwaltung

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
# Satellite sie). Tokens sind MASTER-SEITIG (Proxy injiziert sie) und werden
# nur GLOBAL gespeichert, nie pro-Satellite – im Snapshot maskiert.
# Backend-URLs (TTS_URL, STT_URL, LLM_URL, OPENHAB_URL) sind hier nicht mehr
# editierbar: Satelliten sprechen ausschliesslich den Master-Proxy an.
# Die echten Ziele (*_BACKEND_URL) sind Master-seitig editierbar (UI-Domain
# "Backends"): Store-Wert gewinnt vor ENV-Fallback, werden NIEMALS an
# Satelliten ausgeliefert (get_effective filtert sie) und duerfen per
# Loop-Guard keinen /proxy/-Pfad nennen. Tokens bleiben als SECRET_KEYS
# verwaltbar, der Proxy injiziert sie.
EDITABLE_KEYS = (
    "TTS_MODEL", "TTS_VOICE", "TTS_LANGUAGE",
    "TTS_TIMEOUT",
    "STT_MODEL", "STT_LANGUAGE",
    "STT_TIMEOUT",
    "LLM_MODEL", "LLM_HAS_TOOLS", "LLM_ENABLE_THINKING",
    "LLM_TIMEOUT", "LLM_MAX_TOKENS",
    "OPENHAB_CACHE_TTL",
    "DEFAULT_IMAGE_URL",
    "MUSIC_ROOT", "BRIDGE_HOST", "BRIDGE_PORT",
    "MEMORY_DB", "MEMORY_MAX_TOKENS",
    "MIC_DEVICE", "SPEAKER_DEVICE", "DISPLAY", "FB_DEVICE", "FB_WIDTH", "FB_HEIGHT",
    "ALSA_DEVICE", "ALSA_VOLUME", "ALSA_MUTE",
    "WOL_MAC", "SCHEDULE_FILE", "RULES_FILE",
    "WAKE_WORD", "WAKE_SENS", "VAD_MODE", "VAD_THRESHOLD", "SILENCE_SEC",
    "LOG_LEVEL", "MASTER_POLL_SEC", "LLM_SYSTEM_PROMPT",
    "INTENTS",
)

# Secrets: nur global, nie pro Satellite; Proxy injiziert sie in Requests.
SECRET_KEYS = frozenset({
    "TTS_TOKEN", "STT_TOKEN", "LLM_TOKEN", "OPENHAB_TOKEN",
    # MASTER_TOKEN: Zugang der Satelliten zum Master (Heartbeat, Config-Pull).
    # Auf der Satellitenseite dieselbe Value als ENV MASTER_TOKEN (neben
    # MASTER_URL); hier im Store editierbar (UI-Bereich Secrets).
    "MASTER_TOKEN",
})

# Nur global verwaltbar (Satelliten erben sie, können sie nicht überschreiben).
GLOBAL_ONLY_KEYS = ("MASTER_MUSIC_AUTH",)

UI_KEYS = EDITABLE_KEYS + GLOBAL_ONLY_KEYS

# ── URL-Modell (Konzept-Punkt 1/2) ──────────────────────────────────────
# Der Satellit kennt als API-Ziel ausschliesslich den Master-Proxy; die
# echten Backend-Ziele (LiteLLM/Piper/STT/openHAB) kennt nur der Master.
URL_KEYS = ["LLM_URL", "STT_URL", "TTS_URL", "OPENHAB_URL"]
URL_MODEL = {
    "satellite_keys": URL_KEYS,
    "satellite_value": "<MASTER_URL>/proxy/<llm|stt|tts|openhab>",
    "master_keys": [k.replace("_URL", "_BACKEND_URL") for k in URL_KEYS],
    "master_source": "Master-UI (Store *_URL in der jeweiligen API-Domain) bzw. ENV-Fallback (*_BACKEND_URL)",
    "satellite_sends_token": False,
    "token_home": "nur Master (SECRET_KEYS, Proxy injiziert Authorization)",
}
# Master-seitige Backend-Ziele: die echten Endpunkte, nie ein Proxy-Pfad.
BACKEND_KEYS = [k.replace("_URL", "_BACKEND_URL") for k in URL_KEYS]


# Getrennte Exporte (WebUI: „Export Master" vs. „Export Satelliten"):
#  * Export Master   -> komplette Konfiguration (alle globalen Werte inkl.
#                       Backend-Ziele, System-/Gedaechtnis-Keys, Secrets plus
#                       alle Satelliten-Overrides). Genau die Datei, die der
#                       Import-Button braucht, um einen neuen Master voll-
#                       staendig wiederherzustellen (== export_payload).
#  * Export Satellit -> nur das Noetigste, damit ein neues Geraet (Pi/App)
#                       den Master erreicht: MASTER_URL (Adresse) und
#                       MASTER_TOKEN (Zugang). Modell-/Sprach-/Intent-Konfi-
#                       guration bleibt raus — die zieht das Geraet per
#                       Heartbeat selbst vom Master.


def backend_loop_reject(key: str, value) -> str | None:
    """Zyklus-Schutz: eine URL, die auf den Master-Proxy zurueckzeigt, ware
    eine Endlosschleife (Proxy -> Master -> Proxy ...).

    Regel A: ein Master-Ziel (*_URL) bzw. Backend-Alias (*_BACKEND_URL)
             muss das echte Backend nennen, nie einen /proxy/-Pfad.
    Regel B: keine URL (auch kein Satelliten-Override) darf auf
             localhost/127.0.0.1 mit dem Master-eigenen Port zeigen — der
             Satellit erreicht das localhost des Masters gar nicht, und der
             Master rueber sich sich selbst.

    Rueckgabe: Ablehnungsgrund oder None.
    """
    if not key.endswith("_URL") or not isinstance(value, str) or not value.strip():
        return None
    low = value.strip().lower()
    host = low.split("://", 1)[-1].partition("/")[0].rsplit("@")[-1]
    host_only, _, port = host.partition(":")
    if (key in URL_KEYS or key.endswith("_BACKEND_URL")) and (
            "/proxy/" in low or low.endswith("/proxy")):
        return ("%s muss das echte Backend nennen, nicht den Master-Proxy (%s)"
                % (key, value))
    own = str(os.environ.get("MASTER_PORT", "8791"))
    if host_only in ("localhost", "127.0.0.1", "0.0.0.0", "::1") and port == own:
        return ("%s zeigt auf den Master selbst (%s) — Zyklus" % (key, value))
    return None


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
        Standard-Format normalisiert (dict[str, str|int|bool]).

        Enthält KEINE Secrets – die injiziert der Proxy selbst.
        """
        with self._lock:
            merged = dict(self._global)
            if mac:
                merged.update(self._sats.get(mac.lower(), {}))
        for k in SECRET_KEYS:
            merged.pop(k, None)
        for k in BACKEND_KEYS + URL_KEYS:
            merged.pop(k, None)  # Master-eigene Ziele: Satelliten leiten ihre
            # Proxy-Adressen selbst aus MASTER_URL ab und brauchen sie nie.
        return merged

    def get_secret(self, key: str) -> str:
        """Secret (nur global, nur intern – Proxy)."""
        with self._lock:
            return self._global.get(key) or ""

    def get_backend(self, key: str) -> str:
        """Gespeicherter Wert eines Master-Ziels (*_URL oder Legacy-Alias
        *_BACKEND_URL), sonst ''."""
        with self._lock:
            return str(self._global.get(key) or "")

    def snapshot(self) -> dict:
        """Vollständiger Stand für die UI (inkl. Version).

        Secret-Werte werden maskiert ("" / "gesetzt") – nie im Klartext.
        """
        with self._lock:
            g = dict(self._global)
            for k in SECRET_KEYS:
                g[k] = "gesetzt" if (g.get(k) or "") else ""
            # MASTER_TOKEN kann auch nur per ENV gesetzt sein (Deploy-Fallback);
            # die UI soll es dann trotzdem als "gesetzt" zeigen.
            if not (g.get("MASTER_TOKEN") or "") and os.environ.get(
                    "MASTER_TOKEN", "").strip():
                g["MASTER_TOKEN"] = "gesetzt"
            return {
                "version": self._version,
                "global": g,
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
        """Globale Schluessel setzen (EDITABLE_KEYS + GLOBAL_ONLY + Secrets).

        Zusaetzlich: *_URL = die echten Master-Ziele (im WebUI in der jew.
        API-Domain editierbar; der Master benutzt sie fuer Proxy/Chat/TTS/STT)
        und der Legacy-Alias *_BACKEND_URL. Loop-Guards gelten fuer beide;
        an Satelliten werden diese Keys nie ausgeliefert.
        """
        allowed = (set(EDITABLE_KEYS) | set(GLOBAL_ONLY_KEYS) | SECRET_KEYS
                   | set(BACKEND_KEYS) | set(URL_KEYS))
        def _m() -> None:
            for k, v in (overrides or {}).items():
                if k not in allowed:
                    continue
                why = backend_loop_reject(k, v)
                if why:
                    log.warning("Zentrale Konfig abgelehnt: %s", why)
                    continue
                self._global[k] = v
                if k in URL_KEYS:
                    # Alias bereinigen: *_URL und *_BACKEND_URL duerfen
                    # nicht auseinanderlaufen (Store-Wert gewinnt zuerst).
                    self._global.pop(k.replace("_URL", "_BACKEND_URL"), None)
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

    # ── Import / Export ────────────────────────────────────────

    def export_payload(self, include_secrets: bool = True) -> dict:
        """Komplette Konfiguration als portables JSON (Backup/Transfer).

        Enthält nur explizit gespeicherte Werte (Global + per-Satellite-
        Overrides) – unveränderte Defaults werden nicht exportiert und
        damit nicht festgeschrieben. include_secrets=False lässt die
        Secret-Keys komplett weg (Datei dann sicher weitergebbar).
        """
        with self._lock:
            g: dict = {}
            for k, v in self._global.items():
                if v is None or v == "":
                    continue
                if not include_secrets and k in SECRET_KEYS:
                    continue
                g[k] = v
            sats = {
                mac: {k: v for k, v in (ov or {}).items() if v is not None}
                for mac, ov in self._sats.items()
            }
            sats = {m: ov for m, ov in sats.items() if ov}
            version = self._version
        return {
            "app": "voice-satellite",
            "kind": "config",
            "format": 1,
            "source": "master",
            "exported_at": int(time.time()),
            "version": version,
            "global": g,
            "satellites": sats,
        }

    def export_master(self, include_secrets: bool = True) -> dict:
        """Komplette Master-Konfiguration: alle globalen Werte (inkl.
        Backend-Ziele, System-/Gedaechtnis-Keys, Secrets) plus aller
        per-Satelliten-Overrides. Genau die Datei, die der Import-Button
        braucht, um einen neuen Master vollstaendig aufzusetzen — also
        identisch zu export_payload, nur mit kind="master"."""
        payload = self.export_payload(include_secrets=include_secrets)
        payload["kind"] = "master"
        return payload

    def export_satellites(self, include_secrets: bool = True,
                          master_url: str = "") -> dict:
        """Minimum fuer ein neues Geraet (Pi/App), das den Master erreichen
        soll: nur MASTER_URL (Adresse) + MASTER_TOKEN (Zugang). Keine
        Modell-/Sprach-/Intent-Konfiguration — die zieht das Geraet per
        Heartbeat selbst vom Master. include_secrets=False laesst den Token
        weg (dann nur noch die Adresse)."""
        with self._lock:
            token = self._global.get("MASTER_TOKEN") or ""
            version = self._version
        g: dict = {}
        payload = {
            "app": "voice-satellite",
            "kind": "satellite",
            "format": 1,
            "source": "master",
            "exported_at": int(time.time()),
            "version": version,
            "master_url": master_url,
            "global": g,
            "satellites": {},
        }
        if include_secrets and token:
            # Token in global spiegeln, damit der Master-Import ihn anwenden
            # kann; master_url bleibt top-level (kein Store-Global-Key).
            g["MASTER_TOKEN"] = token
            payload["master_token"] = token
        return payload

    def import_payload(self, data: dict) -> dict:
        """Konfig-Datei (export_payload-Format) in den Store importieren.

        Semantik: Merge (bestehende Werte bleiben, gleiche Keys überschreiben).
        Akzeptiert Master-Export, Satellite-Export (die Overrides werden dem
        MAC-Eintrag des Satelliten zugeordnet) oder ein flaches
        {KEY: Wert}-Dict (→ global). Maskierungs-Platzhalter 'gesetzt'
        werden übersprungen. Nicht erlaubte Keys landen in unknown_keys.
        """
        if not isinstance(data, dict):
            raise ValueError("Import-Datei muss ein JSON-Objekt sein")
        g_allowed = (set(EDITABLE_KEYS) | set(GLOBAL_ONLY_KEYS)
                     | set(SECRET_KEYS) | set(BACKEND_KEYS) | set(URL_KEYS))
        global_in = data.get("global") if isinstance(data.get("global"), dict) else {}
        sats_in = {str(m).lower(): ov
                   for m, ov in (data.get("satellites") or {}).items()
                   if isinstance(ov, dict)}
        # Satellite-Export: eigene Overrides → Eintrag seiner MAC (oder global)
        ov_sat = data.get("overrides")
        if isinstance(ov_sat, dict) and ov_sat:
            mac = str(data.get("mac") or "").lower()
            if mac:
                merged = dict(sats_in.get(mac) or {})
                merged.update({k: v for k, v in ov_sat.items() if k in EDITABLE_KEYS})
                sats_in[mac] = merged
            else:
                merged_g = dict(global_in)
                merged_g.update(ov_sat)
                global_in = merged_g
        # Flaches Dict (keine Sektionen) → globale Overrides
        if not global_in and not sats_in:
            flat = {k: v for k, v in data.items()
                    if k in g_allowed and k not in ("app", "kind", "format")}
            if flat:
                global_in = flat
        unknown: list[str] = []
        g: dict = {}
        for k, v in global_in.items():
            if k not in g_allowed:
                unknown.append(k)
            elif k in SECRET_KEYS and v == "gesetzt":
                continue  # Maskierungs-Platzhalter, echtes Secret bleibt
            else:
                g[k] = v
        sats: dict[str, dict] = {}
        for mac, ov in sats_in.items():
            clean: dict = {}
            for k, v in ov.items():
                if k not in EDITABLE_KEYS:
                    unknown.append(f"{mac}:{k}")
                elif v == "gesetzt":
                    continue
                else:
                    clean[k] = v
            if clean:
                sats[mac] = clean
        if g:
            self.set_global(g)
        for mac, ov in sats.items():
            self.set_satellite(mac, ov)
        return {
            "global_applied": len(g),
            "satellites_applied": {m: len(ov) for m, ov in sats.items()},
            "unknown_keys": sorted(set(unknown)),
        }


# Prozessweites Singleton (von api/proxy/manager genutzt).
store = ConfigStore()
