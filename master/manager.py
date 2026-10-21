"""Satellite Manager – Raspi-Container verwalten per SSH (2.0.0alpha).

Verwaltet Docker-Container auf den Satelliten-Raspis:
  - Aktive Satelliten-Anmeldung (Heartbeat) – keine Auto-Discovery
  - Heartbeat-Tracking
  - Docker-Aktionen (restart, pause, logs)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("master.manager")

# ── SSH-Konfiguration ──────────────────────────────────────────
SSH_USER     = os.environ.get("SSH_USER", "pi")
SSH_PKEY     = os.path.expanduser(os.environ.get(
                      "SSH_PKEY",
                      os.path.join(os.path.expanduser("~"), ".ssh", "vs_master_ed25519")))
SSH_TIMEOUT  = 10  # Sekunden


@dataclass
class Satellite:
    mac: str
    ip: str
    hostname: str
    status: str = "unknown"  # online | offline | unknown
    last_seen: float = 0.0
    info: dict = field(default_factory=dict)
    docker_running: bool = False
    is_known: bool = False   # aus SATELLITES-Konfiguration


class SatManager:
    """Verwaltet Discovery, Heartbeat und Docker-Aktionen."""

    def __init__(self) -> None:
        self.satellites: dict[str, Satellite] = {}
        self._lock = threading.Lock()
        self._discovery_thread: threading.Thread | None = None  # entfernt (Auto-Discovery raus)
        self._monitor_thread: threading.Thread | None = None
        self._key_local: str | None = None
        self._status_cb = None  # Callable[[str, Satellite], None] – bei Status-Wechsel
        self._ensure_ssh_key()

    def set_status_callback(self, cb) -> None:
        """Callback(mac, sat) bei jeder Status-/Docker-Änderung (für SSE)."""
        self._status_cb = cb

    def _notify(self, mac: str, sat: "Satellite") -> None:
        """Status-Änderung an UI weitergeben (außerhalb des Locks aufrufen!)."""
        if self._status_cb is None:
            return
        try:
            self._status_cb(mac, sat)
        except Exception:
            log.debug("Status-Callback fehlgeschlagen", exc_info=True)

    def _ensure_ssh_key(self) -> None:
        """SSH-Key einrichten: Key aus Volume (ro) nach ~/.vs-ssh (0600) kopieren.

        Der gemountete Key hat 644 und liegt auf einem read-only-Volumen,
        daher geht SSH (>=600 nötig) nur über eine lokale Kopie.
        """
        try:
            sshdir = os.path.join(os.path.expanduser("~"), ".vs-ssh")
            try:
                os.makedirs(sshdir, mode=0o700, exist_ok=True)
                key = os.path.join(sshdir, "id_ed25519")
                shutil.copyfile(SSH_PKEY, key)
                os.chmod(key, 0o600)
            except (PermissionError, OSError):
                # Mount-Punkt evtl. root-eigentlich (Docker-Default) → /tmp-Fallback
                sshdir = os.path.join(tempfile.gettempdir(), "vs-ssh")
                os.makedirs(sshdir, mode=0o700, exist_ok=True)
                key = os.path.join(sshdir, "id_ed25519")
                shutil.copyfile(SSH_PKEY, key)
                os.chmod(key, 0o600)
            self._key_local = key
            log.info("SSH-Key nach %s kopiert", key)
        except Exception:
            self._key_local = None
            log.exception("SSH-Key-Setup fehlgeschlagen – Discovery läuft IP-basiert weiter")

    # ── Registrierung (keine Discovery) ─────────────────────────
    #
    # Auto-Discovery (Subnetz-Scan) ist entfernt: Satelliten melden sich
    # aktiv am Master an (Heartbeat via POST /api/heartbeat). Der Master
    # scannt dafür NICHT ins Netz. Konfigurierte Satelliten (SATELLITES)
    # erscheinen sofort im Dashboard als „unknown", bis der erste
    # Heartbeat eintrifft (dann „online").

    def _known_sats(self) -> dict[str, str]:
        """IP → optional MAC aus SATELLITES-Env (ip[,mac];ip[,mac])."""
        known: dict[str, str] = {}
        for tok in os.environ.get("SATELLITES", "").split(";"):
            tok = tok.strip()
            if not tok:
                continue
            parts = tok.split(",")
            ip = parts[0].strip()
            mac = (parts[1].strip() if len(parts) > 1 else "") or ""
            if ip:
                known[ip] = mac.lower()
        return known

    def _register_known(self) -> None:
        """Konfigurierte Satelliten als „unknown" registrieren (einmalig).

        Erst der aktive Heartbeat setzt den Status auf „online" –
        der Monitor-Loop macht ihn offline, wenn er zu lange ausbleibt.
        """
        with self._lock:
            for ip, mac in self._known_sats().items():
                key = mac or ip
                if key not in self.satellites:
                    self.satellites[key] = Satellite(
                        mac=key, ip=ip,
                        hostname=f"sat-{ip.rsplit('.', 1)[-1]}",
                        status="unknown", is_known=True,
                    )
                    log.info("Satellit registriert (wartet auf Heartbeat): %s", ip)

    # ── Heartbeat ──────────────────────────────────────────────

    def start_monitor(self) -> None:
        """Thread: Heartbeat-Status prüfen."""
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="sat-monitor"
        )
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        """Prüfe Heartbeat-Zeit jedes Satelliten."""
        max_missed = int(os.environ.get("SATELLITE_MAX_MISSED", "5"))
        interval = int(os.environ.get("SATELLITE_HEARTBEAT_SEC", "60"))

        while True:
            with self._lock:
                for mac, sat in self.satellites.items():
                    age = time.time() - sat.last_seen
                    if age > interval * max_missed and sat.status != "offline":
                        sat.status = "offline"
                        log.warning("Satellit offline: %s (%.0f s)", sat.hostname, age)
                        self._notify(mac, sat)
                    elif age < interval * 2 and sat.status == "offline":
                        sat.status = "online"
                        log.info("Satellit wieder online: %s", sat.hostname)
                        self._notify(mac, sat)
            time.sleep(interval)

    def _find_by_ip(self, ip: str) -> str | None:
        """Bestehenden Satellite-Key mit gleicher IP finden (Docker-MAC-Merge)."""
        for key, s in self.satellites.items():
            if s.ip == ip:
                return key
        return None

    def record_heartbeat(self, mac: str, data: dict) -> None:
        """Herzschlag empfangen (von Satellite-API).

        data erwartet: hostname, ip, docker_running, pipeline, system, components.
        Auch für MACs, die noch nicht bekannt sind (neu entdeckte Satelliten).
        """
        with self._lock:
            ip = data.get("ip", "")
            key = mac or ip
            if key not in self.satellites:
                # Docker-Fall: MAC- oder IP-Eintrag existiert noch nicht.
                # Versuche zuerst den bestehenden IP-Eintrag zu finden
                # (Discovery hat ggf. einen IP-basierten Eintrag angelegt).
                existing = self._find_by_ip(ip) if ip else None
                if existing is not None:
                    key = existing
                else:
                    self.satellites[key] = Satellite(
                        mac=mac,
                        ip=ip,
                        hostname=data.get("hostname") or f"sat-{key}",
                    )
            # Merge: auch wenn der KEY bereits existiert (z.B. MAC-Eintrag),
            # aktualisiere die IP auf die echte Host-IP (NAT-Quelle), falls
            # sie vom Discovery-Eintrag abweicht.
            sat = self.satellites[key]
            if ip and sat.ip != ip:
                sat.ip = ip
                log.debug("Satellite %s: IP aktualisiert auf %s (NAT-Quelle)", key, ip)
            mac_known = key
            if data.get("hostname") and data["hostname"] != sat.hostname:
                sat.hostname = data["hostname"]
            sat.status = "online"
            sat.last_seen = time.time()
            if "docker_running" in data:
                sat.docker_running = bool(data["docker_running"])
            sat.info = data
            sat.is_known = True  # Herzschlag = bekannt
        # Status-Update an UI (außerhalb des Locks)
        self._notify(key, sat)

    # ── Docker Actions ─────────────────────────────────────────

    async def action_restart_container(self, mac: str) -> dict:
        """Docker-Container auf Satellite neu starten."""
        sat = self.satellites.get(mac)
        if not sat or not sat.ip:
            return {"error": "satellite not found", "mac": mac}

        command = (
            f"ssh -i {SSH_PKEY} -o ConnectTimeout=5 -o BatchMode=no "
            f"-o StrictHostKeyChecking=no "
            f"{SSH_USER}@{sat.ip} "
            f"'sudo docker restart voice-satellite || echo container_not_found'"
        )
        try:
            result = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await result.communicate()
            if b"container_not_found" in stdout:
                return {"status": "not_found", "mac": mac}
            return {"status": "restarted", "mac": mac, "ip": sat.ip}
        except Exception as e:
            return {"status": "error", "error": str(e), "mac": mac}

    async def action_pause_container(self, mac: str) -> dict:
        """Docker-Container auf Satellite pausieren."""
        sat = self.satellites.get(mac)
        if not sat or not sat.ip:
            return {"error": "satellite not found", "mac": mac}

        command = (
            f"ssh -i {SSH_PKEY} -o ConnectTimeout=5 -o BatchMode=no "
            f"-o StrictHostKeyChecking=no "
            f"{SSH_USER}@{sat.ip} "
            f"'sudo docker pause voice-satellite'"
        )
        try:
            result = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await result.communicate()
            return {"status": "paused", "mac": mac, "ip": sat.ip}
        except Exception as e:
            return {"status": "error", "error": str(e), "mac": mac}

    async def action_unpause_container(self, mac: str) -> dict:
        """Docker-Container auf Satellite fortsetzen."""
        sat = self.satellites.get(mac)
        if not sat or not sat.ip:
            return {"error": "satellite not found", "mac": mac}

        command = (
            f"ssh -i {SSH_PKEY} -o ConnectTimeout=5 -o BatchMode=no "
            f"-o StrictHostKeyChecking=no "
            f"{SSH_USER}@{sat.ip} "
            f"'sudo docker unpause voice-satellite'"
        )
        try:
            result = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await result.communicate()
            return {"status": "unpaused", "mac": mac, "ip": sat.ip}
        except Exception as e:
            return {"status": "error", "error": str(e), "mac": mac}

    # ── Query ──────────────────────────────────────────────────

    async def list_satellites(self) -> list[dict]:
        """Liste aller bekannten Satelliten (inkl. Heartbeat-Details)."""
        with self._lock:
            return [
                {
                    "mac": s.mac,
                    "ip": s.ip,
                    "hostname": s.hostname,
                    "status": s.status,
                    "last_seen": s.last_seen,
                    "docker_running": s.docker_running,
                    "known": s.is_known,
                    "pipeline": s.info.get("pipeline"),
                    "system": s.info.get("system"),
                    "components": s.info.get("components"),
                }
                for s in self.satellites.values()
            ]

    async def get_satellite_info(self, mac: str) -> dict:
        """Infos zu einem einzelnen Satelliten."""
        with self._lock:
            sat = self.satellites.get(mac)
            if not sat:
                return {"error": "not found", "mac": mac}
            return {
                "mac": sat.mac,
                "ip": sat.ip,
                "hostname": sat.hostname,
                "status": sat.status,
                "last_seen": sat.last_seen,
                "docker_running": sat.docker_running,
                "info": sat.info,
            }

    def start(self) -> None:
        """Konfigurierte Satelliten registrieren + Heartbeat-Monitor starten."""
        self._register_known()
        self.start_monitor()
        log.info("Satellite Manager gestartet")
