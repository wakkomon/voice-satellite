"""Satellite Manager – Raspi-Container verwalten per SSH

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


def _log_safe(msg, limit: int = 300) -> str:
    """Fremdtext logtauglich machen: eine Zeile, keine Steuerzeichen.

    Die Satelliten-Eventtexte enthalten erkannte Sprache – ohne das hier
    koennte ein Satellit Zeilen in mein Live-Log schmuggeln."""
    s = " ".join(str(msg or "").split())
    return s[:limit] + "…" if len(s) > limit else s

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
    kind: str = "pi"         # 'pi' (hat Web-Dienst) | 'android' (Pull-Empfang)


class SatManager:
    """Verwaltet Discovery, Heartbeat und Docker-Aktionen."""

    def __init__(self) -> None:
        self.satellites: dict[str, Satellite] = {}
        self._lock = threading.Lock()
        self._discovery_thread: threading.Thread | None = None  # entfernt (Auto-Discovery raus)
        self._monitor_thread: threading.Thread | None = None
        self._key_local: str | None = None
        self._status_cb = None  # Callable[[str, Satellite], None] – bei Status-Wechsel
        # hochste je gesehene Pipeline-seq pro Satellit (Live-Log-Dedupe)
        self._event_seqs: dict[str, int] = {}
        # Warteschlange für Android-Apps: die App betreibt keinen Web-Dienst,
        # Durchsagen (/speak, Chat-'out') können nicht zu ihr gepusht werden.
        # Sie holt sich offene Nachrichten mit ihrem nächsten Heartbeat ab.
        self._pending: dict[str, list[dict]] = {}
        self._ensure_ssh_key()

    # ── Pull-Warteschlange für Geräte ohne Web-Dienst (Android-App) ──

    def queue_for(self, key: str, text: str, speak: bool = True,
                  src: str = "", audio_b64: str = "") -> None:
        """Nachricht für ein Pull-Gerät vormerken (wird per Heartbeat abgeholt).

        src (optional): Name des Absenders. Bei Geräte-zu-Gerät-Direktnachrichten
        zeigt das Zielgerät die Quelle damit im Chat an."""
        with self._lock:
            q = self._pending.setdefault(key, [])
            item = {"text": text, "speak": bool(speak)}
            if src:
                item["src"] = str(src)[:64]
            if audio_b64:
                # Master-seitig synthetisiertes WAV — App spielt identisches
                # Audio ab statt selbst zu synthetisieren (Betonungs-Paritaet)
                item["audio_b64"] = str(audio_b64)
            q.append(item)
            del q[:-50]  # nicht unbegrenzt anwachsen lassen

    def drain_pending(self, key: str) -> list[dict]:
        """Vorgemerkte Nachrichten für einen Key holen und leeren."""
        with self._lock:
            return self._pending.pop(key, [])

    def is_android(self, key: str) -> bool:
        """True, wenn der Satellit als App gemeldet ist (kein Web-Dienst)."""
        return self.sat_kind(key) == "android"

    def sat_kind(self, key: str) -> str:
        """Melde-Typ eines Satelliten ('pi' | 'android'); 'pi' wenn unbekannt.

        Apps, die noch kein kind-Feld senden (App < 2.4.1), werden ueber den
        Key-/Hostnamen-Praefix 'android' erkannt — die App-Device-IDs beginnen
        immer damit."""
        with self._lock:
            sat = self.satellites.get((key or "").strip().lower()) \
                or self.satellites.get(key)
            return self._kind_for(key, sat)

    @staticmethod
    def _kind_for(key, sat) -> str:
        """Kern der Kind-Erkennung OHNE Lock — fuer Aufrufer, die den
        Manager-Lock bereits halten (Lock ist nicht rekursiv!)."""
        kind = str(getattr(sat, "kind", "pi") or "pi")
        if kind == "pi":
            k = (key or "").lower()
            host = (getattr(sat, "hostname", "") or "").lower()
            if k.startswith("android") or host.startswith("sat-android") \
                    or host.startswith("android"):
                kind = "android"
        return kind

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
                    sat = Satellite(
                        mac=key, ip=ip,
                        hostname=f"sat-{ip.rsplit('.', 1)[-1]}",
                        status="unknown", is_known=True,
                    )
                    self.satellites[key] = sat
                    log.info("Satellit %s: registriert (wartet auf Heartbeat)", sat.hostname)

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
                        # Bewusst KEIN log.warning: Satelliten gehen jederzeit in
                        # Bereitschaft/Sleep — die Warnung war Dauer-Rauschen im
                        # Live-Log. Statuswechsel + _notify (UI/SSE) bleiben erhalten.
                        self._notify(mac, sat)
                    elif age < interval * 2 and sat.status == "offline":
                        sat.status = "online"
                        log.info("Satellit %s: wieder online", sat.hostname)
                        self._notify(mac, sat)
            time.sleep(interval)

    def _find_by_ip(self, ip: str) -> str | None:
        """Bestehenden Satellite-Key mit gleicher IP finden (Docker-MAC-Merge)."""
        for key, s in self.satellites.items():
            if s.ip == ip:
                return key
        return None

    def record_events(self, mac: str, events: list, reset: bool = False,
                      ip: str = "") -> int:
        """Pipeline-Events eines Satelliten übernehmen (Live-Log-Parität).

        Der Satellite meldet im Heartbeat genau die Einträge, die seine
        Live-Log-Seite (/events, SSE) zeigt — dieselben Einträge führe ich
        hier unter dem Satellitennamen im Master-Live-Log und in den
        Container-Logs, damit Master und Satellite identisch zeigen.

        Dupes (Netzwerk-Retry, doppelter Heartbeat) filtere ich über die
        laufende seq des Satelliten. Startet der Satellite neu, zählt seq
        wieder bei 1; er kennzeichnet den ersten Heartbeat danach mit
        reset=True, dann fängt die Zählung hier ebenfalls von vorn an."""
        if not events:
            return 0
        fresh = [e for e in events if isinstance(e, dict)]
        if not fresh:
            return 0
        with self._lock:
            sat = self.satellites.get(mac) or self.satellites.get(mac.lower())
            key = mac if sat is not None else (self._find_by_ip(ip) if ip else None)
            if key is not None:
                sat = self.satellites.get(key)
            ident = key or mac
            name = (sat.hostname if sat and sat.hostname else ident)[:24]
            last_seq = 0 if reset else self._event_seqs.get(ident, 0)
            out = []
            for ev in fresh:
                try:
                    seq = int(ev.get("seq") or 0)
                except (TypeError, ValueError):
                    seq = 0
                if seq and seq <= last_seq:
                    continue
                msg = ev.get("msg")
                if not (isinstance(msg, str) and msg.strip()):
                    continue    # leere Events wirft das Live-Log sonst blanke Zeilen
                out.append((seq, msg))
            if not out:
                return 0
            hi = max(s for s, _ in out)
            self._event_seqs[ident] = max(hi, 0 if reset else last_seq)
        for seq, msg in sorted(out):
            # Zeit vorn (Master-Log-Format), Satellitenname als Label —
            # wie bei lokalen Eintraegen, damit das UI einheitlich rendert.
            log.info("Satellit %s: %s", name, _log_safe(msg))
        return len(out)

    def record_heartbeat(self, mac: str, data: dict) -> None:
        """Herzschlag empfangen (von Satellite-API).

        data erwartet: hostname, ip, docker_running, pipeline, system, components.
        Auch für MACs, die noch nicht bekannt sind (neu entdeckte Satelliten).
        """
        with self._lock:
            ip = data.get("ip", "")
            key = mac or ip
            # Stabiler IP-Key (aus SATELLITES-Config) hat Vorrang:
            # Docker-Container-MACs sind bei jedem Rebuild neu. Existieren
            # MAC- und IP-Eintrag desselben Satelliten parallel (z.B. weil
            # eine lokale Testanfrage einen MAC-Eintrag angelegt hat),
            # zusammenfuehren: MAC-Eintrag entfallen, Status uebernehmen.
            existing = self._find_by_ip(ip) if ip else None
            if existing is not None and existing != key:
                if key in self.satellites:
                    old_sat = self.satellites.pop(key)
                    keep = self.satellites[existing]
                    if old_sat.last_seen > keep.last_seen:
                        keep.status, keep.last_seen = (
                            old_sat.status, old_sat.last_seen)
                    if old_sat.hostname:
                        keep.hostname = old_sat.hostname
                    keep.info = old_sat.info or keep.info
                    # Event-Seq uebernehmen (Live-Log-Dupe-Filter)
                    self._event_seqs[existing] = max(
                        self._event_seqs.get(existing, 0),
                        self._event_seqs.get(key, 0))
                    log.info("Satellit %s: Duplikat zusammengefuehrt (aus %s)",
                             existing, key)
                key = existing
            elif key not in self.satellites:
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
                log.debug("Satellit %s: IP aktualisiert auf %s (NAT-Quelle)", key, ip)
            mac_known = key
            if data.get("hostname") and data["hostname"] != sat.hostname:
                sat.hostname = data["hostname"]
            sat.status = "online"
            sat.last_seen = time.time()
            if "docker_running" in data:
                sat.docker_running = bool(data["docker_running"])
            kind = str(data.get("kind") or "").strip().lower()
            if kind in ("pi", "android"):
                sat.kind = kind
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

    # ------------------------------------------------------------------
    # Anzeige- und Steuerungs-Helfer (UI-Spalte, Master -> Satellit)
    # ------------------------------------------------------------------
    DEFAULT_WEB_PORT = 8770

    @staticmethod
    def activity(sat: "Satellite") -> str:
        """Was der Satellit gerade tut - fuer die UI-Spalte 'Aktivitaet'.

        Die Phase steht vom Pipeline-Schritt BEVOR er ausgefuehrt wird
        ('STT: Aufnahme -> Text ...'), und der Herzschlag wird bei jedem
        Schritt sofort ausgeloest. Deshalb ist hier die laufende Aktion zu
        sehen und nicht das Ergebnis des letzten Durchlaufs.
        """
        pl = sat.info.get("pipeline") or {}
        phase = (pl.get("phase") or "").strip()
        state = (pl.get("state") or "").strip()
        if sat.status != "online":
            return "offline"
        if phase and phase != "bereit":
            return phase
        if state == "speak":
            return "Antwort wird gespielt"
        if state == "stt":
            return "Aufnahme"
        last = (pl.get("last_event") or "").strip()
        return last or "bereit"

    @staticmethod
    def _web_port(sat: "Satellite") -> int:
        """Port des Web-Dienstes (Heartbeat-Feld, sonst Standard)."""
        try:
            port = int((sat.info or {}).get("web_port") or 0)
        except (TypeError, ValueError):
            return SatManager.DEFAULT_WEB_PORT
        return port if 0 < port < 65536 else SatManager.DEFAULT_WEB_PORT

    def sat_target(self, key: str) -> "tuple[str, str] | None":
        """Satellit per MAC, IP oder Hostnamen -> (Schluessel, Basis-URL).

        Die Basis-URL zeigt auf den Web-Dienst des Satelliten (/speak,
        /volume, /output ...) - nur darueber kann der Master Durchsagen,
        Lautstaerke und Audio-Ausgabe steuern. None, wenn der Satellit
        unbekannt ist oder keine IP hat.
        """
        want = (key or "").strip().lower()
        if not want:
            return None
        with self._lock:
            sat = self.satellites.get(key) or self.satellites.get(want)
            if sat is None:
                for k, s in self.satellites.items():
                    host = (s.hostname or "").lower()
                    if (s.ip or "").lower() == want or host == want \
                            or host.split(".")[0] == want or k.lower() == want:
                        sat, key = s, k
                        break
            if sat is None or not sat.ip:
                return None
            return key, f"http://{sat.ip}:{self._web_port(sat)}"

    def canonical_key(self, mac: str, ip: str = "") -> str | None:
        """Kanonischer Registry-Key fuer ein (mac, ip)-Paar – genau so, wie
        record_heartbeat ihn waehlt. Docker-Satelliten melden ihre Bridge-MAC
        (z. B. 02:42:ac:11:00:03), der Registry-Key ist aber die stabile
        Host-IP (z. B. 192.168.82.32). Chat-Eintraege muessen unter genau
        diesem Key landen, sonst findet die Chat-WebUI sie bei
        Geratefilterung nicht (kein Textinhalt bei ausgewaehltem Gerat)."""
        mac = (mac or "").lower().strip()
        ip = (ip or "").strip()
        with self._lock:
            if mac and mac in self.satellites:
                return mac
            if ip:
                for key, s in self.satellites.items():
                    if s.ip and s.ip == ip:
                        return key
            return mac or None

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
                    # Kopfzeile der Satelliten-Tabelle: App-Stand, gemeldet
                    # vom Satelliten (Build-Version), und Aktionstext statt
                    # der Docker-Angabe, die auf dem Host nicht zuverlaessig
                    # war (Container-MACs aendern sich bei jedem Rebuild).
                    "app_version": (s.info.get("version") or ""),
                    # Meldetyp des Geräts: 'pi' (Web-Dienst) | 'android'
                    # (Pull-Empfang). Die Android-App filtert ihre
                    # Direktnachrichten-Zielgeräte darüber auf andere Apps.
                    "kind": s.kind,
                    "activity": self.activity(s),
                    "phase": ((s.info.get("pipeline") or {}).get("phase") or ""),
                    "last_event": ((s.info.get("pipeline") or {}).get("last_event") or ""),
                    "web_port": self._web_port(s) if s.ip else 0,
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
