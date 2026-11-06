"""Chat-Datenhaltung fuer die Master-WebUI.

Zwei Datenlaufe in CHAT_DIR:
  chat/<mac>.jsonl       Eintraege der Satelliten (der Satellite pusht pro
                         Sprech-Turn user/assistant + musik, Felder:
                         {eid, ts, role, text, path}) und Web-Chat-Nachrichten.
  recs/<key>.wav         TTS-Aufnahmen: der Master zeichnet jede TTS-Ausgabe
                         mit, die ueber den TTS-Proxy laeuft (also auch alle
                         Sprech-Ausgaben der Satelliten + Web-Chat-Antworten).
                         Key = Hash des eingelesenen Texts -> gleicher Text =
                         gleiche Aufnahme (die Chat-UI findet sie ueber den
                         Eintrag-Text).

Zugriff ist filebasiert (eine Zeile je JSONL-Eintrag, appends), keine
Datenbank – passt zum leichten Master-Stack.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time

from . import config

CHAT_DIR = config.CHAT_DIR
RECS_DIR = os.path.join(CHAT_DIR, "recs")
ACK_PATH = os.path.join(CHAT_DIR, "_ack.json")
os.makedirs(RECS_DIR, exist_ok=True)

MAX_WAV = 50 * 1024 * 1024          # 50 MB pro Aufnahme
MAX_RECS = 400                      # danach aelteste raus

_lock = threading.Lock()

# erlaubte Eintrags-Typen des Satelliten (alles andere wird verworfen)
_KINDS = {"user", "assistant", "musik", "system"}


def _chat_path(mac: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9:]", "_", mac or "web")
    return os.path.join(CHAT_DIR, f"chat_{safe}.jsonl")


def key_for(text: str) -> str:
    """Stabiler Key einer Aufnahme aus dem eingelesenen Text
    (SHA-1, erste 16 Hex-Zeichen) – identisch auf Master und WebUI."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def _append_chat(mac: str, entry: dict) -> None:
    with _lock:
        with open(_chat_path(mac), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def add_chat(mac: str, kind: str, text: str, *, who: str = "",
             path: str = "", rec: str | None = None,
             out: str | None = None, think: str = "") -> dict:
    """Einen (Web-)Eintrag anhängen; gibt ihn zurueck (inkl. ts).
    out = Zielgerät der Ausgabe (nur bei assistant-Einträgen gesetzt):
    'browser' oder Satellit-Key — bestimmt, WO der Text gesprochen wurde."""
    e = {"ts": time.time(), "kind": kind, "text": text or "",
         "mac": mac or "web", "who": who or ""}
    if path:
        e["path"] = path
    if rec:
        e["rec"] = rec
    if out:
        e["out"] = out
    if think:
        e["think"] = think
    _append_chat(mac or "web", e)
    return e


def chat_entries(mac: str | None, limit: int = 200) -> list:
    """Letzte Eintraege eines Satelliten (neueste zuerst) oder alle
    (mac=None, jedes Element mit mac-Feld) – fuer den Chat-Tab."""
    import glob
    if mac:
        files = [_chat_path(mac)]
    else:
        files = sorted(glob.glob(os.path.join(CHAT_DIR, "chat_*.jsonl")))
    out = []
    for fn in files:
        try:
            with open(fn, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
    out.sort(key=lambda e: e.get("ts") or 0)
    return list(reversed(out))[:limit]


def add_sats_chat(mac: str, entries: list) -> int:
    """Chat-Eintraege des Satelliten (direktes Push oder Heartbeat).
    Dedupliziert ueber die mitgesendete eid (Pro-Mac-Counter, persistiert
    in _ack.json). Gibt die neue hoechste eid zurueck (chat_ack)."""
    last = 0
    try:
        with open(ACK_PATH, encoding="utf-8") as f:
            last = int((json.load(f).get(mac) or 0))
    except (OSError, ValueError):
        pass
    new_last = last
    n = 0
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        kind = e.get("role")
        if kind not in _KINDS:
            continue
        try:
            eid = int(e.get("eid") or 0)
        except (TypeError, ValueError):
            continue
        if eid <= last:
            continue                      # schon einmal zugestellt
        _append_chat(mac, {"ts": e.get("ts") or time.time(),
                            "kind": kind, "text": e.get("text") or "",
                            "mac": mac,
                            **({"path": e["path"]} if e.get("path") else {})})
        n += 1
        new_last = max(new_last, eid)
    if new_last != last:
        try:
            acks = {}
            try:
                with open(ACK_PATH, encoding="utf-8") as f:
                    acks = json.load(f)
            except (OSError, ValueError):
                acks = {}
            acks[mac] = new_last
            tmp = ACK_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(acks, f)
            os.replace(tmp, ACK_PATH)
        except OSError:
            pass
    return new_last


def list_recordings() -> list:
    """Alle TTS-Aufnahmen (key, groesse, mtime)."""
    out = []
    try:
        for fn in os.listdir(RECS_DIR):
            if not fn.endswith(".wav"):
                continue
            full = os.path.join(RECS_DIR, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            out.append({"key": fn[:-4], "size": st.st_size,
                        "mtime": int(st.st_mtime)})
    except OSError:
        pass
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def _prune_recs() -> None:
    """Auf MAX_RECS Aufnahmen begrenzen (aelteste loeschen)."""
    rs = list_recordings()
    for r in rs[MAX_RECS:]:
        try:
            os.remove(os.path.join(RECS_DIR, r["key"] + ".wav"))
        except OSError:
            pass


def save_recording(key: str, data: bytes) -> bool:
    """TTS-Ausgabe sichern; True, wenn neu gespeichert."""
    if not key or not re.fullmatch(r"[a-f0-9]{12,32}", key):
        return False
    if not data or len(data) < 400 or len(data) > MAX_WAV:
        return False
    fn = os.path.join(RECS_DIR, key + ".wav")
    with _lock:
        if os.path.exists(fn):
            return False
        tmp = fn + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, fn)
        _prune_recs()
        return True
