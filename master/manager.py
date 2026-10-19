"""Satellite Manager – Raspi-Container verwalten per SSH (2.0.0alpha).

Verwaltet Docker-Container auf den Satelliten-Raspis:
  - Auto-Discovery (ping/SSH-Scan)
  - Heartbeat-Tracking
  - Docker-Aktionen (restart, pause, logs)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
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


def _ssh_base_opts(key: str | None = None) -> list[str]:
    """Gemeinsame SSH-Optionen (Key, kein Passwort, kurze Timeout)."""
    opts = [
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=5",
    ]
    if key and os.path.exists(key):
        opts += ["-i", key]
    return opts


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
        self._discovery_thread: threading.Thread | None = None
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

    # ── Discovery ──────────────────────────────────────────────────

    def start_discovery(self) -> None:
        """Thread: Parallel-TCP-Scan (Port 22) im Subnetz + SSH-Identifikation."""
        self._discovery_thread = threading.Thread(
            target=self._discover_loop, daemon=True, name="sat-discover"
        )
        self._discovery_thread.start()

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

    @staticmethod
    def _tcp_probe(ip: str) -> bool:
        """TCP-22-Port offen? (kein ping nötig, läuft auch in slim-Images)."""
        try:
            s = socket.create_connection((ip, 22), timeout=0.8)
            s.close()
            return True
        except OSError:
            return False

    def _discover_loop(self) -> None:
        """Periodischer paralleler Port-22-Scan, dann SSH-Identifikation."""
        sats_cfg = self._known_sats()  # ip -> mac (aus SATELLITES)
        if sats_cfg:
            ips = list(sats_cfg.keys())   # Nur explizit definierte IPs
        else:
            net = os.environ.get("SATELLITE_NETWORK", "192.168.1.0/24")
            prefix = ".".join(net.split(".")[:3]) + "."
            my_ip = self._own_ip()
            ips = [f"{prefix}{i}" for i in range(1, 254)
                   if f"{prefix}{i}" != my_ip]
        interval = int(os.environ.get("SATELLITE_DISCOVERY_SEC", "120"))
        from concurrent.futures import ThreadPoolExecutor

        while True:
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=64) as pool:
                open_ips = [ip for ip, ok in zip(ips, pool.map(self._tcp_probe, ips)) if ok]
            log.info("Discovery: %d Host(s) mit offenem Port 22 in %.1f s",
                     len(open_ips), time.time() - t0)
            for ip in open_ips:
                self._identify(ip)
            # Nicht (neu) gesehene explizite Satelliten → online halten
            for ip in ips:
                self._upsert(ip, sats_cfg.get(ip), "", known=True)
            with self._lock:
                for mac, sat in self.satellites.items():
                    if sat.status == "online" and (
                            time.time() - sat.last_seen > 3 * interval):
                        sat.status = "offline"
                        log.warning("Satellit offline (nicht im Scan): %s", sat.hostname)
                        self._notify(mac, sat)
            time.sleep(interval)

    def _own_ip(self) -> str:
        """Eigene IP, damit der Master sich nicht selbst entdeckt."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return ""

    def _identify(self, ip: str) -> None:
        """SSH (BatchMode, kurz) für Hostname; ohne Key nur IP-basiert."""
        try:
            result = subprocess.run(
                ["ssh"] + _ssh_base_opts(self._key_local)
                + [f"{SSH_USER}@{ip}", "hostname 2>/dev/null || true"],
                capture_output=True, timeout=4,
            )
        except (subprocess.TimeoutExpired, Exception):
            result = None
        hostname = ""
        if result is not None and result.returncode == 0:
            hostname = (result.stdout or b"").decode("utf-8", "replace").strip()
            if hostname:
                log.info("Satellit identifiziert: %s (%s)", ip, hostname)
        mac = self._get_mac(ip)
        label = hostname or (mac.split(":")[-1] if mac else ip.rsplit(".", 1)[-1])
        self._upsert(ip, mac, label)

    def _upsert(self, ip: str, mac: str | None, hostname: str,
                known: bool = False) -> None:
        """Satellite registrieren / neu bewerten (Key: MAC wenn bekannt, sonst IP)."""
        with self._lock:
            key = mac or ip
            if key in self.satellites:
                sat = self.satellites[key]
                sat.ip = ip
                if hostname and hostname != sat.hostname:
                    sat.hostname = hostname
                sat.status = "online"
                sat.last_seen = time.time()
                sat.is_known = sat.is_known or known
            else:
                self.satellites[key] = Satellite(
                    mac=key, ip=ip,
                    hostname=hostname or f"sat-{ip.rsplit('.', 1)[-1]}",
                    status="online", last_seen=time.time(), is_known=known,
                )
                log.info("Satellit %s: %s (%s)",
                         "bekannt" if known else "neu", self.satellites[key].hostname, ip)

    def _get_mac(self, ip: str) -> str | None:
        """MAC-Adresse aus ARP-Tabelle holen."""
        try:
            result = subprocess.run(
                ["ip", "n", "show", ip],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if "link/ether" in result.stdout:
                parts = result.stdout.split()
                for i, part in enumerate(parts):
                    if part == "link/ether" and i + 1 < len(parts):
                        mac_str = parts[i + 1]
                        # MAC: xx:xx:xx:xx:xx:xx
                        if len(mac_str) == 17 and mac_str.count(":") == 5:
                            return mac_str
        except Exception:
            pass
        return None

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
        """Discovery und Heartbeat-Monitor starten."""
        self.start_discovery()
        self.start_monitor()
        log.info("Satellite Manager gestartet")
