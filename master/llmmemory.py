"""Zentrales Gedaechtnis des Masters — Geraete-Speicher (Master-Modus).

Im Master-Modus liegt das Gedaechtnis AUSSCHLIESSLICH hier: der Web-Chat
des Masters UND alle Satelliten (deren app/memory.py ruft im Master-Modus
nur noch /api/memory/sat/* auf). Key pro Geraet = kanonischer
Registry-Key (Host-IP bzw. MAC) bzw. "web" fuer den Browser-Chat.

- Kurzzeit: Web-Chat -> die letzten N Eintraege aus dem Chat-JSONL;
  Satelliten -> history-Tabelle (key+channel), vom Geraet per
  POST /api/memory/sat/exchange gemeldet.
- Langzeit: Fakten in einer sqlite-Tabelle mit key-Spalte (pro Geraet bis
  CHAT_MAX_FACTS); extrahiert der Master nach Chat-Antworten und nach
  Satelliten-Turns (POST /api/memory/sat/extract).

Der Master fuehrt kein SQLite-Chatlog fuer den Web-Chat — das JSONL der
Chat-UI ist dessen Historie; hier wird nur daraus gelesen (nur
user/assistant mit Text).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time

from . import chatstore, config

log = logging.getLogger("llmmemory")

_lock = threading.Lock()

_SYSTEM_PROMPT = (
    "Du bist der Sprachassistent des Voice-Satellite-Masters und antwortest "
    "kurz und praezise. Du bist Teil eines fortlaufenden Chats: beziehe "
    "dich auf fruehere Aussagen aus dem Verlauf."
)


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(config.CHAT_MEMORY_DB)
    cols = [r[1] for r in con.execute("PRAGMA table_info(facts)")]
    if not cols:
        con.execute("CREATE TABLE facts (ts INTEGER, fact TEXT, "
                    "key TEXT DEFAULT 'web', UNIQUE(fact, key))")
    elif "key" not in cols:
        # Altes Schema: Fakten global ohne Geraet-Schluessel (fact UNIQUE).
        # Neues Schema mit key-Spalte; bestehende Fakten stammen aus dem
        # Web-Chat und bekommen "web".
        con.execute("CREATE TABLE facts_new (ts INTEGER, fact TEXT, "
                    "key TEXT DEFAULT 'web', UNIQUE(fact, key))")
        con.execute("INSERT OR IGNORE INTO facts_new (ts, fact, key) "
                    "SELECT ts, fact, 'web' FROM facts")
        con.execute("DROP TABLE facts")
        con.execute("ALTER TABLE facts_new RENAME TO facts")
    con.execute("CREATE TABLE IF NOT EXISTS history (ts INTEGER, role TEXT, "
                "content TEXT, key TEXT, channel TEXT DEFAULT 'voice')")
    con.commit()
    return con


def history(chat_key: str, limit: int | None = None) -> list:
    """Kurzzeitgedaechtnis des WEB-CHATS: letzte limit Nachrichten des
    Chats als [{role, content}] in Zeitreihenfolge (neueste zuletzt).
    (Satelliten-Historie liegt in der history-Tabelle -> exchange_history.)

    Nur user/assistant-Eintraege mit Text — musik/system-Kacheln und
    leerer Text sind kein Promptmaterial."""
    limit = limit or config.CHAT_MAX_HISTORY
    # chat_entries liefert NEUESTE zuerst: dort aufwaerlen, bis limit
    # Nutz-Eintraege gefunden sind, dann umdrehen auf Zeitreihenfolge.
    entries = chatstore.chat_entries(chat_key, limit=200)
    msgs = []
    for e in entries:
        if e.get("kind") not in ("user", "assistant"):
            continue
        t = (e.get("text") or "").strip()
        if not t:
            continue
        msgs.append({"role": "user" if e["kind"] == "user" else "assistant",
                     "content": t})
        if len(msgs) >= limit:
            break
    return list(reversed(msgs))


def build_messages(chat_key: str, text: str, limit: int | None = None) -> list:
    """Voller LLM-Request: [system?] + Kurzzeithistorie + aktuelle Frage.

    Die aktuelle Frage ist beim Aufruf (noch) NICHT im JSONL — sie wird
    nach der Antwort als assistant-Eintrag gespeichert, die Frage selbst
    kommt hier explizit als letztes Message hinzu."""
    msgs = []
    facts = load_facts(chat_key)
    if facts:
        system = _SYSTEM_PROMPT + ("\n\nDas weisst du bereits ueber den "
                                   "Nutzer:\n- "
                                   + "\n- ".join(facts[:config.CHAT_MAX_FACTS]))
        msgs.append({"role": "system", "content": system})
    msgs.extend(history(chat_key, limit))
    msgs.append({"role": "user", "content": text})
    return msgs


# ── Langzeit: Fakten, pro Geraet-Schluessel getrennt ───────────────

def load_facts(key: str = "web") -> list:
    if not config.CHAT_MEMORY_FACTS_ENABLED:
        return []
    with _lock, _conn() as con:
        # rowid als Tiebreak: mehrere Fakten in derselben Sekunde waeren
        # sonst in der Cap-/Reihenfolge-Auswahl indeterminiert.
        return [f for (f,) in con.execute(
            "SELECT fact FROM facts WHERE key=? ORDER BY ts DESC, rowid DESC",
            (key,))]


def add_facts(facts: list, key: str = "web") -> None:
    with _lock, _conn() as con:
        now = int(time.time())
        for f in facts:
            con.execute("INSERT OR IGNORE INTO facts (ts, fact, key) VALUES (?,?,?)",
                        (now, f, key))
        con.execute(
            "DELETE FROM facts WHERE key=? AND rowid NOT IN "
            "(SELECT rowid FROM facts WHERE key=? ORDER BY rowid DESC LIMIT ?)",
            (key, key, config.CHAT_MAX_FACTS))
        con.commit()


def replace_facts(key: str, facts: list) -> int:
    """Faktenliste eines Geraets komplett ersetzen (WebUI-Editor).
    Dupes werden entfernt, Cap ist CHAT_MAX_FACTS. Gibt die Anzahl der
    gespeicherten Fakten zurueck."""
    clean = list(dict.fromkeys(str(f).strip() for f in facts if str(f).strip()))
    kept = clean[:config.CHAT_MAX_FACTS]
    with _lock, _conn() as con:
        con.execute("DELETE FROM facts WHERE key=?", (key,))
        now = int(time.time())
        for f in kept:
            con.execute("INSERT OR IGNORE INTO facts (ts, fact, key) VALUES (?,?,?)",
                        (now, f, key))
        con.commit()
    return len(kept)


def fact_keys() -> list:
    """Alle Geraet-Schluessel, fuer die Fakten existieren."""
    with _lock, _conn() as con:
        return [k for (k,) in con.execute("SELECT DISTINCT key FROM facts")]


# ── Kurzzeit der Satelliten (Tabelle; Web-Chat bleibt das JSONL) ────

def add_exchange(key: str, channel: str, user_msg: str,
                 assistant_msg: str) -> None:
    """Ein Frage-Antwort-Paar eines Geraets (Kanale voice/web getrennt),
    pro key+channel auf CHAT_MAX_HISTORY*2 Eintraege begrenzt."""
    with _lock, _conn() as con:
        now = int(time.time())
        con.execute("INSERT INTO history VALUES (?,?,?,?,?)",
                    (now, "user", user_msg, key, channel))
        con.execute("INSERT INTO history VALUES (?,?,?,?,?)",
                    (now, "assistant", assistant_msg, key, channel))
        con.execute(
            "DELETE FROM history WHERE key=? AND channel=? AND rowid NOT IN "
            "(SELECT rowid FROM history WHERE key=? AND channel=? "
            "ORDER BY ts DESC, rowid DESC LIMIT ?)",
            (key, channel, key, channel, config.CHAT_MAX_HISTORY * 2))
        con.commit()


def exchange_history(key: str, channel: str = "",
                     limit: int | None = None) -> list:
    """Kurzzeit-Historie eines Geraets als [{ts, role, content, channel}]
    in Zeitreihenfolge. channel='' = alle Kanale."""
    limit = limit or config.CHAT_MAX_HISTORY
    q = "SELECT ts, role, content, channel FROM history WHERE key=?"
    args: list = [key]
    if channel:
        q += " AND channel=?"
        args.append(channel)
    q += " ORDER BY ts DESC, rowid DESC LIMIT ?"
    args.append(limit * 2)
    with _lock, _conn() as con:
        rows = con.execute(q, args).fetchall()
    return [{"ts": t, "role": r, "content": c, "channel": ch}
            for t, r, c, ch in reversed(rows)]


def clear_history(key: str, channel: str = "") -> None:
    """Verlauf eines Geraets loeschen (Fakten bleiben)."""
    q = "DELETE FROM history WHERE key=?"
    args: list = [key]
    if channel:
        q += " AND channel=?"
        args.append(channel)
    with _lock, _conn() as con:
        con.execute(q, args)
        con.commit()


def history_keys() -> list:
    """Alle Geraet-Schluessel mit Kurzzeit-Historie."""
    with _lock, _conn() as con:
        return [k for (k,) in con.execute("SELECT DISTINCT key FROM history")]


def parse_facts(raw: str) -> list:
    """Robustes Parsen der Extraktor-Antwort: erstes JSON-Array, sonst []."""
    m = re.search(r"\[.*?\]", raw or "", re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group())
    except (ValueError, TypeError):
        return []
    return [str(f).strip() for f in items if str(f).strip()]


MEMORY_EXTRACT_PROMPT = (
    "Extrahiere aus diesem Chat-Dialog maximal 3 wichtige Fakten ueber den "
    "Nutzer oder explizite Wuensche/Vorlieben die man sich merken sollte. "
    'Antworte NUR mit einer JSON-Liste von Strings, z.B.: '
    '["Nutzer heisst Thomas", "Nutzer mag Kaffee"]. '
    "Wenn es nichts Wichtiges zu merken gibt, antworte mit: []\n"
    "Dialog:\nNutzer: {user}\nDu: {assistant}"
)


def extract_facts(user_msg: str, assistant_msg: str, model: str = "",
                  key: str = "web") -> list:
    """Synchroner Fakten-Extraktor (blockiert kurz — deshalb async-Callsite).

    Liefern nur, wenn das Upstream-LLM erreichbar ist; sonst [] + Log."""
    if not config.CHAT_MEMORY_FACTS_ENABLED:
        return []
    url = config.LLM_BACKEND_URL
    if not url or not assistant_msg:
        return []
    import httpx
    from . import proxy
    tok = proxy._upstream_token("LLM_TOKEN", proxy.ENV_LLM_TOKEN)
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": "Du extrahierst Fakten. Antworte nur mit JSON."},
            {"role": "user",
             "content": MEMORY_EXTRACT_PROMPT.format(
                 user=user_msg, assistant=assistant_msg[:2000])},
        ],
        "stream": False,
        "max_tokens": 300,
    }
    try:
        r = httpx.post(url, json=payload,
                       headers=headers, timeout=60.0)
        r.raise_for_status()
        data = r.json()
        raw = data["choices"][0]["message"]["content"]
        facts = parse_facts(raw)
        if facts:
            add_facts(facts, key)
            log.info("Fakten gespeichert (%s): %s", key, facts)
        return facts
    except Exception as e:
        log.warning("Fakten-Extraktion fehlgeschlagen (%s): %s", key, e)
        return []


def extract_facts_async(user_msg: str, assistant_msg: str, model: str = "",
                        key: str = "web") -> None:
    t = threading.Thread(target=extract_facts,
                         args=(user_msg, assistant_msg, model, key),
                         daemon=True)
    t.start()
