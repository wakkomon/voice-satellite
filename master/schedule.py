"""Master-zentrales Scheduling: Wecker/Timer/Erinnerungen, die im
MASTER-Modus von den Satelliten gemeldet wurden.

Semantik (verbindlich): Im Master-Modus leben die Instanzen beim Master
(nicht auf dem Satelliten). Der Master feuert zum Zeitpunkt und liefert
das Ereignis an das Quell-Geraet:
  * Pi-Satellit:   POST https://<ip>:<port>/notify  (Ton + Display + TTS)
  * Android-App:   Pull-Warteschlange (queue_for) mit kind-Feld; die App
                   holt es beim naechsten Heartbeat ab (kein Web-Dienst).

Persistenz: JSON im Config-Volume (ueberlebt Master-Neustarts). Offlinee
Ziele: max. 10 x 60 s Nachversuch, dann fallen sie ab (Log-Eintrag).

Aufgaben (kind="task", wiederkehrend): taeglich/woechentlich zu hh:mm —
der Prompt wird zur Zeit am LLM ausgefuehrt (task_runner-Hook aus api.py)
und die Antwort ans Geraet geliefert; danach automatisch auf den naechsten
Termin umgesetzt.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid

import httpx

from . import manager as _manager_mod
from .manager import SatManager

log = logging.getLogger("master.schedule")

KINDS = ("alarm", "timer", "reminder", "task")
KIND_LABEL = {"alarm": "Wecker", "timer": "Timer",
              "reminder": "Erinnerung", "task": "Aufgabe"}


def _next_occurrence(item: dict, now: float | None = None) -> float:
    """Naechsten Termin eines wiederkehrenden Eintrags ( lokale Zeit ).

    recur 'daily' | 'weekly'; hh/mm gesetzt; bei weekly zusaetzlich dow
    (0=Montag .. 6=Sonntag — identisch zu time.tm_wday).
    """
    now = time.time() if now is None else float(now)
    hh = max(0, min(23, int(item.get("hh") or 0)))
    mm = max(0, min(59, int(item.get("mm") or 0)))
    recur = item.get("recur") or "daily"
    lt = time.localtime(now)
    if recur == "weekly":
        dow = item.get("dow")
        dow = lt.tm_wday if not isinstance(dow, int) or dow < 0 \
            else dow % 7
        delta = (dow - lt.tm_wday) % 7
    else:
        delta = 0
    cand = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + delta,
                        hh, mm, 0, -1, -1, -1))
    if cand <= now + 1:
        step = 7 if recur == "weekly" else 1
        cand = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + delta + step,
                            hh, mm, 0, -1, -1, -1))
    return cand
MAX_RETRIES = 10
RETRY_WAIT = 60.0


class MasterSchedule:
    def __init__(self, manager: SatManager, path: str):
        self.manager = manager
        self.path = path
        self.items: list[dict] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Aufgaben-Ausfuehrung (wird von api.py gesetzt): item ->
        # {"answer": str, "audio_b64": str|None}; blockierend, laeuft im
        # Scheduler-Thread.
        self.task_runner = None
        self._load()

    # ── Persistenz ──────────────────────────────────────────────
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items") if isinstance(data, dict) else data
            if isinstance(items, list):
                self.items = [i for i in items
                              if isinstance(i, dict)
                              and i.get("kind") in KINDS
                              and isinstance(i.get("fire_at"), (int, float))]
        except FileNotFoundError:
            self.items = []
        except (OSError, ValueError) as e:
            log.warning("Master-Schedule nicht lesbar (%s): %s", self.path, e)
            self.items = []

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "items": self.items},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("Master-Schedule nicht schreibbar: %s", e)

    # ── API ─────────────────────────────────────────────────────
    def add(self, mac: str, kind: str, fire_at: float, text: str,
            recur: str | None = None, hh: int | None = None,
            mm: int | None = None, dow: int | None = None) -> dict:
        cap = 500 if kind == "task" else 200
        item = {"id": uuid.uuid4().hex[:12], "mac": (mac or "").lower(),
                "kind": kind, "fire_at": float(fire_at),
                "text": str(text or "")[:cap], "created": time.time(),
                "tries": 0}
        if kind == "task":
            item["recur"] = recur if recur in ("daily", "weekly") else "daily"
            item["hh"] = int(hh or 0)
            item["mm"] = int(mm or 0)
            if item["recur"] == "weekly":
                item["dow"] = int(dow) if isinstance(dow, int) \
                    and 0 <= dow <= 6 else time.localtime().tm_wday
            item["fire_at"] = _next_occurrence(item)
        with self._lock:
            self.items.append(item)
            self._save()
        log.info("master schedule: %s %s fuer %s um %s",
                 kind, item["id"], mac,
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(fire_at)))
        return item

    def update(self, mac: str, item_id: str, text: str | None = None,
               recur: str | None = None, hh: int | None = None,
               mm: int | None = None, dow: int | None = None,
               fire_at: float | None = None) -> dict | None:
        """Eintrag bearbeiten (Einstellungen-Seite der App)."""
        with self._lock:
            for i in self.items:
                if i["id"] != item_id or i["mac"] != (mac or "").lower():
                    continue
                if text is not None:
                    cap = 500 if i["kind"] == "task" else 200
                    i["text"] = str(text or "")[:cap]
                if i["kind"] == "task":
                    if recur in ("daily", "weekly"):
                        i["recur"] = recur
                    if hh is not None:
                        i["hh"] = max(0, min(23, int(hh)))
                    if mm is not None:
                        i["mm"] = max(0, min(59, int(mm)))
                    if dow is not None:
                        i["dow"] = max(0, min(6, int(dow)))
                    i["fire_at"] = _next_occurrence(i)
                elif fire_at is not None:
                    i["fire_at"] = float(fire_at)
                i["tries"] = 0
                self._save()
                return dict(i)
        return None

    def delete(self, mac: str, kind: str, by_time: float | None = None,
               all_: bool = False, by_id: str | None = None) -> int:
        n = 0
        with self._lock:
            if by_id:
                victim = [i for i in self.items
                          if i["id"] == by_id
                          and i["mac"] == (mac or "").lower()]
                if victim:
                    self.items = [i for i in self.items
                                  if i["id"] != by_id]
                    self._save()
                    return 1
                return 0
            sel = [i for i in self.items
                   if i["kind"] == kind and i["mac"] == (mac or "").lower()]
            if by_time is not None:
                ta = time.localtime(by_time)
                sel = [i for i in sel if
                       time.localtime(i["fire_at"])[:4] == ta[:4]
                       and time.localtime(i["fire_at"]).tm_hour == ta.tm_hour
                       and time.localtime(i["fire_at"]).tm_min == ta.tm_min]
            if all_:
                victim = sel
            elif sel:
                victim = [sorted(sel, key=lambda i: i["fire_at"])[0]]
            else:
                victim = []
            if victim:
                ids = {v["id"] for v in victim}
                self.items = [i for i in self.items if i["id"] not in ids]
                n = len(ids)
                self._save()
        return n

    def list(self, mac: str | None = None, kind: str | None = None) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self.items
                    if (mac is None or i["mac"] == (mac or "").lower())
                    and (kind is None or i["kind"] == kind)]

    # ── Scheduler-Thread ────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="master-schedule")
        self._thread.start()
        log.info("Master-Schedule-Thread gestartet (%d Eintraege geladen)",
                 len(self.items))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                due = self._pop_due()
                for item in due:
                    self._deliver(item)
            except Exception as e:
                log.error("Master-Schedule-Tick: %s", e)
            self._stop.wait(1.0)

    def _pop_due(self) -> list[dict]:
        now = time.time()
        with self._lock:
            out = [dict(i) for i in self.items if i["fire_at"] <= now]
            if out:
                ids = {i["id"] for i in out}
                self.items = [i for i in self.items if i["id"] not in ids]
                self._save()
        return out

    def _reschedule(self, item: dict) -> None:
        """Wiederkehrende Aufgabe auf den naechsten Termin setzen."""
        item["fire_at"] = _next_occurrence(item)
        item["tries"] = 0
        with self._lock:
            if item["id"] not in {i["id"] for i in self.items}:
                self.items.append(item)
                self._save()
        log.info("master schedule: Aufgabe %s -> %s", item["id"],
                 time.strftime("%Y-%m-%d %H:%M",
                               time.localtime(item["fire_at"])))

    def _deliver(self, item: dict) -> None:
        mac = item["mac"]
        sat = self.manager.satellites.get(mac)
        online = bool(sat and sat.status == "online" and not sat.disabled)
        if not online:
            item["tries"] = int(item.get("tries") or 0) + 1
            if item["tries"] >= MAX_RETRIES:
                if item.get("recur"):
                    log.warning("master schedule: Aufgabe %s uebersprungen "
                                "(Geraet nicht erreichbar) — naechster Termin",
                                item["id"])
                    self._reschedule(item)
                    return
                log.warning("master schedule: %s fuer %s abgeworfen "
                            "(Geraet nicht erreichbar)", item["kind"], mac)
                return
            item["fire_at"] = time.time() + RETRY_WAIT
            with self._lock:
                if item["id"] not in {i["id"] for i in self.items}:
                    self.items.append(item)
                    self._save()
            return
        self._deliver_to(mac, item)
        if item.get("recur"):
            self._reschedule(item)
            return
        log.info("master schedule: %s %s an %s ausgeliefert",
                 item["kind"], item["id"],
                 getattr(sat, "hostname", mac))

    def _deliver_to(self, mac: str, item: dict) -> None:
        kind, text, audio = item["kind"], item["text"], None
        if kind == "task":
            # Prompt am LLM ausfuehren (task_runner aus api.py), Antwort
            # inkl. vorgefertigtem Master-TTS ausliefern.
            res = {}
            if self.task_runner:
                try:
                    res = self.task_runner(item) or {}
                except Exception as e:
                    log.warning("master schedule: Task %s fehlgeschlagen: %s",
                                item["id"], e)
            ans = str(res.get("answer") or "").strip()
            if ans and not ans.startswith("LLM"):
                text = ans[:500]
            audio = res.get("audio_b64") or None
        speak = kind in KINDS
        if self.manager.sat_kind(mac) == "android":
            # App: Pull-Warteschlange (Heartbeat-Ack 'messages').
            self.manager.queue_for(
                mac, text, speak=speak, src="Aufgabe" if kind == "task"
                else "Master", audio_b64=audio or "",
                kind=kind)
            return
        # Pi: Push auf den Web-Dienst (/notify: Ton + Display + TTS).
        hit = self.manager.sat_target(mac)
        if not hit:
            return
        key, base_url = hit
        payload = {"kind": kind, "text": text, "src": "Master"}
        try:
            with httpx.Client(verify=False, timeout=15) as c:
                r = c.post(base_url + "/notify", json=payload)
                if r.status_code != 200:
                    log.warning("master schedule: %s /notify HTTP %d",
                                mac, r.status_code)
        except Exception as e:
            log.warning("master schedule: %s Push fehlgeschlagen: %s", mac, e)


# Singleton-Instanz (wird in api.py erzeugt, wenn der Pfad bekannt ist).
_store: MasterSchedule | None = None


def get_store() -> MasterSchedule | None:
    return _store


def set_store(store: MasterSchedule | None) -> None:
    global _store
    _store = store

