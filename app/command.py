"""Kommando-Interpreter: deutscher Satz -> (geraet, wert) oder None.

  'schalte das licht jetzt an'     -> ('licht', 'ON')
  'setze die heizung auf 21 grad'  -> ('heizung', '21')
  'mach die jalousien aus'         -> ('jalousien', 'OFF')
"""
import re

_FILLERS = {"jetzt", "bitte", "sofort", "wieder", "mal", "dann", "gleich", "heute", "so", "ca"}
_VERBS = {"schalte", "schalten", "setze", "setzen", "mache", "mach", "macht", "dimme", "dimmen",
          "fahre", "fahr", "stelle", "stellen", "regle", "regeln", "starte", "stoppe", "fahr"}
_ARTICLES = {"das", "die", "der", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer",
             "im", "am", "an", "auf", "um", "zu", "zur", "zum", "beim", "vom", "no",
             "unser", "unsere", "mein", "meine"}
_ON_WORDS = {"an", "ein", "einschalten", "angehen", "angeht"}
_OFF_WORDS = {"aus", "ausschalten", "ausgehen", "ausgeht", "zu", "geschlossen"}

_STATE_RE = re.compile(
    r"(^|[ ,.])(an|ein|einschalten|angehen|angeht|aus|ausschalten|ausgehen|ausgeht)(?=$|[ ,.!?])",
    re.IGNORECASE)
_NUM_RE = re.compile(r"(?:auf|um|bis|soweit)?\s*(\d{1,3})\s*(grad|°|prozent|%)?", re.IGNORECASE)


def parse(text):
    """-> (device:str, value:'ON'|'OFF'|'17') | None."""
    raw = text.strip().rstrip(".?!").lower()
    if not raw:
        return None

    value = None
    state_tail = raw
    m = _STATE_RE.search(raw)
    if m:
        value = "ON" if m.group(2).lower() in _ON_WORDS else "OFF"
        state_tail = (raw[:m.start()] + " " + raw[m.end():]).strip()
    else:
        m2 = _NUM_RE.search(raw)
        if m2:
            value = m2.group(1)
            state_tail = (raw[:m2.start()] + " " + raw[m2.end():]).strip()

    words = re.findall(r"[a-zäöüß0-9]+", state_tail)
    keep = [w for w in words
            if w not in _FILLERS and w not in _VERBS and w not in _ARTICLES
            and w not in _ON_WORDS and w not in _OFF_WORDS
            and w not in {"grad", "prozent", "celsius"}]
    device = " ".join(keep).strip()
    if not device or value is None:
        return None
    return device, value
