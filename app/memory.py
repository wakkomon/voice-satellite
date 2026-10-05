"""Gedächtnis in SQLite (statt chat_history.json/memory.json).

- Kurzzeit: letzte N Nachrichten, wandern in den LLM-Prompt.
- Langzeit: extrahierte Fakten, werden dem System-Prompt vorangestellt.
"""
import json
import logging
import re
import sqlite3
import threading

from . import config, http

log = logging.getLogger("memory")

_lock = threading.Lock()


def _conn():
    con = sqlite3.connect(config.MEMORY_DB)
    con.execute("CREATE TABLE IF NOT EXISTS history "
                "(ts INTEGER, role TEXT, content TEXT, channel TEXT DEFAULT 'voice')")
    con.execute("CREATE TABLE IF NOT EXISTS facts  (ts INTEGER, fact TEXT UNIQUE)")
    try:
        # Altes Schema ohne channel-Spalte: Migration + einmaliges Leeren der
        # Kurzzeithistorie. Alte Webui-Eingaben sind nicht nachträglich von
        # echten Sprachwechseln zu trennen und würden sonst weiter in den
        # Voice-Prompt sickern (genau der Bug, den der channel fixes).
        con.execute("ALTER TABLE history ADD COLUMN channel TEXT DEFAULT 'voice'")
        con.execute("DELETE FROM history")
        con.commit()
    except sqlite3.OperationalError:
        pass  # Spalte existiert bereits
    return con


def add_exchange(user_msg: str, assistant_msg: str, channel: str = "voice"):
    """Ein Frage-Antwort-Paar in den Kurzzeitspeicher eines Kanals.

    'voice' = Wake-Word-Pipeline, 'web' = Webui-Texteingabe (/intent).
    Die Kanäle sind getrennt: was im Webui getippt wurde, hängt das
    Voice-Gespräch nicht voll – und umgekehrt.
    """
    with _lock, _conn() as con:
        now = int(__import__("time").time())
        con.execute("INSERT INTO history VALUES (?,?,?,?)",
                    (now, "user", user_msg, channel))
        con.execute("INSERT INTO history VALUES (?,?,?,?)",
                    (now, "assistant", assistant_msg, channel))
        con.execute(
            "DELETE FROM history WHERE rowid NOT IN "
            "(SELECT rowid FROM history ORDER BY ts DESC LIMIT ?)",
            (config.MAX_HISTORY * 2,),
        )


def recent_history(channel: str = "voice"):
    """Kurzzeitgedaechtnis NUR dieses Kanals – getrennte Unterhaltungen.
    rowid DESC bricht Gleichstand bei gleicher Sekunde (user/assistant
    werden im selben Insert geschrieben) – sonst waere die Reihenfolge
    im Prompt unspecified."""
    with _lock, _conn() as con:
        rows = con.execute(
            "SELECT role, content FROM history WHERE channel=? "
            "ORDER BY ts DESC, rowid DESC LIMIT ?",
            (channel, config.MAX_HISTORY * 2),
        ).fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]


def load_facts():
    with _lock, _conn() as con:
        return [f for (f,) in con.execute("SELECT fact FROM facts ORDER BY ts DESC")]


def add_facts(facts):
    with _lock, _conn() as con:
        for f in facts:
            con.execute("INSERT OR IGNORE INTO facts (ts, fact) VALUES (?,?)",
                        (int(__import__("time").time()), f))
        con.execute(
            "DELETE FROM facts WHERE rowid NOT IN "
            "(SELECT rowid FROM facts ORDER BY ts DESC LIMIT ?)",
            (config.MAX_FACTS,),
        )


def extract_facts_async(user_msg: str, assistant_msg: str):
    """Fakten-Extraktion im Hintergrund-thread (blockiert die Antwort nicht)."""
    t = threading.Thread(target=extract_facts, args=(user_msg, assistant_msg), daemon=True)
    t.start()


def extract_facts(user_msg: str, assistant_msg: str):
    prompt = config.MEMORY_EXTRACT_PROMPT.format(user=user_msg, assistant=assistant_msg)
    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": "Du extrahierst Fakten. Antworte nur mit JSON."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "max_tokens": 300,
    }
    try:
        data = http.post_json(config.LLM_URL, payload, token=config.LLM_TOKEN, timeout=60)
        raw = data["choices"][0]["message"]["content"].strip()
        m = re.search(r"\[.*?\]", raw, re.DOTALL)
        if m:
            facts = [str(f).strip() for f in json.loads(m.group()) if str(f).strip()]
            if facts:
                add_facts(facts)
                log.info("Fakten gespeichert: %s", facts)
    except Exception as e:
        log.warning("Fakten-Extraktion fehlgeschlagen: %s", e)
