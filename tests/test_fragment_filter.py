"""Unit-Tests fuer master/fragment.py (Stdlib, laeuft lokal).

Halbe-Satz-Filter: Marker-Erkennung, Regel-Injektion in den /proxy/llm-Body
und Normalisierung von Nicht-Stream-Antworten.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from master.fragment import (FRAGMENT_MARKER, enabled, inject_rule,  # noqa: E402
                             is_fragment, normalize_response)


def t_is_fragment_variants():
    for m in ("[[HALBER_SATZ]]", "[HALBER_SATZ]", "HALBER_SATZ",
              "HALBER SATZ", "halber satz", "[[HALBER_SATZ]].",
              " [[HALBER_SATZ]] ", "[[HALBER_SATZ]] das war unvollstaendig"):
        assert is_fragment(m), m


def t_is_fragment_rejects_normal_answers():
    for a in ("Das Licht ist an.", "Spaeter hei?zt es HALBER SATZ nicht.",
              "", None, "Ich verstehe nur Bahnhof",
              "Die Lampe Flur ist an."):
        assert not is_fragment(a), a


def t_enabled_defaults_and_overrides():
    old = os.environ.pop("LLM_DROP_FRAGMENTS", None)
    try:
        assert not enabled(None)            # kein Store-Wert, kein ENV -> aus
        assert not enabled("")
        os.environ["LLM_DROP_FRAGMENTS"] = "1"
        assert enabled(None)                # ENV-Fallback am Master
        assert enabled("0") is False        # Store-Wert gewinnt
        assert enabled("true")
        assert enabled("1")
        os.environ["LLM_DROP_FRAGMENTS"] = "0"
        assert enabled("on")                # Store an, ENV aus
    finally:
        os.environ.pop("LLM_DROP_FRAGMENTS", None)
        if old is not None:
            os.environ["LLM_DROP_FRAGMENTS"] = old


def t_inject_rule_appends_system_message():
    body = json.dumps({"model": "m", "messages": [
        {"role": "system", "content": "alter Prompt"},
        {"role": "user", "content": "mach die"},
    ]}).encode()
    out = json.loads(inject_rule(body))
    msgs = out["messages"]
    assert len(msgs) == 3 and msgs[-1]["role"] == "system"
    assert FRAGMENT_MARKER in msgs[-1]["content"]
    assert msgs[0]["content"] == "alter Prompt"
    assert out["model"] == "m"
    # Idempotent: zweite Injektion andert nichts mehr.
    again = json.loads(inject_rule(json.dumps(out).encode()))
    assert len(again["messages"]) == 3


def t_inject_rule_tolerates_bad_bodies():
    assert inject_rule(b"not json") == b"not json"
    assert inject_rule(b'{"no": "messages"}') == b'{"no": "messages"}'
    assert inject_rule(b'{"messages": []}') == b'{"messages": []}'


def t_normalize_response_rewrites_fragment():
    raw = json.dumps({"choices": [{"message": {
        "content": "HALBER SATZ", "role": "assistant"}}]}).encode()
    out, changed = normalize_response(raw)
    assert changed
    msg = json.loads(out)["choices"][0]["message"]
    assert msg["content"] == FRAGMENT_MARKER
    assert msg["role"] == "assistant"


def t_normalize_response_passes_through():
    raw = json.dumps({"choices": [{"message": {
        "content": "Der Schalter ist an."}}]}).encode()
    out, changed = normalize_response(raw)
    assert not changed and out == raw
    # Thinking-Fallback: Marker in reasoning_content wird ebenfalls normiert.
    raw2 = json.dumps({"choices": [{"message": {
        "content": "", "reasoning_content": "[[HALBER_SATZ]]"}}]}).encode()
    out2, changed2 = normalize_response(raw2)
    assert changed2
    assert json.loads(out2)["choices"][0]["message"]["reasoning_content"] \
        == FRAGMENT_MARKER
    # Muell/keine choices: unveraendert durchreichen.
    assert normalize_response(b"<html>boom</html>") == (b"<html>boom</html>",
                                                        False)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_")]
    for fn in fns:
        fn()
        print("OK", fn.__name__)
    print(f"{len(fns)} Tests bestanden")
