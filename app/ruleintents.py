"""Regelbasierte Sprachfunktionen OHNE LLM.

Zwei Schichten:
1. Parser (REINE Funktionen, keine I/O) — parse_alarm/parse_timer/...
   -> Datenklasse oder None. Das ist der Teil, der auf Pi UND App
   identisch nachgebaut wird (App: core/RuleIntents.kt, dieselbe Grammatik).
2. Executor: try_rule_intents(text, ctx) -> (gehandelt, Antwort) | None.
   Der Context (RuleCtx) kapselt Modus-Unterschiede:
   - master-Modus:   Instanzen am MASTER ablegen (POST /api/schedule ...),
                     Nachrichten/Signale/Sprachnachrichten ueber Master-Endpunkte.
   - standalone-Modus: Instanzen lokal ablegen (app/schedule.py), Trigger
                     laeuft lokal am Satelliten.
   Der Intent-Code selbst ist in beiden Modi identisch — nur der Ctx
   wechselt das Ziel.

Grammatik (Deutsch 1., Englisch 2., grob gegen STT-Varianten):
  Wecker:      "stelle Wecker auf 07:15" / "stell einen Wecker um halb acht
                (morgen)" / "wecke mich um 7 uhr" / "set an alarm for 7 am"
                / "loesche den Wecker" / "alle Wecker loeschen"
  Timer:       "stelle einen Timer auf 5 Minuten" / "Timer 10 Sekunden" /
                "starte Timer ueber 2 Stunden" / "set a timer for 10 minutes"
                / "Timer stoppen" / "alle Timer loeschen"
  Erinnerung:  "erinnere mich (morgen) um 7:30 an <text>" /
                "Erinnerung an <text> am Freitag um 9 Uhr"
  Nachricht:   "sende (eine) Nachricht an <ziel>: <text>"  (MASTER NUR)
  Sprachnachr: "sende Sprachnachricht an <ziel>: <text>"     (MASTER NUR)
  Signalton:   "rufe <ziel>" -> 10 s Signalton am Ziel         (MASTER NUR)
  openHAB:     "setze <label> auf <zahl> [Einheit]" (Einheit wird ignoriert)
  Kamera:      "zeige Kamera <name>" (siehe intent.try_show_camera + CAMERAS)
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger("ruleintents")


# ---------------------------------------------------------------------------
# Normalisierung: kleingeschrieben, Umlaute ausgeschrieben (ae/oe/ue/ss),
# Punkt/Strichpunkt erhalten (7.15 / 7:15), sonst nur a-z 0-9 + Leerzeichen.
# ---------------------------------------------------------------------------

def norm(text: str) -> str:
    t = (text or "").lower().strip()
    t = t.replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    t = re.sub(r"[^a-z0-9.: ]", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


# Ziffern-Woerter (1-12) fuer Uhrzeiten, Tage und Dichten.
_NUM_WORDS = {
    "eins": 1, "ein": 1, "zwei": 2, "drei": 3, "vier": 4, "fuenf": 5,
    "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11,
    "zwoelf": 12, "zwölf": 12,
}

_NUM = r"(?:\d{1,2}|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")"


def _num(word: str) -> int | None:
    """Ziffern-Strang oder Ziffern-Wort -> int (0-99), sonst None."""
    w = (word or "").strip()
    if not w:
        return None
    if w.isdigit():
        return int(w)
    return _NUM_WORDS.get(w.lower())


# ---------------------------------------------------------------------------
# Uhrzeit: 07:15 / 07.15 / "7 uhr" / "sieben uhr 15" / "halb acht" /
# "viertel (nach/vor) acht" / "dreiviertel acht" / Mitternacht / EN am/pm
# ---------------------------------------------------------------------------

def parse_clock(text: str) -> tuple[int, int] | None:
    """Erste Uhrzeit im (rohen ODER normalisierten) Satz. -> (h, m) oder None."""
    t = norm(text)
    if not t:
        return None
    _has_uhr = re.search(r"\b(" + _NUM + r")\s+uhr", t) is not None
    m = re.search(r"\b(\d{1,2})[:.](\d{1,2})\s*(am|pm)?\b", t)
    if m and not _has_uhr:
        h, mi = int(m.group(1)), int(m.group(2))
        if h <= 23 and mi <= 59:
            if m.group(3) == "pm" and h < 12:
                h += 12
            elif m.group(3) == "am" and h == 12:
                h = 0
            return h, mi
    m = re.search(r"\b(" + _NUM + r")\s+uhr(?:e|zeit)?\s*(?:(\d{1,2})\s*(?:minuten?)?)?\b", t)
    if m:
        h = _num(m.group(1))
        if h is not None and 0 <= h <= 23:
            mi = int(m.group(2)) if m.group(2) else 0
            if mi <= 59:
                return h, mi
    m = re.search(r"\bhalb\s+(" + _NUM + r")\b", t)
    if m:
        h = _num(m.group(1))
        if h is not None and h >= 1:
            return h - 1, 30
    m = re.search(r"\b(viertel|dreiviertel|drei\s+viertel)\s+(nach|vor)?\s*(" + _NUM + r")\b", t)
    if m:
        h = _num(m.group(3))
        if h is not None:
            if m.group(1) in ("dreiviertel", "drei viertel"):
                # "dreiviertel acht" = 7:45 (sueddeutscher Brauch)
                return (h - 1, 45) if h >= 1 else (23, 45)
            if m.group(2) == "vor":
                return (h - 1, 45) if h >= 1 else (23, 45)
            return h % 24, 15
    if re.search(r"\bmitternacht\b", t):
        return 0, 0
    # Englisch: "7 am" / "seven o'clock" / "seven fifteen"
    m = re.search(r"\b(" + _NUM + r")\s*(am|pm)\b", t)
    if m:
        h = _num(m.group(1))
        if h is not None and 1 <= h <= 12:
            if m.group(2) == "pm" and h < 12:
                h += 12
            if m.group(2) == "am" and h == 12:
                h = 0
            return h, 0
    m = re.search(r"\b(" + _NUM + r")\s+o'?clock\b", t)
    if m:
        h = _num(m.group(1))
        if h is not None:
            return h % 24, 0
    m = re.search(r"\b(" + _NUM + r")\s+(" + _NUM + r")\b", t)
    if m:
        h, mi = _num(m.group(1)), _num(m.group(2))
        if h is not None and mi is not None and 1 <= h <= 12 and mi < 60 \
                and re.search(r"\b(clock|time|today|tomorrow|alarm|morning)\b", t):
            return h % 24, mi
    return None


# ---------------------------------------------------------------------------
# Datumswörter -> Abstand in Tagen ab heute (heute=0, morgen=1, ...)
# ---------------------------------------------------------------------------

_WEEKDAYS = {
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
    "freitag": 4, "samstag": 5, "sonntag": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def parse_day(text: str, now: time.struct_time | None = None) -> int | None:
    """Relatives Datum im Satz. -> Tage ab heute oder None (heute)."""
    t = norm(text)
    if not t:
        return None
    now = now or time.localtime()
    if re.search(r"\buebermorgen\b", t):
        return 2
    if re.search(r"\bmorgen\b", t):
        return 1
    if re.search(r"\bheute\b", t):
        return 0
    m = re.search(r"\bin\s+(\d{1,2})\s+tagen\b", t)
    if m:
        d = int(m.group(1))
        if 1 <= d <= 30:
            return d
    for w, wd in _WEEKDAYS.items():
        if re.search(r"\b" + w + r"\b", t):
            delta = (wd - now.tm_wday) % 7
            # "naechsten Freitag" auf dem Freitag => in einer Woche (7)
            if delta == 0 and re.search(r"\bnaechst\w*\b", t):
                delta = 7
            return delta
    return None


def resolve_fire_time(clock: tuple[int, int], day: int | None,
                       now: time.struct_time | None = None) -> time.struct_time:
    """Uhrzeit + Tagesabstand -> naechste Zeit >= jetzt (heute, sonst morgen)."""
    now = now or time.localtime()
    base = now if not day else time.localtime(time.mktime(now) + day * 86400)
    target = time.struct_time(base[:3] + (clock[0], clock[1], 0, 0, 0, -1))
    if day is None and target < now:
        target = time.struct_time(base[:3] + (clock[0], clock[1], 0, 0, 0, -1))
        # heute 07:00 gesagt um 08:00 -> morgen
        target = time.localtime(time.mktime(target) + 86400)
    return target


# ---------------------------------------------------------------------------
# Dauer: "5 Minuten", "zehn sekunden", "2 stunden", "eine halbe stunde",
# EN "5 minutes" / "10 seconds" / "2 hours"
# ---------------------------------------------------------------------------

_DURATION_WORDS = {
    "sekunden": "s", "sekunde": "s", "sek": "s", "sec": "s", "secs": "s",
    "seconds": "s", "second": "s",
    "minuten": "m", "minute": "m", "min": "m", "minutes": "m", "minute": "m",
    "stunden": "h", "stunde": "h", "std": "h", "hours": "h", "hour": "h",
}
_DURATION_FACT = {"s": 1, "m": 60, "h": 3600}
_HALF = {"halbe", "halb", "half", "eine halbe"}


def parse_duration(text: str) -> int | None:
    """Dauer im Satz -> Sekunden (>=1, <= 24 h), sonst None."""
    t = norm(text)
    if not t:
        return None
    half = re.search(r"\b(?:eine\s+halbe|halbe|half)\s+(\w+)\b", t)
    words = re.findall(r"\b\w+\b", t)
    for i, w in enumerate(words):
        if w in _DURATION_WORDS:
            unit = _DURATION_WORDS[w]
            # Zahl davor (ggf. mehrere Woerter: "eins", "eine", "zwei")
            n = None
            for back in range(i - 1, max(-1, i - 4), -1):
                cand = words[back]
                if cand.isdigit():
                    n = int(cand)
                    break
                if cand in _NUM_WORDS:
                    n = _NUM_WORDS[cand]
                    break
                if cand in _HALF:
                    n = 0.5
                    break
                if cand in ("eine", "ein", "einen", "the", "a", "an"):
                    n = 1
                    break
                if re.match(r"^\d+(?:[.,]\d+)?$", cand):
                    n = float(cand.replace(",", "."))
                    break
            if n is None:
                continue
            secs = int(n * _DURATION_FACT[unit])
            if 1 <= secs <= 86400:
                return secs
    if half and half.group(1) in _DURATION_WORDS:
        secs = int(0.5 * _DURATION_FACT[_DURATION_WORDS[half.group(1)]])
        if 1 <= secs <= 86400:
            return secs
    return None


def fmt_duration(secs: int) -> str:
    if secs % 3600 == 0:
        h = secs // 3600
        return f"{h} Stunde{'n' if h != 1 else ''}"
    if secs % 60 == 0:
        m = secs // 60
        return f"{m} Minute{'n' if m != 1 else ''}"
    return f"{secs} Sekunde{'n' if secs != 1 else ''}"


def fmt_clock(clock: tuple[int, int]) -> str:
    return f"{clock[0]:02d}:{clock[1]:02d}"


def fmt_day(day: int | None) -> str:
    if not day:
        return ""
    if day == 1:
        return "morgen"
    if day == 2:
        return "übermorgen"
    return f"in {day} Tagen"


# ---------------------------------------------------------------------------
# Intent-Datenklassen
# ---------------------------------------------------------------------------

@dataclass
class RuleAlarm:
    action: str                 # "set" | "delete" | "delete_all"
    clock: tuple[int, int] | None = None
    day: int | None = None      # None = heute/naechste Moeglichkeit
    raw: str = ""


@dataclass
class RuleTimer:
    action: str                 # "set" | "stop" | "stop_all"
    seconds: int | None = None
    raw: str = ""


@dataclass
class RuleReminder:
    text: str
    clock: tuple[int, int] | None = None
    day: int | None = None
    raw: str = ""


@dataclass
class RuleMessage:
    target: str
    text: str = ""
    voice: bool = False         # True = Sprachnachricht (TTS am Ziel)
    raw: str = ""


@dataclass
class RuleCall:
    target: str                 # Rufe <ziel> -> 10 s Signalton
    raw: str = ""


@dataclass
class RuleOpenHab:
    label: str                  # Item-Label (Aufloesung: openHAB-Item-Cache)
    value: str                  # numerischer Wert (Einheit wird ignoriert)
    raw: str = ""


@dataclass
class RuleCamera:
    name: str                   # Kamera-Name (Config CAMERAS oder M3U-Index)
    raw: str = ""


# ---------------------------------------------------------------------------
# Parser (rein)
# ---------------------------------------------------------------------------

_ALARM_VERBS = r"(?:stelle|stell|mach|mache|setze|setz|aktiviere|wecke|stelle\s+dir)"
_ALARM_DELETE = r"(?:loesche|loese|entferne|streich|streich\s+ihn|deaktiviere|schalte|stoppe|beende|tilge)"


def parse_alarm(text: str, now: time.struct_time | None = None) -> RuleAlarm | None:
    t = norm(text)
    if not t:
        return None
    # Loeschen: "loesche den Wecker [um 7:30]" / "alle Wecker (loeschen|aus)"
    if re.search(r"\b" + _ALARM_DELETE + r"\b", t) and re.search(r"\b(?:wecker|alarm)\b", t):
        if re.search(r"\balle\b", t):
            return RuleAlarm("delete_all", raw=t)
        c = parse_clock(t)
        return RuleAlarm("delete", clock=c, raw=t)
    if re.fullmatch(r"(?:wecker|alarm)\s+auss?", t) or re.fullmatch(
            r"(?:alle\s+)?wecker\s+(?:aus|stoppen|loeschen)", t):
        return RuleAlarm("delete_all" if "alle" in t else "delete", raw=t)
    # Setzen
    m = re.search(r"\b" + _ALARM_VERBS + r"\b.*\b(?:wecker|alarm)\b", t)
    if not m and re.search(r"\bwecke\s+(?:mich|uns|dich|sie)\b", t):
        m = True
    if not m:
        m = re.search(r"\bset\s+(?:an?\s+|the\s+)?alarm\b", t)
    if not m:
        return None
    clock = parse_clock(t)
    # ohne Uhrzeit: Executor stellt Rueckfrage (keine None, sonst LLM)
    return RuleAlarm("set", clock=clock, day=parse_day(t, now), raw=t)


_TIMER_VERBS = r"(?:stelle|stell|starte|start|setze|setz|mach|mache|bitte)"


def parse_timer(text: str) -> RuleTimer | None:
    t = norm(text)
    if not t:
        return None
    # Stoppen: "Timer stoppen" / "alle Timer loeschen" / "Timer aus"
    if re.search(r"\b(?:timer|countdown|countdowns)\b", t) and re.search(
            r"\b(?:stoppen|stopp|abbrechen|loeschen|loese|beenden|beende|aus|enden|tilgen)\b", t):
        return RuleTimer("stop_all" if re.search(r"\balle\b", t) else "stop", raw=t)
    if re.fullmatch(r"(?:timer|countdown)\s*(?:anhalten|stoppen|stopp|beenden|aus)?", t) and t != "timer":
        return RuleTimer("stop", raw=t)
    # Setzen
    if not re.search(r"\b(?:timer|countdown)\b", t):
        if not re.search(r"\b(?:set|start)\s+(?:a\s+)?(?:timer|countdown)\b", t):
            return None
    m = re.search(
        r"(?:^|\b)(?:stelle|stell|starte|start|setze|setz|mach|mache|bitte)\s+"
        r"(?:einen|eine|den|der|mal\s+|a\s+|an\s+)?(?:timer|countdown)\s+"
        r"(?:auf|ueber|fuer|von|ab|for)?\s*(?P<dur>.+)$", t)
    if m:
        secs = parse_duration(m.group("dur"))
        if secs:
            return RuleTimer("set", seconds=secs, raw=t)
    # Kurzform "5 minuten timer" / "timer 5 minuten" / "10 minuten"
    if re.search(r"\b(?:timer|countdown)\b", t) or re.search(r"\bset\s+a\s+timer\b", t):
        secs = parse_duration(t)
        if secs and not re.search(r"\b(?:stoppen|stopp|loeschen|aus|abbrechen|beenden)\b", t):
            return RuleTimer("set", seconds=secs, raw=t)
    return None


_REMINDER_START = (
    r"\berinnere\s+(?:mich|uns|dich)\b|\berinnerung\s+an\b|\bdenk\s+(?:mal\s+)?dran\b")


def _strip_when(t: str) -> str:
    """Datum-/Uhrzeit-Woerter aus dem Erinnerungstext entfernen."""
    t = re.sub(r"\b(?:heute|morgen|uebermorgen)\b", " ", t)
    for w in _WEEKDAYS:
        t = re.sub(r"\b" + w + r"\b", " ", t)
    t = re.sub(r"\bin\s+\d{1,2}\s+tagen\b", " ", t)
    t = re.sub(r"\bnaechst\w*\b", " ", t)
    t = re.sub(r"\b\d{1,2}[:.]\d{1,2}\b", " ", t)
    t = re.sub(r"\b(\d{1,2}|\w+)\s+uhr(?:e|zeit)?\s*\d{1,2}?\b", " ", t)
    t = re.sub(r"\bhalb\s+\w+\b", " ", t)
    t = re.sub(r"\b(?:am|um|den|die|das|an|an\s+die|an\s+den|an\s+das)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip(" ,;")


def parse_reminder(text: str, now: time.struct_time | None = None) -> RuleReminder | None:
    t = norm(text)
    if not t:
        return None
    m = re.search(_REMINDER_START, t)
    if not m:
        return None
    rest = t[m.end():]
    clock = parse_clock(t)
    day = parse_day(t, now)
    if "erinnerung an" in t or "denk" in t or m.group(0).startswith("denk"):
        text_part = rest
    else:
        text_part = re.sub(r"^\s*an\s+", "", rest)
    text_part = _strip_when(text_part)
    if not text_part:
        return None
    if clock is None and day is not None:
        # nur Datum, keine Uhrzeit -> Rueckfrage im Executor
        pass
    return RuleReminder(text=text_part, clock=clock, day=day, raw=t)


def _split_target_text(rest: str) -> tuple[str, str]:
    """'Ziel: Text' / 'Ziel, Text' / 'Ziel dass Text' / 'Ziel Text' zerlegen.
    STT liefert die Sprechpause oft als Leerzeichen statt Doppelpunkt —
    das Ziel ist das erste (ggf. zweite) Wort, der Rest ist der Text."""
    rest = (rest or "").strip()
    if ":" in rest:
        target, txt = rest.split(":", 1)
    elif "," in rest:
        target, txt = rest.split(",", 1)
    elif re.search(r"\bdass\b", rest):
        target, txt = re.split(r"\bdass\b", rest, maxsplit=1)
    else:
        words = rest.split()
        # Ziel = erstes Wort (zweites mit dazu, wenns ein Artikel ist);
        # alles Weitere ist der Nachrichtentext.
        n = 2 if len(words) > 2 and words[0] in ("den", "die", "das", "an") else 1
        target, txt = " ".join(words[:n]), " ".join(words[n:])
    target = re.sub(r"^(?:den|die|das|an)\s+", "", target).strip()
    txt = txt.strip(" ,")
    return target, txt


def parse_message(text: str, voice: bool = False) -> RuleMessage | None:
    t = norm(text)
    if not t:
        return None
    if voice:
        kind_re = r"(?:eine\s+)?sprach\s*(?:nachricht|nachrichten)"
    else:
        # Lookbehinds: "sprachnachricht" haelt die Sprach- vom Textpfad trennend
        kind_re = (r"(?:eine\s+|einen\s+)?(?:text)?"
                   r"(?<!sprach)(?<!sprach\s)nachricht")
    m = re.search(
        r"\b(?:sende|schick|schreib|gib|uebertrag|ueberbringe|send|write)\s+"
        + kind_re + r"\s+an\s+(?P<rest>.+)$", t)
    if not m:
        return None
    target, text_part = _split_target_text(m.group("rest"))
    if not target:
        return None
    return RuleMessage(target=target, text=text_part, voice=voice, raw=t)


def parse_call(text: str) -> RuleCall | None:
    t = norm(text)
    if not t:
        return None
    m = re.match(r"^(?:rufe|ruf|ruft|call)\s+(?P<t>.+)$", t)
    if not m:
        m = re.search(r"\b(?:signalisiere|signalisieren)\s+(?P<t>.+)$", t)
    if not m:
        m = re.search(r"\b(?:spiele|spielt|bring)\s+(?:einen\s+)?signalton\s+"
                      r"(?:auf|an|bei|fuer)\s+(?P<t>.+)$", t)
    if not m:
        return None
    target = m.group("t").strip()
    target = re.sub(r"^(?:den|die|das|an)\s+", "", target)
    target = re.sub(r"\s+(?:gerade|mal|bitte)$", "", target)
    if len(target) < 2 or " " not in target and len(target) < 3:
        return None
    return RuleCall(target=target, raw=t)


# Einheiten, die am Wert hängen und IGNORIERT werden:
_UNIT_WORDS = {
    "grad", "grade", "celsius", "prozent", "prozente", "percent",
    "watt", "volt", "ampere", "kelvin", "lux", "bar", "psi",
    "kmh", "mph", "hertz", "ph", "ppm", "kilowatt", "megawatt",
    "wattstunden", "wattstunde", "kilowattstunden", "kilowattstunde",
    "minuten", "minute", "sekunden", "sekunde", "std", "stunden", "stunde",
    "drehzahl", "runden",
}


def parse_openhab_set(text: str) -> RuleOpenHab | None:
    """'setze <label> auf <zahl> [einheit]' -> (label, wert); Einheit ignoriert.

    Nur wenn eine der oben gelisteten Einheiten angehaengt ist oder gar
    keine Einheit; sonst None (anderer Pfad/LLM), damit 'setze X auf 5 min'
    keinen Timer fälschlich als openHAB-Wert uminterpretiert."""
    text = (text or "").replace("°C", " Grad").replace("°c", " Grad") \
                        .replace("°F", " Grad").replace("°", " Grad")
    t = norm(text)
    if not t:
        return None
    m = re.match(
        r"^(?:setze|stelle|regle|dimme|mach|mache)\s+"
        r"(?:das|die|den|der|einen|eine|meine|meinen|unsere|unser|den\s+)?"
        r"(?P<label>.+?)\s+auf\s+"
        r"(?P<val>\d+(?:[.,]\d+)?)\s*(?P<unit>[a-z]{2,12})?\s*$", t)
    if not m:
        return None
    unit = (m.group("unit") or "").strip()
    if unit and unit not in _UNIT_WORDS:
        return None
    val = m.group("val").replace(",", ".")
    label = m.group("label").strip()
    if not label:
        return None
    # Regel-Eigene als Label ausschliessen: "setze Timer auf 5 Minuten"
    # darf kein openHAB-Item werden.
    if label.lower() in {"timer", "countdown", "wecker", "alarm",
                         "nachricht", "kamera"}:
        return None
    return RuleOpenHab(label=label, value=val, raw=t)


_CAMERA_VERB = r"^(?:zeige|zeig|show)\s+(?:die\s+|mir\s+)?(?:kamera|camera)\s+(?P<name>.+)$"


def parse_camera(text: str) -> RuleCamera | None:
    t = norm(text)
    if not t:
        return None
    m = re.match(_CAMERA_VERB, t)
    if not m:
        m = re.search(r"\b(?:kamera|camera)\s+(?P<name>[a-z0-9 ]{2,32}?)\s+"
                      r"(?:los|status|bild|ansehen|zeigen)\b", t)
    if not m:
        return None
    name = m.group("name").strip()
    if not name:
        return None
    return RuleCamera(name=name, raw=t)



# ---------------------------------------------------------------------------
# Executor-Context: kapselt die Modus-Unterschiede (master | standalone).
# ---------------------------------------------------------------------------

class RuleCtx:
    """Schnittstelle, die der Executor braucht. Beide Bauformen (Pi, App)
    implementieren genau diese Methode; der Intent-Code wird dadurch in
    master- UND standalone-Modus identisch.

    Alle Methoden geben (ok: bool, Antwort: str) zurück.
    """
    mode: str = "standalone"          # "master" | "standalone"

    def now(self) -> time.struct_time:
        return time.localtime()

    # Zeiteinstellungen (beide Modi; Ziel wechselt mit dem Modus)
    def add_alarm(self, clock, day) -> tuple[bool, str]: ...
    def delete_alarm(self, clock=None, all_: bool = False) -> tuple[bool, str]: ...
    def add_timer(self, seconds: int) -> tuple[bool, str]: ...
    def stop_timer(self, all_: bool = False) -> tuple[bool, str]: ...
    def add_reminder(self, text: str, clock, day) -> tuple[bool, str]: ...

    # Zwischengeraete-Kommunikation (NUR master-Modus)
    def send_message(self, target: str, text: str) -> tuple[bool, str]: ...
    def send_voice(self, target: str, text: str) -> tuple[bool, str]: ...
    def call_target(self, target: str) -> tuple[bool, str]: ...

    # Direktaktionen (beide Modi, gerate-lokal)
    def openhab_set(self, label: str, value: str) -> tuple[bool, str]: ...
    def show_camera(self, name: str) -> tuple[bool, str]: ...


def _alarm_label(clock, day) -> str:
    lbl = "um " + fmt_clock(clock)
    d = fmt_day(day)
    if d:
        lbl += " " + d
    return lbl


def _master_down(ans: str) -> tuple[bool, str]:
    return (False, ans or "Der Master ist gerade nicht erreichbar.")


class SatCtx(RuleCtx):
    """Satelliten-Implementation (Pi). master-Modus = MASTER_URL gesetzt."""

    def __init__(self, pipeline=None):
        from . import config as cfg, http, schedule as sched
        self._cfg = cfg
        self._http = http
        self._sched = sched
        self.pipeline = pipeline
        self.mode = "master" if cfg.MASTER_URL else "standalone"

    # ── Helfer ───────────────────────────────────────────────────
    def _post(self, url: str, payload: dict, timeout: int = 10):
        return self._http.post_json(url, payload,
                                    token=self._cfg.MASTER_TOKEN or None,
                                    timeout=timeout)

    def _master_url(self, path: str) -> str:
        return f"{self._cfg.MASTER_URL.rstrip('/')}{path}"

    def _sched_url(self, tail: str = "") -> str:
        """Master-Route pfadgleich zu dm/call: mac im Pfad (Handler-Auth)."""
        return self._master_url(
            f"/api/satellite/{self._cfg.SATELLITE_MAC}/schedule{tail}")

    @staticmethod
    def _fire_epoch(clock, day, now: time.struct_time) -> float:
        t = resolve_fire_time(clock, day, now)
        return time.mktime(t)

    @staticmethod
    def _answer(data: dict) -> tuple[bool, str]:
        ok = bool(data.get("ok"))
        return (ok, str(data.get("answer") or ("") if ok
                        else (data.get("error") or "Fehler am Master.")))

    # ── Wecker ───────────────────────────────────────────────────
    def add_alarm(self, clock, day) -> tuple[bool, str]:
        now = time.localtime()
        fire = self._fire_epoch(clock, day, now)
        label = _alarm_label(clock, day)
        if self.mode == "master":
            try:
                data = self._post(self._sched_url(), {
                    "mac": self._cfg.SATELLITE_MAC, "kind": "alarm",
                    "fire_at": fire, "text": label})
            except Exception as e:
                log.warning("schedule/master: %s", e)
                return _master_down(str(e))
            ok, ans = self._answer(data)
            return (ok, ans or f"Wecker {label} gestellt.")
        item = self._sched.get_store().add("alarm", fire, label)
        return (True, f"Wecker {label} gestellt ({item['id']}).")

    def delete_alarm(self, clock=None, all_: bool = False) -> tuple[bool, str]:
        if self.mode == "master":
            payload = {"mac": self._cfg.SATELLITE_MAC, "kind": "alarm",
                       "all": bool(all_)}
            if clock is not None:
                payload["by_time"] = time.mktime(
                    time.struct_time(time.localtime()[:3] + clock + (0, 0, 0, -1)))
            try:
                data = self._post(self._sched_url("/delete"), payload)
            except Exception as e:
                return _master_down(str(e))
            ok, ans = self._answer(data)
            n = data.get("removed") if isinstance(data, dict) else None
            return (ok, ans or (f"{n} Wecker geloescht." if n else
                                "Wecker geloescht." if ok else
                                "Kein passender Wecker gefunden."))
        if clock is not None and not all_:
            # nur der Wecker mit passender Uhrzeit loeschen
            now = time.localtime()
            target = time.mktime(
                time.struct_time(now[:3] + clock + (0, 0, 0, -1)))
            n = self._sched.get_store().remove("alarm", by_time=target)
            if not n:
                return (False, "Kein Wecker zu dieser Uhrzeit gefunden.")
        else:
            n = self._sched.get_store().remove("alarm", all_=all_)
        if n:
            return (True, "Wecker geloescht." if n == 1
                    else f"{n} Wecker geloescht.")
        return (False, "Kein Wecker gefunden.")

    # ── Timer ────────────────────────────────────────────────────
    def add_timer(self, seconds: int) -> tuple[bool, str]:
        label = fmt_duration(seconds)
        fire = time.time() + seconds
        if self.mode == "master":
            try:
                data = self._post(self._sched_url(), {
                    "mac": self._cfg.SATELLITE_MAC, "kind": "timer",
                    "fire_at": fire, "text": label})
            except Exception as e:
                return _master_down(str(e))
            ok, ans = self._answer(data)
            return (ok, ans or f"Timer laeuft: {label}.")
        item = self._sched.get_store().add("timer", fire, label)
        return (True, f"Timer laeuft: {label}.")

    def stop_timer(self, all_: bool = False) -> tuple[bool, str]:
        if self.mode == "master":
            try:
                data = self._post(self._sched_url("/delete"), {
                    "mac": self._cfg.SATELLITE_MAC, "kind": "timer",
                    "all": bool(all_)})
            except Exception as e:
                return _master_down(str(e))
            ok, ans = self._answer(data)
            return (ok, ans or "Timer gestoppt.")
        n = self._sched.get_store().remove("timer", all_=all_)
        if n:
            return (True, "Timer gestoppt." if n == 1 else f"{n} Timer gestoppt.")
        return (False, "Kein laufender Timer.")

    # ── Erinnerung ───────────────────────────────────────────────
    def add_reminder(self, text: str, clock, day) -> tuple[bool, str]:
        now = time.localtime()
        fire = self._fire_epoch(clock, day, now)
        when = "um " + fmt_clock(clock) + (" " + fmt_day(day) if fmt_day(day) else "")
        if self.mode == "master":
            try:
                data = self._post(self._sched_url(), {
                    "mac": self._cfg.SATELLITE_MAC, "kind": "reminder",
                    "fire_at": fire, "text": text})
            except Exception as e:
                return _master_down(str(e))
            ok, ans = self._answer(data)
            return (ok, ans or f"Erinnerung {when}: {text}.")
        self._sched.get_store().add("reminder", fire, text)
        return (True, f"Erinnerung {when}: {text}.")

    # ── Zwischengeraete (master only) ────────────────────────────
    def _need_master(self) -> tuple[bool, str] | None:
        if self.mode == "master":
            return None
        return (False, "Das funktioniert nur im Master-Modus " \
                       "(Standalone-Geraet ohne Master).")

    def send_message(self, target: str, text: str) -> tuple[bool, str]:
        guard = self._need_master()
        if guard:
            return guard
        try:
            data = self._post(self._master_url(
                f"/api/satellite/{self._cfg.SATELLITE_MAC}/dm"),
                {"to": target, "text": text}, timeout=15)
        except Exception as e:
            return _master_down(str(e))
        if data.get("ok"):
            return (True, f"Nachricht an {target} gesendet.")
        return (False, str(data.get("error") or "Nachricht nicht zugestellt."))

    def send_voice(self, target: str, text: str) -> tuple[bool, str]:
        guard = self._need_master()
        if guard:
            return guard
        if not text:
            return (False, "Worum soll es in der Sprachnachricht gehen?")
        try:
            data = self._post(self._master_url(
                f"/api/satellite/{self._cfg.SATELLITE_MAC}/dm-voice"),
                {"to": target, "text": text}, timeout=60)
        except Exception as e:
            return _master_down(str(e))
        if data.get("ok"):
            note = " (ohne Sprache)" if data.get("text_only") else ""
            return (True, f"Sprachnachricht an {target} unterwegs{note}.")
        return (False, str(data.get("error") or "Sprachnachricht nicht zugestellt."))

    def call_target(self, target: str) -> tuple[bool, str]:
        guard = self._need_master()
        if guard:
            return guard
        try:
            data = self._post(self._master_url(
                f"/api/satellite/{self._cfg.SATELLITE_MAC}/call"),
                {"to": target}, timeout=15)
        except Exception as e:
            return _master_down(str(e))
        if data.get("ok"):
            return (True, f"Signalton an {target} (10 Sek.).")
        return (False, str(data.get("error") or "Ziel nicht gefunden."))

    # ── openHAB ──────────────────────────────────────────────────
    def openhab_set(self, label: str, value: str) -> tuple[bool, str]:
        from . import router
        best = None
        for item in router._cache.get():
            names = [item.get("label") or "", item.get("name") or ""]
            meta = item.get("metadata") or {}
            for a in str(meta.get("VoiceAlias", {}).get("value", "")).split(","):
                if a.strip():
                    names.append(a.strip())
            for nm in names:
                if nm and (router._normalize(nm) in router._normalize(label)
                           or router._normalize(label) in router._normalize(nm)):
                    if best is None or len(nm) > len(best[0]):
                        best = (nm, item)
        if best is None:
            return (False, f"openHAB-Item '{label}' nicht gefunden.")
        name = best[1].get("name") or ""
        if not name:
            return (False, "Item ohne Namen – nicht schaltbar.")
        url = router._smarthome_proxy_path(name)
        if not url:
            return (False, "openHAB ist nicht erreichbar (kein Master).")
        try:
            ok = self._http.post_raw(url, value,
                                     token=self._cfg.MASTER_TOKEN or None,
                                     timeout=10)
        except Exception as e:
            log.error("openHAB-Set %s=%s fehlgeschlagen: %s", name, value, e)
            return (False, "Bei der Verbindung zum Smarthome ist ein Fehler aufgetreten.")
        lbl = best[0]
        return (bool(ok), f"{lbl} auf {value}." if ok
                else f"{lbl}: Kommando fehlgeschlagen.")

    # ── Kamera ───────────────────────────────────────────────────
    def show_camera(self, name: str) -> tuple[bool, str]:
        from . import intent
        # 1. Konfigurierte Kamera (CAMERAS=Name=Snapshot-URL)
        handled, resp = intent.try_show_camera_config(name)
        if handled:
            return (True, resp or f"Kamera {name} wird angezeigt")
        # 2. Fallback: bestehender M3U-/Bridge-Pfad (gleicher Name)
        handled, resp = intent.try_show_camera("zeige Kamera " + name)
        if handled:
            return (True, resp or f"Kamera {name} wird angezeigt")
        return (False, f"Kamera '{name}' nicht gefunden " \
                       "(Config: CAMERAS, sonst Kamera/*.m3u).")


# ---------------------------------------------------------------------------
# Dispatcher: Text -> (gehandelt, Antwort) | None
# ---------------------------------------------------------------------------

_CALL_DONTS = {"mich", "dich", "uns", "sie", "ihnen", "hermann", "hermann an"}


def try_rule_intents(text: str, ctx: RuleCtx) -> tuple[bool, str] | None:
    """Regel-Intent-Stack. Gibt (True, Antwort) zurueck, wenn ein Befehl
    regelbasiert erledigt wurde; None, wenn es zu den Regel-Funktionen
    nicht passt (naechste Stufe: openHAB-Match/LLM)."""
    t = norm(text)
    if not t:
        return None

    # 1. Wecker
    a = parse_alarm(text, ctx.now())
    if a is not None:
        if a.action == "set":
            if a.clock is None:
                return (True, "Um welche Uhrzeit soll der Wecker klingeln?")
            ok, ans = ctx.add_alarm(a.clock, a.day)
            return (True, ans) if ok else (True, "Wecker ging nicht: " + ans)
        ok, ans = ctx.delete_alarm(clock=a.clock, all_=(a.action == "delete_all"))
        return (True, ans)

    # 2. Timer
    tm = parse_timer(text)
    if tm is not None:
        if tm.action == "set":
            if tm.seconds is None:
                return (True, "Wie lange soll der Timer laufen?")
            ok, ans = ctx.add_timer(tm.seconds)
            return (True, ans) if ok else (True, "Timer ging nicht: " + ans)
        ok, ans = ctx.stop_timer(all_=(tm.action == "stop_all"))
        return (True, ans)
    if re.search(r"\b(?:timer|countdown)\b", t) and \
            re.search(r"\b(?:stelle|stell|starte|start|setze|mach)\b", t) \
            and parse_duration(t) is None:
        return (True, "Wie lange soll der Timer laufen?")

    # 3. Erinnerung
    rem = parse_reminder(text, ctx.now())
    if rem is not None:
        if rem.clock is None:
            return (True, "Um wie viel Uhr soll ich dich erinnern?")
        ok, ans = ctx.add_reminder(rem.text, rem.clock, rem.day)
        return (True, ans) if ok else (True, "Erinnerung ging nicht: " + ans)

    # 4. Sprachnachricht (master only)
    vm = parse_message(text, voice=True)
    if vm is not None:
        ok, ans = ctx.send_voice(vm.target, vm.text)
        return (True, ans)

    # 5. Nachricht (master only)
    msg = parse_message(text, voice=False)
    if msg is not None:
        if not msg.text:
            return (True, f"Was soll die Nachricht an {msg.target} sagen?")
        ok, ans = ctx.send_message(msg.target, msg.text)
        return (True, ans)

    # 6. Signalton (master only)
    call = parse_call(text)
    if call is not None and call.target not in _CALL_DONTS:
        ok, ans = ctx.call_target(call.target)
        return (True, ans)

    # 7. openHAB-Wert mit Einheit
    oh = parse_openhab_set(text)
    if oh is not None:
        ok, ans = ctx.openhab_set(oh.label, oh.value)
        if ok:
            return (True, ans)
        return None  # Item nicht gefunden -> evtl. allgemeiner openHAB-Pfad

    # 8. Kamera (Config-Snapshot; Fallback: M3U-Index)
    cam = parse_camera(text)
    if cam is not None:
        ok, ans = ctx.show_camera(cam.name)
        if ok:
            return (True, ans)
        return None  # Kamera unbekannt -> M3U-Pfad im Intent-Stack probieren

    return None

