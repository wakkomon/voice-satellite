"""Heartbeat-Modul: Push-Status an Master-Container.

Sendet alle HEARTBEAT_SEC einen POST an /api/satellite/{mac}/heartbeat
mit Pipeline-Status, System-Info, Komponenten-Health. Der Master
aktualisiert last_seen und Sat-Info über den heartbeat.

Zusätzlich wird in jedem Zyklus der zentrale Config-Apply vom Master
gezogen (GET /api/config/apply?mac=…) – so wirken TTS/STT/LLM-Änderungen
am Master auf den Satelliten OHNE Neustart (dynamischer Config-Konsum in
tts.py/stt.py/router.py).

WICHTIG: Der Satellit hat KEIN httpx (nur pvporcupine + Pillow). Alle
HTTP-Requests laufen über die stdlib-urllib-Clients in ``app.http``.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import socket
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, chatlog, config, http

log = logging.getLogger(__name__)

# ── System-Identifikation (einmalig beim Start) ────────────────
# SAT_NAME: lesbarer Name fuer den Master (Live-Log, Satellitenliste). Ohne
# Docker ist gethostname() der Hostname des Rechners; im Container waere er die
# Container-ID, deshalb setzt deploy.sh SAT_NAME auf den Hostnamen des Pi.
_HOSTNAME = (config._env("SAT_NAME", "") or socket.gethostname()
             or config._env("HOSTNAME", "unknown"))
_MAC = config._env("SAT_MAC", "") or config._env("SATELLITE_MAC", "") or ""

def _read_mac_from_sys(name: str) -> str:
    """MAC aus /sys/class/net/<name>/address (kein ip-Tool nötig)."""
    try:
        with open(f"/sys/class/net/{name}/address") as f:
            return f.read().strip().lower()
    except OSError:
        return ""

for _candidate in ("eth0", "wlan0", "eth1", "wlan1"):
    if _MAC:
        break
    _mac = _read_mac_from_sys(_candidate)
    if _mac:
        _MAC = _mac

# Fallback: erste NIC mit MAC, die nicht lo ist
if not _MAC:
    try:
        for _nic in sorted(os.listdir("/sys/class/net")):
            if _nic == "lo":
                continue
            _mac = _read_mac_from_sys(_nic)
            if _mac:
                _MAC = _mac
                break
    except Exception:
        pass


# ── Pipeline-Status ───────────────────────────────────────────

def get_satellite_info(pipeline) -> dict:
    """Status-Payload für den Master-Heartbeat."""
    status: dict = {
        "mac": _MAC,
        "hostname": _HOSTNAME,
        "ip": "",
        "docker_running": True,          # Altbestand, UI zeigt den Wert nicht mehr
        "config_version": config.config_version(),
        # App-Version: der Master zeigt sie in der Kopfzeile der Satelliten
        # und verifiziert nach jedem Push, ob der Satellit wirklich auf dem
        # neuen Stand laeuft.
        "version": __version__,
        # Web-Port dieses Satelliten: nur so kann der Master Sprachausgabe,
        # Lautstaerke und Audio-Ausgabe steuern (Broadcast an jeden Raum).
        "web_port": config.WEB_PORT,
        "pipeline": {
            "state": pipeline.state if pipeline else "idle",
            "web_audio_busy": getattr(pipeline, "web_audio_busy", False),
            # phase = der Schritt, der JETZT laeuft. Die Master-Tabelle zeigt
            # sie in 'Aktivitaet' (vorher: nur idle/listening/speaking).
            "phase": getattr(pipeline, "phase", None),
            "speaking": bool(getattr(pipeline, "speaking", False)),
            # letzter Live-Log-Eintrag: Fallback fuer die UI-Spalte, wenn die
            # Phase schon wieder 'bereit' ist (z.B. kurz nach Antwortende).
            "last_event": (getattr(pipeline, "events", []) or [{}])[-1].get("msg"),
        },
        "system": {
            "hostname": _HOSTNAME,
            "platform": platform.platform(),
            "cpu_load": _get_cpu_load(),
        },
        "components": {
            "tts": _check_component(config.TTS_URL),
            "stt": _check_component(config.STT_URL),
            "llm": _check_component(config.LLM_URL),
            "openhab": _check_component(config.OPENHAB_URL),
        },
    }
    # Eigene IP holen (für Master-Log)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        status["ip"] = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    return status


def _get_cpu_load() -> float:
    """1-Minuten-Load-Average."""
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return 0.0


def _check_component(url: str) -> str:
    """Component-Status prüfen (HTTP, stdlib).

    Semantik: "läuft der Dienst?" — eine jegliche HTTP-Antwort (inkl. 4xx/5xx)
    beweist, dass der Server erreichbar ist. Der Master-Proxy antwortet auf
    GET/HEAD am /proxy/-Endpoint mit 200 (Health-Probe, kein Upstream-Call).
    Nur Netzwerkfehler/Timeout = unreachable.
    """
    if not url:
        return "unconfigured"
    try:
        req = urllib.request.Request(url, method="GET")
        if config.MASTER_TOKEN and "/proxy/" in url:
            req.add_header("Authorization", f"Bearer {config.MASTER_TOKEN}")
        with urllib.request.urlopen(req, timeout=2):
            return "ok"
    except urllib.error.HTTPError:
        return "ok"  # Server antwortet (4xx/5xx) → erreichbar
    except Exception:
        return "unreachable"  # Connection-Refused / Timeout / DNS


# ── Config-Apply (Master → Satellite) ─────────────────────────

_last_master_version = 0  # letzte vom Master angewendete Config-Version


def pull_and_apply_config() -> bool:
    """Config-Änderungen vom Master ziehen und dynamisch anwenden.

    GET {MASTER_URL}/api/config/apply?mac={MAC}&version={zuletzt}
    Antwort: {"changed": bool, "version": N, "overrides": {KEY: VAL, …}}
    Nur wenn changed und tatsächlich etwas angewendet wurde → True.
    """
    global _last_master_version
    if not config.MASTER_URL or not _MAC:
        return False
    url = (
        config.MASTER_URL.rstrip("/")
        + "/api/config/apply?"
        + urllib.parse.urlencode({"mac": _MAC, "version": _last_master_version})
    )
    try:
        data = http.get_json(url, timeout=5, token=config.MASTER_TOKEN or None)
    except Exception as e:
        log.debug("Config-Apply-Abfrage fehlgeschlagen: %s", e)
        return False
    if not isinstance(data, dict):
        return False
    # Master-Version nachverfolgen, damit der Pull idle bleibt (kein No-Op-Spam).
    mv = data.get("version")
    if isinstance(mv, int):
        _last_master_version = mv
    if not data.get("changed"):
        return False
    overrides = data.get("overrides") or {}
    applied, ignored = config.apply_overrides(overrides)
    if applied:
        config.bump_config_version()
        # Lokales Snapshot persistieren (Fallback, wenn Master beim
        # nächsten Start nicht erreichbar ist).
        config.save_local_config(overrides, _last_master_version)
        log.info(
            "Config vom Master angewendet: %s (ignoriert: %s) → version %d",
            ", ".join(sorted(applied)),
            ", ".join(ignored) if ignored else "–",
            config.config_version(),
        )
    return bool(applied)


# ── Heartbeat-Thread ──────────────────────────────────────────

_running = False
_heartbeat_thread: threading.Thread | None = None
_stop_event = threading.Event()
# seq des zuletzt gemeldeten Pipeline-Events: der Live-Log des Masters soll
# genau die Events zeigen, die auch im Live-Log des Satelliten (SSE) stehen
_last_event_seq = 0


# Ein Live-Log-Eintrag soll am Master SOFORT erscheinen, nicht erst beim
# naechsten Heartbeat-Takt: die Pipeline setzt dieses Event, der
# Heartbeat-Thread meldet daraufhin Status + neue Events sofort.
_events_wake = threading.Event()


def notify_events() -> None:
    """Von der Pipeline bei jedem Live-Log-Eintrag gerufen (nicht blockierend).

    Weckt den Heartbeat-Thread, damit der Master den aktuellen Zustand der
    Pipeline sieht ('Aufnahme läuft …', 'STT …'), waehrend sie noch laeuft.
    """
    _events_wake.set()


def wait_for_event(timeout: float) -> bool:
    """Bis timeout auf einen neuen Pipeline-Event warten (Heartbeat-Thread)."""
    got = _events_wake.wait(timeout=timeout)
    _events_wake.clear()
    return got


def _fresh_events(pipeline, limit: int = 40) -> list:
    """Pipeline-Events, die der Master noch nicht bekommen hat.

    Es sind genau die Eintraege, die der Satellit in seinem Live-Log
    (SSE /events) zeigt; der Master fuehrt sie unter seinem Satellitennamen
    ebenfalls im Live-Log. Mit der laufenden seq wird nichts doppelt
    gemeldet, nach einem Satelliten-Neustart zaehlt seq wieder bei 1."""
    if not pipeline:
        return []
    evs = [e for e in getattr(pipeline, "events", [])
           if e.get("seq", 0) > _last_event_seq]
    return evs[-limit:] if len(evs) > limit else evs


def _do_heartbeat(pipeline) -> None:
    """Einmaliger Heartbeat-POST an den Master (stdlib).

    Die neuen Pipeline-Events haengen wir direkt ans Heartbeat-Payload
    ('events') – ein Request statt zwei, und der Master legt sie in seinem
    Live-Log ab, bevor er den Status speichert."""
    global _last_event_seq
    if not config.MASTER_URL or not config.HEARTBEAT_ENABLED:
        return
    if not _MAC:
        log.debug("Kein MAC gefunden – Heartbeat übersprungen")
        return
    payload = {}
    evs: list = []
    try:
        payload = get_satellite_info(pipeline)
        evs = _fresh_events(pipeline)
        if evs:
            payload["events"] = evs
            if _last_event_seq == 0:
                # erster Event-Bericht seit (Neu-)Start des Satelliten: der
                # Master setzt seine seq-Zaehlung fuer uns zurueck
                payload["events_reset"] = True
        chat_pending = chatlog.pending()
        if chat_pending:
            # ausstehende Chat-Eintraege (Push war ausgefallen) nachliefern
            payload["chat"] = chat_pending
        url = f"{config.MASTER_URL.rstrip('/')}/api/satellite/{payload['mac']}/heartbeat"
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if config.MASTER_TOKEN:
            headers["Authorization"] = f"Bearer {config.MASTER_TOKEN}"
        req = urllib.request.Request(
            url, data=data, headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                if evs:
                    _last_event_seq = max(e.get("seq", 0) for e in evs)
                if chat_pending:
                    # Master bestaetigt die uebergebenen Chat-Eintraege
                    try:
                        chat_ack = json.loads(resp.read() or b"{}").get("chat_ack")
                        if chat_ack:
                            chatlog.ack(int(chat_ack))
                    except Exception:
                        pass
                log.debug("Heartbeat OK (%s, %d Events)",
                          payload.get("hostname", "?"), len(evs))
            else:
                log.warning("Heartbeat-Antwort %d", resp.status)
    except Exception as e:
        log.warning("Heartbeat-Fehler: %s", e)


def _heartbeat_loop(pipeline) -> None:
    """Periodischer Heartbeat-POST im Thread."""
    log.info("Heartbeat-Thread gestartet (alle %ds, Master %s)",
             config.HEARTBEAT_SEC, config.MASTER_URL)
    # Lokales Config-Snapshot laden (Fallback, falls Master nicht
    # erreichbar ist) – vor dem ersten Pull, damit die Proxy-URLs
    # sofort verfügbar sind.
    config.load_local_config()
    # Config einmalig beim Start vom Master ziehen
    pull_and_apply_config()
    while not _stop_event.is_set():
        _do_heartbeat(pipeline)
        # Auf neue Live-Log-Events hoeren: der Master sieht, was die Pipeline
        # gerade tut (Aufnahme/STT/Router), statt erst bis zu HEARTBEAT_SEC
        # spaeter. Laenger als 2s darf der Status maximal veralten.
        waited = 0.0
        period = max(1.0, float(config.HEARTBEAT_SEC))
        while not _stop_event.is_set() and waited < period:
            if wait_for_event(timeout=min(2.0, period - waited)):
                _do_heartbeat(pipeline)
            waited += min(2.0, period - waited)


def start_heartbeat_thread(pipeline) -> None:
    """Heartbeat-Thread starten (wenn MASTER_URL gesetzt)."""
    global _running, _heartbeat_thread
    if _running or not config.MASTER_URL or not config.HEARTBEAT_ENABLED:
        return
    _running = True
    _stop_event.clear()
    _heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(pipeline,),
        daemon=True,
        name="sat-heartbeat",
    )
    _heartbeat_thread.start()


def stop_heartbeat_thread() -> None:
    """Heartbeat-Thread stoppen."""
    global _running, _heartbeat_thread
    if _heartbeat_thread:
        _stop_event.set()
        _heartbeat_thread.join(timeout=5)
        _heartbeat_thread = None
    _running = False
    log.info("Heartbeat-Thread gestoppt")
