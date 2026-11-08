"""Regressionstest: Chat-Kern darf keine Request-Variable aus _chat_send referenzieren.

PITFALL (Master <=2.0.49): Beim Core-Split (_chat_send -> _chat_send_core,
Commit 8394691) blieb `headers=proxy._headers(tok, req)` im Kern stehen —
`req` ist dort nicht in Scope -> jeder LLM-Aufruf im Master-Chat crashte mit
NameError, abgefangen als "LLM-Fehler" (chat_web.jsonl 07.11). Der Kern laeuft
auch ohne HTTP-Request (MCP chat_send), also ist req=None erlaubt.
Test statisch per symtable — master/api.py importiert Starlette/httpx und ist
lokal nicht importierbar.
"""
import os
import symtable

API = os.path.join(os.path.dirname(__file__), "..", "master", "api.py")


def _find(tbl, name):
    for c in tbl.get_children():
        if c.get_name() == name:
            return c
        r = _find(c, name)
        if r:
            return r
    return None


def t_core_has_no_free_request_var():
    st = symtable.symtable(open(API).read(), API, "exec")
    core = _find(st, "_chat_send_core")
    assert core is not None, "_chat_send_core gefunden?"
    for sym in core.get_symbols():
        # freien Namen, die nur aus _chat_send kommen koennen, verbieten
        if sym.get_name() == "req":
            raise AssertionError("freies 'req' in _chat_send_core (NameError-Falle)")
    for sym in core.get_symbols():
        n = sym.get_name()
        if n in ("who", "rid", "text", "mac", "speak", "out"):
            assert sym.is_local(), f"{n} muss Parameter des Kerns sein"


def t_headers_accepts_none_request():
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "master", "proxy.py")).read()
    assert "def _headers(token: str, req: Request | None = None" in src
    assert "if req is not None:" in src


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("t_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL OK")
