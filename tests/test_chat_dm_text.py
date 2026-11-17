"""Regressionstest: Chat-Antwort optional als Text an Zielgeraet (dm-Flag).

Feature: Web-Chat /api/chat/send {dm:true} + MCP chat_send dm-Option —
die Antwort liegt zusaetzlich als reine Textnachricht (speak=False, ohne
TTS) in der Pull-Warteschlange des Zielgeraets, unabhaengig vom 'speak'-
Schalter. statisch geprüft, da master/api.py lokal nicht importierbar ist
(Starlette/httpx fehlen — siehe test_chat_core_scope.py).
"""
import os
import re
import symtable

HERE = os.path.dirname(__file__)
API = os.path.join(HERE, "..", "master", "api.py")
MCP = os.path.join(HERE, "..", "master", "mcp_gateway.py")
UI = os.path.join(HERE, "..", "master", "static", "chat.html")


def _find(tbl, name):
    for c in tbl.get_children():
        if c.get_name() == name:
            return c
        r = _find(c, name)
        if r:
            return r
    return None


def t_core_dm_parameter():
    st = symtable.symtable(open(API).read(), API, "exec")
    core = _find(st, "_chat_send_core")
    assert core is not None
    assert any(s.get_name() == "dm" and s.is_local()
               for s in core.get_symbols()), "dm-Parameter fehlt im Kern"


def t_endpoint_passes_dm():
    src = open(API).read()
    assert re.search(r'dm = bool\(body\.get\("dm", False\)\)', src), \
        "_chat_send liest dm nicht"
    assert "who, rid, dm=dm)" in src, "_chat_send reicht dm nicht an core"


def t_core_queues_text_without_tts():
    src = open(API).read()
    assert re.search(r'manager\.queue_for\(k, answer, speak=False, src=who\)',
                     src), "queue_for(answer, speak=False) fehlt"
    # DM blockiert nicht auf 'speak': Bedingung prueft dm, nicht want_tts
    m = re.search(r'if dm and answer and not answer\.startswith\("LLM"\)', src)
    assert m, "dm-Zweig nicht gefunden"


def t_mcp_dm_passthrough():
    src = open(MCP).read()
    assert '"dm": {"type": "boolean"' in src, "MCP-Schema: dm fehlt"
    assert "dm=bool(params.get(\"dm\", False))" in src, "MCP: dm nicht durchgereicht"


def t_ui_has_dm_toggle():
    src = open(UI).read()
    assert 'id="dm-toggle"' in src, "UI: dm-Schalter fehlt"
    assert 'dm: $("dm-toggle").checked' in src, "UI: dm nicht im Request-Body"
    assert "syncDmToggle" in src, "UI: Browser-Sonderfall fehlt"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("t_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL OK")
