"""Zeitgesteuerte Instanzen (Wecker/Timer/Erinnerung) am Satelliten.

Nur der STANDALONE-Pfad: ohne Master lebt das Storage lokal (JSON unter
DATA_DIR/schedule.json) und ein 1-sec-Ticker feuert faellige Eintraege.
Im MASTER-Modus liegt das Storage stattdessen beim Master (master/schedule.
py) — der Satellit erhaelt die Trigger per POST /notify. Der Parser/Intent
Code (app/ruleintents.py) ist in beiden Modi identisch; nur der RuleCtx
wechselt das Ziel.

Persistenz: atomares JSON (tmp + os.replace) — ueberlebt Neustarts.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid

from . import config

log = logging.getLogger("schedule")

KINDS = ("alarm", "timer", "reminder")

_store_lock = threading.Lock()
_store: "Store | None" = None
_scheduler: "Scheduler | None" = None
_scheduler_lock = threading.Lock()


def _default_path() -> str:
    return os.path.join(config.DATA_DIR, "schedule.json")


class Store:
    """JSON-Store: {kind: [items]} mit atomarer Persistenz."""

    def __init__(self, path: str):
        self.path = path
        self.items: list[dict] = []
        self.load()

    # ── Persistenz ──────────────────────────────────────────────
    def load(self) -> None:
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
            log.warning("Schedule-Store nicht lesbar (%s): %s", self.path, e)
            self.items = []

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "items": self.items},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("Schedule-Store nicht schreibbar: %s", e)

    # ── Zugriff ─────────────────────────────────────────────────
    def add(self, kind: str, fire_at: float, text: str = "") -> dict:
        item = {"id": uuid.uuid4().hex[:12], "kind": kind,
                "fire_at": float(fire_at), "text": str(text or "")[:200],
                "created": time.time()}
        with _store_lock:
            self.items.append(item)
            self.save()
        log.info("schedule: %s %s um %s", kind, item["id"],
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(fire_at)))
        return item

    def due(self, now: float | None = None) -> list[dict]:
        now = now if now is not None else time.time()
        with _store_lock:
            out = [dict(i) for i in self.items if i["fire_at"] <= now]
            for i in out:
                self.items = [x for x in self.items if x["id"] != i["id"]]
            if out:
                self.save()
        return out

    def remove(self, kind: str, by_time: float | None = None,
               all_: bool = False, which: str = "oldest") -> int:
        """Eintraege loeschen: all (alle der Sorte), by_time (um HH:MM),
        otherwise oldest (oder newest). -> Anzahl geloescht."""
        n = 0
        with _store_lock:
            sel = [i for i in self.items if i["kind"] == kind]
            if by_time is not None:
                sel = [i for i in sel if self._same_hm(i["fire_at"], by_time)]
            if all_:
                victim = sel
            elif sel:
                sel = sorted(sel, key=lambda i: i["fire_at"])
                victim = [sel[0] if which == "oldest" else sel[-1]]
            else:
                victim = []
            if victim:
                ids = {v["id"] for v in victim}
                self.items = [i for i in self.items if i["id"] not in ids]
                n = len(ids)
                self.save()
        return n

    @staticmethod
    def _same_hm(a: float, b: float) -> bool:
        ta, tb = time.localtime(a), time.localtime(b)
        return (ta.tm_hour, ta.tm_min) == (tb.tm_hour, tb.tm_min)

    def list(self, kind: str | None = None) -> list[dict]:
        with _store_lock:
            return [dict(i) for i in self.items
                    if kind is None or i["kind"] == kind]


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store(_default_path())
    return _store


class Scheduler:
    """1-sec-Ticker: faellige Eintraege feuern via on_fire(item)."""

    def __init__(self, on_fire, tick: float = 1.0):
        self.on_fire = on_fire
        self.tick = tick
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="schedule-timer")
        self._thread.start()
        log.info("Schedule-Scheduler gestartet")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for item in get_store().due():
                    self._fire(item)
            except Exception as e:
                log.error("Schedule-Tick-Fehler: %s", e)
            self._stop.wait(self.tick)

    def _fire(self, item: dict) -> None:
        try:
            self.on_fire(item)
        except Exception as e:
            log.error("Schedule-Fire %s fehlgeschlagen: %s",
                      item.get("kind"), e)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None


def start_scheduler(on_fire) -> Scheduler:
    """Standalone-Scheduler starten (idempotent)."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = Scheduler(on_fire)
        _scheduler.start()
        return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            _scheduler.stop()
            _scheduler = None

