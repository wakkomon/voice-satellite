"""Unit-Tests fuer master/chatstream.py (Stdlib, laeuft lokal)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from master.chatstream import split_sse_chunk, split_think_tags  # noqa: E402

TD = chr(60) + "think" + chr(62)
TC = chr(60) + "/think" + chr(62)


def t_sse_reasoning_and_content():
    lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"Den"}}]}',
        'data: {"choices":[{"delta":{"reasoning_content":"ke"}}]}',
        'data: {"choices":[{"delta":{"content":"Hal"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    think = answer = ""
    for ln in lines:
        r, c = split_sse_chunk(ln)
        think += r
        answer += c
    assert think == "Denke", think
    assert answer == "Hallo", answer


def t_sse_tolerates_noise():
    assert split_sse_chunk(": keepalive") == ("", "")
    assert split_sse_chunk("") == ("", "")
    assert split_sse_chunk("data: {abgebrochen") == ("", "")
    assert split_sse_chunk('data: {"choices":[]}') == ("", "")
    assert split_sse_chunk("event: ping") == ("", "")


def t_sse_reasoning_alt_key():
    r, c = split_sse_chunk('data: {"choices":[{"delta":{"reasoning":"x","content":"y"}}]}')
    assert (r, c) == ("x", "y")


def t_think_tags_split():
    a, th = split_think_tags(f"Hallo {TD}zwischengedacht{TC} tschuess")
    assert a == "Hallo  tschuess".strip() and th == "zwischengedacht", (a, th)


def t_think_tags_open_end():
    a, th = split_think_tags(f"Antwort {TD}denken bis ende")
    assert a == "Antwort" and th == "denken bis ende", (a, th)


def t_think_tags_none():
    assert split_think_tags("nur text") == ("nur text", "")


def t_think_tags_role_close_only():
    # closing tag ohne oeffnendes -> kein Thinking, Text bleibt
    a, th = split_think_tags(f"x {TC} y")
    assert th == "" and a == f"x {TC} y".strip()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("t_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL OK")
