"""Unit-Tests fuer master/llmmemory.py (Stdlib, laeuft lokal)."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _setup(tmp):
    """Frische Imports mit CHAT_DIR auf ein Temp-Verzeichnis zeigen."""
    for mod in list(sys.modules):
        if mod.startswith("master"):
            del sys.modules[mod]
    os.environ["CHAT_DIR"] = tmp
    os.environ["CHAT_MEMORY_DB"] = os.path.join(tmp, "memory.db")
    os.environ["CHAT_MAX_HISTORY"] = "4"
    os.environ["CHAT_MAX_FACTS"] = "3"
    from master import chatstore, llmmemory
    return chatstore, llmmemory


def _write_chat(tmp, entries):
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, "chat_web.jsonl"), "a", encoding="utf-8") as f:
        for i, e in enumerate(entries):
            e.setdefault("ts", 1000 + i)
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def t_history_filters_and_limits():
    with tempfile.TemporaryDirectory() as tmp:
        cs, mm = _setup(tmp)
        _write_chat(tmp, [
            {"kind": "user", "text": "Hallo"},
            {"kind": "musik", "text": "irgendwas"},      # gefiltert
            {"kind": "assistant", "text": "Hallo!"},
            {"kind": "system", "text": "start"},         # gefiltert
            {"kind": "assistant", "text": "   "},        # leer, gefiltert
            {"kind": "user", "text": "Wie gehts?"},
            {"kind": "assistant", "text": "Gut."},
            {"kind": "user", "text": "Danke."},
        ])
        msgs = mm.history("web")
        # CHAT_MAX_HISTORY=4, chronologisch, nur user/assistant mit Text
        assert [m["content"] for m in msgs] == \
            ["Hallo", "Hallo!", "Gut.", "Danke."], msgs
        assert [m["role"] for m in msgs] == \
            ["user", "assistant", "assistant", "user"], msgs


def t_build_messages_shape():
    with tempfile.TemporaryDirectory() as tmp:
        cs, mm = _setup(tmp)
        _write_chat(tmp, [
            {"kind": "user", "text": "Mein Name ist Karl."},
            {"kind": "assistant", "text": "Klar, Karl."},
        ])
        mm.add_facts(["Nutzer heisst Karl"])
        msgs = mm.build_messages("web", "Wie hies ich?")
        assert msgs[0]["role"] == "system"
        assert "Karl" in msgs[0]["content"]
        assert msgs[-1] == {"role": "user", "content": "Wie hies ich?"}
        # Zwischen Historie und aktueller Frage: der laufende Verlauf
        assert msgs[-2] == {"role": "assistant", "content": "Klar, Karl."}


def t_build_messages_no_facts_no_system():
    with tempfile.TemporaryDirectory() as tmp:
        cs, mm = _setup(tmp)
        _write_chat(tmp, [{"kind": "user", "text": "Hi"}])
        msgs = mm.build_messages("web", "Und?")
        assert all(m["role"] != "system" for m in msgs)
        assert msgs[-1] == {"role": "user", "content": "Und?"}


def t_facts_dedupe_and_cap():
    with tempfile.TemporaryDirectory() as tmp:
        cs, mm = _setup(tmp)
        mm.add_facts(["a", "b", "c", "d"])  # CHAT_MAX_FACTS=3 -> "a" raus
        mm.add_facts(["b", "e"])            # Duplikat bleibt weg
        facts = mm.load_facts()
        assert "a" not in facts and "b" in facts
        assert len(facts) == 3, facts


def t_parse_facts():
    from master import llmmemory as mm
    assert mm.parse_facts('["Nutzer heisst Karl"]') == ["Nutzer heisst Karl"]
    assert mm.parse_facts('Text davor ["a", "b"] und nachher') == ["a", "b"]
    assert mm.parse_facts("[]") == []
    assert mm.parse_facts("nichts JSON") == []
    assert mm.parse_facts("") == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_")]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"{len(fns)}/{len(fns)} OK")
