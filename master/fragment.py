"""Halbe-Satz-Filter: unvollstaendige Satzfetzen erkennen und verwerfen.

Sprachaufnahmen koennen mitten im Satz abbrechen (VAD-Stille, angeschnittener
Sprechstart). Der LLM soll solche Fragmente NICHT beantworten, sondern den
Marker [[HALBER_SATZ]] zurueckgeben; Satellit und App verwerfen die Antwort
dann – kein TTS, kein Chat/History-Eintrag.

Der Master injiziert die Regel zentral in jeden /proxy/llm-Request, wenn der
Config-Key LLM_DROP_FRAGMENTS aktiv ist (Config-UI, global; ENV-Fallback am
Master, Default aus). Nicht-Stream-Antworten werden normalisiert: meldet das
Modell einen Fragment-Satz (variabel formuliert), ersetzt der Proxy den
Inhalt durch den exakten Marker, damit die Clients einheitlich erkennen.

Stdlib only -> lokal testbar (tests/test_fragment_filter.py).
"""
from __future__ import annotations

import json
import os
import re

FRAGMENT_MARKER = "[[HALBER_SATZ]]"

# Die injizierte Anweisung: bewusst kurz und absolut – ein echtes Fragment
# bekommt ausschliesslich den Marker, sonst normale Antwort.
FRAGMENT_RULE = (
    "Regel: Ist die letzte Nutzer-Nachricht ein offensichtliches Satzfragment "
    "(unvollstaendig/abgebrochen, z.B. eine angeschnittene Sprachaufnahme wie "
    "'mach die' oder 'und dann im'), dann antworte exakt und ausschliesslich "
    "mit " + FRAGMENT_MARKER + " ohne jedes andere Zeichen und ohne Erklaerung. "
    "In allen anderen Faellen antworte normal."
)

# Toleranter Erkennungspfad: [[HALBER_SATZ]], [HALBER_SATZ], HALBER SATZ ...
# Der Marker muss am Anfang stehen – eine regulare Antwort beginnt nicht mit
# "HALBER SATZ", selbst wenn sie den Begriff spaeter erwaehnt.
_FRAG_RE = re.compile(r"^\[*\s*HALBER[ _\-]*SATZ\s*\]*", re.I)


def is_fragment(text) -> bool:
    """True, wenn die (fertige) Antwort ein reiner Fragment-Marker ist."""
    return bool(_FRAG_RE.match((text or "").strip()))


def enabled(store_value) -> bool:
    """Schalter-Auswertung: Config-Store-Wert hat Vorrang, sonst ENV
    LLM_DROP_FRAGMENTS am Master, Default '0' (Rollout: erst einschalten,
    wenn Satelliten und Apps den Marker-Client tragen)."""
    v = store_value
    if v is None or str(v).strip() == "":
        v = os.environ.get("LLM_DROP_FRAGMENTS", "0")
    return str(v).strip().lower() in ("1", "true", "on", "ja", "yes")


def inject_rule(body: bytes) -> bytes:
    """Chat-Completion-Body um die Fragment-Regel (system) ergaenzen.

    Haengt die Regel als weitere system-Nachricht ans Ende der messages-
    Liste. Bei Parse-Problemen oder ohne messages-Liste bleibt der Body
    unveraendert (Proxy reicht dann nur durch)."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return body
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return body
    # Idempotent: bei mehrfach durchlaufenen Proxys nicht doppelt injizieren.
    for m in messages:
        if isinstance(m, dict) and FRAGMENT_MARKER in str(m.get("content") or ""):
            return body
    messages.append({"role": "system", "content": FRAGMENT_RULE})
    try:
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body


def normalize_response(raw: bytes) -> tuple[bytes, bool]:
    """Nicht-Stream-JSON-Antwort pruefen: Fragment-Antwort -> Inhalt auf den
    exakten Marker setzen. Liefert (body, geaendert)."""
    try:
        data = json.loads(raw)
        msg = data["choices"][0]["message"]
    except (ValueError, TypeError, KeyError, IndexError):
        return raw, False
    for field in ("content", "reasoning_content"):
        if is_fragment(msg.get(field)):
            msg[field] = FRAGMENT_MARKER
            return json.dumps(data, ensure_ascii=False).encode("utf-8"), True
    return raw, False
