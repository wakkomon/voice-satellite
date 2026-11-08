"""Gedaechtnis — zentral am Master (Master-Modus) oder lokal (Standalone).

Master-Modus (MASTER_URL gesetzt): das Gedaechtnis liegt ausschliesslich
am Master (sqlite + Chat-JSONL; editierbar im Master-WebUI unter
Konfiguration -> Gedaechtnis). Dieser Container schreibt nichts Eigenes:

- add_exchange   -> POST {MASTER}/api/memory/sat/exchange
- recent_history -> GET  {MASTER}/api/memory/sat/history
- load_facts     -> GET  {MASTER}/api/memory/sat/facts
- extract_facts  -> POST {MASTER}/api/memory/sat/extract
  (die Fakten-Extraktion macht der Master — das entlastet den Pi 3)

Bei Master-Ausfall: leerer Kontext statt lokalem Zweitspeicher — bewusst,
Nutzer-Vorgabe ist EIN zentrales Gedaechtnis im Master-Modus.

Standalone ohne MASTER_URL: wie bisher SQLite in config.MEMORY_DB
(Kurzzeit history + Langzeit facts, Kanale voice/web getrennt).
"""
import json
import logging
import re
import sqlite3
import threading
import time
from urllib.parse import urlencode

from . import config, http

log = logging.getLogger("memory")

_lock = threading.Lock()


def _master_mode() -> bool:
    """Master-Modus = MASTER_URL gesetzt (Proxy-Architektur, zentrales Gedaechtnis)."""
    return bool(getattr(config, "MASTER_URL", ""))


def _mac() -> str:
    # identisch zum Heartbeat: SAT_MAC mit SATELLITE_MAC-Fallback
    return config._env("SAT_MAC", "") or config.SATELLITE_MAC or ""


def _mget(path: str, params: dict, timeout: float = 5.0) -> dict:
    url = f"{config.MASTER_URL.rstrip('/')}{path}?{urlencode(params)}"
    return http.get_json(url, token=config.MASTER_TOKEN, timeout=timeout)


def _mpost(path: str, payload: dict, timeout: float = 10.0,
           retries: int = 1) -> dict:
    url = f"{config.MASTER_URL.rstrip('/')}{path}"
    return http.post_json(url, payload, token=config.MASTER_TOKEN,
                          timeout=timeout, retries=retries)


# ── Oeffentliche API (Router nutzt sie kanalgetrennt) ────────────────

def add_exchange(user_msg: str, assistant_msg: str, channel: str = "voice"):
    """Ein Frage-Antwort-Paar in den Kurzzeitspeicher eines Kanals.

    'voice' = Wake-Word-Pipeline, 'web' = Webui-Texteingabe (/intent).
    Die Kanäle sind getrennt: was im Webui getippt wurde, contaminert
    nicht die Sprachkonversation (und umgekehrt). Im Master-Modus liegt
    der Speicher am Master, hier nur der API-Aufruf."""
    if _master_mode():
        try:
            _mpost("/api/memory/sat/exchange",
                   {"mac": _mac(), "channel": channel,
                    "user": user_msg, "assistant": assistant_msg})
        except Exception as e:
            log.warning("Gedaechtnis: Austausch nicht am Master gespeichert: %s", e)
        return
    _local_add_exchange(user_msg, assistant_msg, channel)


def recent_history(channel: str = "voice"):
    """Kurzzeitgedaechtnis NUR dieses Kanals — getrennte Unterhaltungen."""
    if _master_mode():
        try:
            data = _mget("/api/memory/sat/history",
                         {"mac": _mac(), "channel": channel})
            return [{"role": m.get("role"), "content": m.get("content")}
                    for m in (data.get("messages") or [])]
        except Exception as e:
            log.warning("Gedaechtnis: Verlauf nicht vom Master holbar: %s", e)
            return []
    return _local_recent_history(channel)


def load_facts():
    """Langzeit-Fakten dieses Geraets (im Master-Modus: dort gespeichert)."""
    if _master_mode():
        try:
            return list(_mget("/api/memory/sat/facts",
                               {"mac": _mac()}).get("facts") or [])
        except Exception as e:
            log.warning("Gedaechtnis: Fakten nicht vom Master holbar: %s", e)
            return []
    return _local_load_facts()


def extract_facts_async(user_msg: str, assistant_msg: str):
    """Fakten-Extraktion anstossen. Master-Modus: der Master extrahiert
    (Hintergrund-Thread dort, Pi bleibt leicht); Standalone: lokal."""
    if _master_mode():
        def _run():
            try:
                _mpost("/api/memory/sat/extract",
                       {"mac": _mac(), "user": user_msg,
                        "assistant": assistant_msg,
                        "model": config.LLM_MODEL},
                       timeout=60, retries=0)
            except Exception as e:
                log.warning("Gedaechtnis: Fakten-Extraktion am Master "
                            "fehlgeschlagen: %s", e)
        threading.Thread(target=_run, daemon=True).start()
        return
    t = threading.Thread(target=extract_facts,
                         args=(user_msg, assistant_msg), daemon=True)
    t.start()


def extract_facts(user_msg: str, assistant_msg: str):
    """Nur Standalone — im Master-Modus extrahiert der Master selbst."""
    if _master_mode():
        return
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
        data = http.post_json(config.LLM_URL, payload, token=config.MASTER_TOKEN, timeout=60)
        raw = data["choices"][0]["message"]["content"].strip()
        m = re.search(r"\[.*?\]", raw, re.DOTALL)
        if m:
            facts = [str(f).strip() for f in json.loads(m.group()) if str(f).strip()]
            if facts:
                _local_add_facts(facts)
                log.info("Fakten gespeichert: %s", facts)
    except Exception as e:
        log.warning("Fakten-Extraktion fehlgeschlagen: %s", e)


# ── Standalone-Speicher (SQLite, nur ohne MASTER_URL in Nutzung) ─────

def _local_conn():
    con = sqlite3.connect(config.MEMORY_DB)
    con.execute("CREATE TABLE IF NOT EXISTS history "
                "(ts INTEGER, role TEXT, content TEXT, channel TEXT DEFAULT 'voice')")
    con.execute("CREATE TABLE IF NOT EXISTS facts  (ts INTEGER, fact TEXT UNIQUE)")
    try:
        # Altes Schema ohne channel-Spalte: Migration + einmaliges Leeren der
        # Kurzzeithistorie. Alte Webui-Eingaben sind nicht nachträglich von
        # echten Sprachwechseln zu trennen und würden sonst weiter in den
        # Voice-Prompt sickern (genau der Bug, den der channel fixt).
        con.execute("ALTER TABLE history ADD COLUMN channel TEXT DEFAULT 'voice'")
        con.execute("DELETE FROM history")
        con.commit()
    except sqlite3.OperationalError:
        pass  # Spalte existiert bereits
    return con


def _local_add_exchange(user_msg: str, assistant_msg: str, channel: str = "voice"):
    with _lock, _local_conn() as con:
        now = int(time.time())
        con.execute("INSERT INTO history VALUES (?,?,?,?)",
                    (now, "user", user_msg, channel))
        con.execute("INSERT INTO history VALUES (?,?,?,?)",
                    (now, "assistant", assistant_msg, channel))
        con.execute(
            "DELETE FROM history WHERE rowid NOT IN "
            "(SELECT rowid FROM history ORDER BY ts DESC, rowid DESC LIMIT ?)",
            (config.MAX_HISTORY * 2,),
        )


def _local_recent_history(channel: str = "voice"):
    """rowid DESC bricht Gleichstand bei gleicher Sekunde (user/assistant
    werden im selben Insert geschrieben) – sonst waere die Reihenfolge
    im Prompt unspecified."""
    with _lock, _local_conn() as con:
        rows = con.execute(
            "SELECT role, content FROM history WHERE channel=? "
            "ORDER BY ts DESC, rowid DESC LIMIT ?",
            (channel, config.MAX_HISTORY * 2),
        ).fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]


def _local_load_facts():
    with _lock, _local_conn() as con:
        return [f for (f,) in con.execute("SELECT fact FROM facts ORDER BY ts DESC, rowid DESC")]


def _local_add_facts(facts):
    with _lock, _local_conn() as con:
        for f in facts:
            con.execute("INSERT OR IGNORE INTO facts (ts, fact) VALUES (?,?)",
                        (int(time.time()), f))
        con.execute(
            "DELETE FROM facts WHERE rowid NOT IN "
            "(SELECT rowid FROM facts ORDER BY rowid DESC LIMIT ?)",
            (config.MAX_FACTS,),
        )
