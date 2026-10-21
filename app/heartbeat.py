"""Heartbeat-Modul: Push-Status an Master-Container (2.0.0alpha).

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

from . import config, http

log = logging.getLogger(__name__)

# ── System-Identifikation (einmalig beim Start) ────────────────
_HOSTNAME = socket.gethostname() or config._env("HOSTNAME", "unknown")
_MAC = config._env("SAT_MAC", "") or ""

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
        "docker_running": True,
        "config_version": config.config_version(),
        "pipeline": {
            "state": pipeline.state if pipeline else "idle",
            "web_audio_busy": getattr(pipeline, "web_audio_busy", False),
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
        data = http.get_json(url, timeout=5)
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


def _do_heartbeat(pipeline) -> None:
    """Einmaliger Heartbeat-POST an den Master (stdlib)."""
    if not config.MASTER_URL or not config.HEARTBEAT_ENABLED:
        return
    if not _MAC:
        log.debug("Kein MAC gefunden – Heartbeat übersprungen")
        return
    payload = {}
    try:
        payload = get_satellite_info(pipeline)
        url = f"{config.MASTER_URL.rstrip('/')}/api/satellite/{payload['mac']}/heartbeat"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                log.debug("Heartbeat OK (%s)", payload.get("hostname", "?"))
            else:
                log.warning("Heartbeat-Antwort %d", resp.status)
    except Exception as e:
        log.warning("Heartbeat-Fehler: %s", e)


def _heartbeat_loop(pipeline) -> None:
    """Periodischer Heartbeat-POST im Thread."""
    log.info("Heartbeat-Thread gestartet (alle %ds, Master %s)",
             config.HEARTBEAT_SEC, config.MASTER_URL)
    # Config einmalig beim Start vom Master ziehen
    pull_and_apply_config()
    while not _stop_event.is_set():
        _do_heartbeat(pipeline)
        _stop_event.wait(config.HEARTBEAT_SEC)


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
