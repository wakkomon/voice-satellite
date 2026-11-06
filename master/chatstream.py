"""SSE-Chunk-Parsing und Think-Auswertung fuer den Chat-LLM-Stream.

Stdlib only -> lokal testbar (tests/test_chat_stream_parsing.py), ohne
Starlette/httpx-Stack des Masters.

OpenAI-kompatible Streams senden Deltas als `data: {...}`-Zeilen, am Ende
`data: [DONE]`. Gedankengaenge von Thinking-Modellen kommen je nach Backend
unterschiedlich:
  - delta.reasoning_content  (vLLM / LiteLLM / Hermes-Gateway auf .33:8642)
  - inline in delta.content  als  -Tags (Qwen-Chat-Format)
  - delta.reasoning        (einige Gateway-Dialekte)
Die Chat-UI zeigt beides getrennt: Thinking als aufklappbarer Block,
Antwort als laufender Text.
"""
from __future__ import annotations

import json
import re

_THINK_OPEN = re.compile(r"<\s*(think|thinking)\s*>")
_THINK_CLOSE = re.compile(r"<\s*/\s*(think|thinking)\s*>")

_REASON_KEYS = ("reasoning_content", "reasoning")


def split_sse_chunk(line: str) -> tuple[str, str]:
    """Eine SSE-Zeile parsen -> (reasoning_delta, content_delta).

    Keepalive-Kommentare, Parse-Fehler und [DONE] liefern still ('', '') —
    der Stream laeuft dann einfach weiter.
    """
    line = line.strip()
    if not line.startswith("data:"):
        return "", ""
    body = line[len("data:"):].strip()
    if not body or body == "[DONE]":
        return "", ""
    try:
        obj = json.loads(body)
    except ValueError:
        return "", ""
    choices = obj.get("choices") or []
    delta = (choices[0] if choices else {}).get("delta") or {}
    reasoning = "".join(str(delta.get(k) or "") for k in _REASON_KEYS)
    return reasoning, str(delta.get("content") or "")


def split_think_tags(text: str) -> tuple[str, str]:
    """Inline-Denktags aus content extrahieren -> (answer, think).

    Ein offenes Denktags ohne Abschluss zaehlt bis zum Textende als
    Thinking (Streaming-Zwischenstand).
    """
    if not (_THINK_OPEN.search(text) or _THINK_CLOSE.search(text)):
        return text, ""
    think_parts: list[str] = []
    answer_parts: list[str] = []
    pos = 0
    in_think = False
    while True:
        pat = _THINK_CLOSE if in_think else _THINK_OPEN
        m = pat.search(text, pos)
        if not m:
            (think_parts if in_think else answer_parts).append(text[pos:])
            break
        (think_parts if in_think else answer_parts).append(text[pos:m.start()])
        pos = m.end()
        in_think = not in_think
    return "".join(answer_parts).strip(), "".join(think_parts).strip()
