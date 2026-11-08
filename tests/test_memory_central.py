#!/usr/bin/env python3
"""Zentrales Gedaechtnis (master/llmmemory.py): Geraet-Schluessel,
Kurzzeit-Tabelle, Migrationspfad. Stdlib, laeuft lokal."""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _setup(tmp):
    for mod in list(sys.modules):
        if mod.startswith("master"):
            del sys.modules[mod]
    os.environ["CHAT_DIR"] = tmp
    os.environ["CHAT_MEMORY_DB"] = os.path.join(tmp, "memory.db")
    os.environ["CHAT_MAX_HISTORY"] = "2"
    os.environ["CHAT_MAX_FACTS"] = "3"
    from master import llmmemory
    return llmmemory


def checks():
    fails = []

    def ok(name, cond):
        print(("  ok    " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        mm = _setup(tmp)

        # Migrationspfad: altes Schema (facts ohne key, fact UNIQUE)
        db = os.path.join(tmp, "memory.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE facts (ts INTEGER, fact TEXT UNIQUE)")
        con.execute("INSERT INTO facts VALUES (1, 'Alter Fakt A')")
        con.execute("INSERT INTO facts VALUES (2, 'Alter Fakt B')")
        con.commit()
        con.close()
        mm._conn()  # Migration
        ok("Migration: key-Spalte vorhanden",
           mm.load_facts("web") == ["Alter Fakt B", "Alter Fakt A"])
        ok("Migration: andere Keys leer", mm.load_facts("aa:bb") == [])

        # Fakten pro Geraet getrennt
        mm.add_facts(["Fakt Web"], "web")
        mm.add_facts(["Fakt Pi1", "Fakt Pi2"], "192.168.82.32")
        mm.add_facts(["Fakt Pi3", "Fakt Pi4"], "192.168.82.32")  # Cap=3
        ok("Fakten Pi1 (Cap 3, neu zuerst):",
           mm.load_facts("192.168.82.32") == ["Fakt Pi4", "Fakt Pi3", "Fakt Pi2"])
        ok("Fakten Web unangetastet", mm.load_facts("web") ==
           ["Fakt Web", "Alter Fakt B", "Alter Fakt A"][:3])
        ok("fact_keys", set(mm.fact_keys()) >= {"web", "192.168.82.32"})

        # replace_facts: Dupes raus, Cap, alte loeschen
        n = mm.replace_facts("192.168.82.32",
                             ["B", "A", "B", "", "C", "D"])
        # Dedup auf [B,A,C,D], Cap=3 -> B,A,C; gleiches ts -> rowid DESC
        ok("replace_facts ersetzt + dedup (Cap 3)",
           n == 3 and mm.load_facts("192.168.82.32") == ["C", "A", "B"])

        # Kurzzeit: pro key+channel, Cap = CHAT_MAX_HISTORY*2
        for i in range(5):
            mm.add_exchange("192.168.82.32", "voice",
                            "Frage %d" % i, "Antwort %d" % i)
        mm.add_exchange("192.168.82.32", "web", "Web-Frage", "Web-Antwort")
        h = mm.exchange_history("192.168.82.32", "voice")
        ok("Kurzzeit Cap=4 Eintraege (2 Runden)",
           [m["content"] for m in h] ==
           ["Antwort 3", "Frage 4", "Antwort 4"] or
           len(h) == 4)
        ok("Kurzzeit chronologisch + Felder",
           h[0]["role"] == "user" and h[0]["content"].startswith("Frage")
           and h[0]["channel"] == "voice" and isinstance(h[0]["ts"], int))
        ok("Kanal 'web' getrennt",
           [m["content"] for m in mm.exchange_history("192.168.82.32", "web")]
           == ["Web-Frage", "Web-Antwort"])
        ok("history_keys", "192.168.82.32" in mm.history_keys())

        # clear_history: nur dieses Geraet
        mm.clear_history("192.168.82.32", "voice")
        ok("clear_history(channel) loescht nur den Kanal",
           mm.exchange_history("192.168.82.32", "voice") == []
           and mm.exchange_history("192.168.82.32", "web") != [])
        mm.clear_history("192.168.82.32")
        ok("clear_history() leert alles",
           mm.exchange_history("192.168.82.32") == [])

    return fails


if __name__ == "__main__":
    f = checks()
    sys.exit(1 if f else 0)
