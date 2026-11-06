"""Gedaechtnis des Master-Chat-WebUIs (analog app/memory.py auf dem Satellite).

- Kurzzeit: die letzten N Eintraege eines Chats (aus dem Chat-JSONL,
  kanalgetrennt pro mac/"web") wandern als messages in den LLM-Request.
- Langzeit: nach jeder Chat-Antwort extrahiert ein kleiner LLM-Call Fakten
  ("Nutzer heisst …") in eine sqlite-Tabelle; bis zu MAX_FACTS davon
  stehen im Systemprompt.

Der Master hat kein SQLite-Chatlog — das JSONL der Chat-UI ist die
Historie; hier wird nur daraus gelesen (nur user/assistant mit Text).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading

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
    con.execute("CREATE TABLE IF NOT EXISTS facts (ts INTEGER, fact TEXT UNIQUE)")
    con.commit()
    return con


def history(chat_key: str, limit: int | None = None) -> list:
    """Kurzzeitgedaechtnis: letzte limit Nachrichten des Chats als
    [{role, content}] in Zeitreihenfolge (neueste zuletzt).

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
    facts = load_facts()
    if facts:
        system = _SYSTEM_PROMPT
        if config.CHAT_MEMORY_FACTS_ENABLED and facts:
            system += ("\n\nDas weisst du bereits ueber den Nutzer:\n- "
                       + "\n- ".join(facts[:config.CHAT_MAX_FACTS]))
        msgs.append({"role": "system", "content": system})
    msgs.extend(history(chat_key, limit))
    msgs.append({"role": "user", "content": text})
    return msgs


def load_facts() -> list:
    if not config.CHAT_MEMORY_FACTS_ENABLED:
        return []
    with _lock, _conn() as con:
        # rowid als Tiebreak: mehrere Fakten in derselben Sekunde waeren
        # sonst in der Cap-/Reihenfolge-Auswahl indeterminiert.
        return [f for (f,) in con.execute(
            "SELECT fact FROM facts ORDER BY ts DESC, rowid DESC")]


def add_facts(facts: list) -> None:
    with _lock, _conn() as con:
        for f in facts:
            con.execute("INSERT OR IGNORE INTO facts (ts, fact) VALUES (?,?)",
                        (int(__import__("time").time()), f))
        con.execute(
            "DELETE FROM facts WHERE rowid NOT IN "
            "(SELECT rowid FROM facts ORDER BY rowid DESC LIMIT ?)",
            (config.CHAT_MAX_FACTS,))


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


def extract_facts(user_msg: str, assistant_msg: str, model: str = "") -> list:
    """Synchroner Fakten-Extraktor (blockiert kurz — deshalb async-Callsite).

    Liefern nur, wenn das Upstream-LLM erreichbar ist; sonst [] + Log.
    """
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
            add_facts(facts)
            log.info("Chat-Fakten gespeichert: %s", facts)
        return facts
    except Exception as e:
        log.warning("Chat-Fakten-Extraktion fehlgeschlagen: %s", e)
        return []


def extract_facts_async(user_msg: str, assistant_msg: str, model: str = "") -> None:
    t = threading.Thread(target=extract_facts,
                         args=(user_msg, assistant_msg, model), daemon=True)
    t.start()
