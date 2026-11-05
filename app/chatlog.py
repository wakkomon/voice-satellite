"""Chatlog fuer die Master-WebUI: Eintraege der Sprech-Turne + Push.

Der Satellite zeichnet pro Turn ein (user, assistant) sowie musik-Paare
auf und pusht sie an den Master (POST /api/satellite/{mac}/chat).
Lokales SQLite als Puffer; der Master dedupliziert via UNIQUE(mac, eid)
und gibt chat_ack zurueck ( hoechste bestaetigte eid ).
Der Heartbeat schickt aengeregte Eintraege als Nachschub ("chat"-Key).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request

from . import config

log = logging.getLogger("chatlog")

DB_PATH = os.path.join(config.DATA_DIR, "chatlog.db")

_lock = threading.Lock()
_send_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_ack = 0          # hoechste vom Master bestaetigte eid


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS chat ("
            "eid INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts REAL, role TEXT, text TEXT, path TEXT)")
        _conn.commit()
    return _conn


def enabled() -> bool:
    """Chatlog laeuft nur mit konfiguriertem Master (sonst kein Ziel)."""
    return bool(config.MASTER_URL)


def emit(role: str, text: str, path: str = "") -> int | None:
    """Eintrag anlegen und asynchronen Push triggern.

    role: user | assistant | musik | system
    path: zusaetzlicher Pfad/URL (z.B. Musikdatei, fuer den Player).
    Gibt die eid zurueck oder None, falls nicht gespeichert.
    """
    text = (text or "").strip()
    if not text or not enabled():
        return None
    try:
        with _lock:
            cur = _db().execute(
                "INSERT INTO chat (ts, role, text, path) VALUES (?,?,?,?)",
                (time.time(), role, text[:500], path[:500]))
            eid = int(cur.lastrowid)
            _conn.commit()
    except Exception as e:
        log.warning("Chatlog-Eintrag fehlgeschlagen: %s", e)
        return None
    threading.Thread(target=_push, daemon=True).start()
    return eid


def ack(eid: int) -> None:
    """Master-ACK: alle Eintraege mit eid <= eid sind bei ihm gesichert."""
    global _ack
    try:
        with _lock:
            if eid > _ack:
                _ack = eid
                # Bestaetigte Eintraege loeschen (Puffer klein halten).
                _db().execute("DELETE FROM chat WHERE eid < ?", (eid - 300,))
                _conn.commit()
    except Exception:
        pass


def pending() -> list:
    """Noch nicht ackte Eintraege (Heartbeat-Nachschub)."""
    if not enabled():
        return []
    try:
        with _lock:
            rows = _db().execute(
                "SELECT eid, ts, role, text, path FROM chat"
                " WHERE eid > ? ORDER BY eid LIMIT 50", (_ack,)).fetchall()
        return [{"eid": r[0], "ts": r[1], "role": r[2],
                 "text": r[3], "path": r[4]} for r in rows]
    except Exception:
        return []


def _push() -> None:
    """Alle ausstehenden Eintraege an den Master senden."""
    with _send_lock:
        entries = pending()
        if not entries:
            return
        mac = _mac()
        if not mac:
            return
        url = f"{config.MASTER_URL.rstrip('/')}/api/satellite/{mac}/chat"
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps({"entries": entries}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST")
            if config.MASTER_TOKEN:
                req.add_header("Authorization",
                               f"Bearer {config.MASTER_TOKEN}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read() or b"{}")
                    ack(int(data.get("chat_ack") or entries[-1]["eid"]))
        except Exception as e:
            # Nix kritisch: der Heartbeat schickt die Eintraege nach.
            log.debug("Chat-Push wartet (Heartbeat nach): %s", e)


def _mac() -> str:
    from . import heartbeat
    return getattr(heartbeat, "_MAC", "") or ""
