#!/usr/bin/env python3
"""Regelbasierte Intents (ohne LLM): Parser + Dispatcher.

Reine Parser-Tests (keine Hardware, kein Netzwerk):
  * Uhrzeit-/Datum-/Dauer-Parser (07:15, 7 Uhr, halb acht, morgen, 5 Minuten)
  * Intent-Parser: Wecker, Timer, Erinnerung, Nachricht, Sprachnachricht,
    Signalton, openHAB-Wert mit Einheit, Kamera
  * Dispatcher mit FakeCtx: jeder Befehl landet in der richtigen Aktion,
    Nicht-Regel-Texte (Lampen, Musik) bleiben None (keine Regression).

Aufruf:  python3 tests/test_ruleintents.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import ruleintents as R  # noqa: E402

fails = []


def ok(name, cond, detail=""):
    print(("  ok    " if cond else "  FAIL  ") + name + (f"  [{detail}]" if (detail and not cond) else ""))
    if not cond:
        fails.append(name)


# ── Uhrzeit ────────────────────────────────────────────────────
ok("clock 07:15", R.parse_clock("stelle wecker auf 07:15") == (7, 15))
ok("clock 7:15", R.parse_clock("wecker um 7:15") == (7, 15))
ok("clock 07.15 (Punkt)", R.parse_clock("wecker um 07.15") == (7, 15))
ok("clock 7 uhr", R.parse_clock("stelle wecker um 7 uhr") == (7, 0))
ok("clock sieben uhr", R.parse_clock("wecke mich um sieben uhr") == (7, 0))
ok("clock 7 uhr 15", R.parse_clock("wecker auf 7 uhr 15") == (7, 15))
ok("clock 7 uhr 5", R.parse_clock("wecker um 7 uhr 5") == (7, 5))
ok("clock halb acht", R.parse_clock("wecker um halb acht") == (7, 30))
ok("clock viertel acht", R.parse_clock("wecker um viertel acht") == (8, 15))
ok("clock viertel nach acht", R.parse_clock("wecker um viertel nach acht") == (8, 15))
ok("clock viertel vor acht", R.parse_clock("wecker um viertel vor acht") == (7, 45))
ok("clock dreiviertel acht", R.parse_clock("wecker um dreiviertel acht") == (7, 45))
ok("clock mitternacht", R.parse_clock("wecker um mitternacht") == (0, 0))
ok("clock EN 7 am", R.parse_clock("set an alarm for 7 am") == (7, 0))
ok("clock EN 5:30pm", R.parse_clock("alarm at 5:30pm") == (17, 30))
ok("clock Uhrzug prioritär vor Datum", R.parse_clock("erinnere mich am 12.03. um 9 uhr") == (9, 0))
ok("clock negativ", R.parse_clock("licht an") is None)

# ── Datum ──────────────────────────────────────────────────────
ok("day heute", R.parse_day("erinnere mich heute an X") == 0)
ok("day morgen", R.parse_day("wecker morgen um 7") == 1)
ok("day übermorgen", R.parse_day("wecker übermorgen um 7") == 2)
ok("day übermorgen (ae)", R.parse_day("wecker uebermorgen um 7") == 2)
ok("day Wochentag", R.parse_day("erinnerung am freitag") in range(0, 8))
ok("day in 3 tagen", R.parse_day("erinnerung in 3 tagen") == 3)
ok("day neutral", R.parse_day("licht an") is None)

# ── Dauer ──────────────────────────────────────────────────────
ok("dur 5 minuten", R.parse_duration("5 minuten") == 300)
ok("dur zehn sekunden", R.parse_duration("zehn sekunden") == 10)
ok("dur 2 stunden", R.parse_duration("2 stunden") == 7200)
ok("dur eine halbe stunde", R.parse_duration("eine halbe stunde") == 1800)
ok("dur EN 10 minutes", R.parse_duration("10 minutes") == 600)
ok("dur EN 2 hours", R.parse_duration("2 hours") == 7200)
ok("dur negativ", R.parse_duration("licht an") is None)

# ── Wecker ─────────────────────────────────────────────────────
a = R.parse_alarm("Stell einen Wecker auf 07:15")
ok("alarm set 07:15", a is not None and a.action == "set" and a.clock == (7, 15), repr(a))
a = R.parse_alarm("stell wecker um halb acht morgen")
ok("alarm set halb acht morgen", a is not None and a.action == "set" and a.clock == (7, 30) and a.day == 1, repr(a))
a = R.parse_alarm("Wecke mich um 7 Uhr")
ok("alarm wecke mich 7 uhr", a is not None and a.action == "set" and a.clock == (7, 0), repr(a))
a = R.parse_alarm("Set an alarm for 7 am")
ok("alarm EN", a is not None and a.action == "set" and a.clock == (7, 0), repr(a))
a = R.parse_alarm("lösche den Wecker")
ok("alarm delete", a is not None and a.action == "delete", repr(a))
a = R.parse_alarm("Lösche alle Wecker")
ok("alarm delete_all", a is not None and a.action == "delete_all", repr(a))
a = R.parse_alarm("Wecker aus")
ok("alarm aus", a is not None and a.action == "delete", repr(a))
a = R.parse_alarm("stelle Wecker")
ok("alarm ohne Uhrzeit (Rückfrage)", a is not None and a.action == "set" and a.clock is None, repr(a))
ok("alarm negativ (Licht)", R.parse_alarm("licht an") is None)
ok("alarm negativ (Timer-Text)", R.parse_alarm("timer 5 minuten") is None)

# ── Timer ──────────────────────────────────────────────────────
t = R.parse_timer("Stelle einen Timer auf 5 Minuten")
ok("timer set 5 min", t is not None and t.action == "set" and t.seconds == 300, repr(t))
t = R.parse_timer("Timer 10 Sekunden")
ok("timer Kurzform 10 s", t is not None and t.action == "set" and t.seconds == 10, repr(t))
t = R.parse_timer("Starte einen Timer über 2 Stunden")
ok("timer set 2 h", t is not None and t.action == "set" and t.seconds == 7200, repr(t))
t = R.parse_timer("set a timer for 10 minutes")
ok("timer EN", t is not None and t.action == "set" and t.seconds == 600, repr(t))
t = R.parse_timer("Timer stoppen")
ok("timer stop", t is not None and t.action == "stop", repr(t))
t = R.parse_timer("Alle Timer löschen")
ok("timer stop_all", t is not None and t.action == "stop_all", repr(t))
ok("timer negativ (Licht)", R.parse_timer("lampe an") is None)

# ── Erinnerung ─────────────────────────────────────────────────
r = R.parse_reminder("Erinnere mich morgen um 7:30 an die Abrechnung")
ok("reminder morgen 7:30", r is not None and r.clock == (7, 30) and r.day == 1 and r.text == "abrechnung", repr(r))
r = R.parse_reminder("erinnere mich um 9 uhr an die Medikamente nehmen")
ok("reminder heute 9 uhr", r is not None and r.clock == (9, 0) and "medikamente" in r.text, repr(r))
r = R.parse_reminder("Erinnerung an den Post um 18 Uhr")
ok("reminder Variante 2", r is not None and r.clock == (18, 0) and "post" in r.text, repr(r))
r = R.parse_reminder("erinnere mich an die Miete")
ok("reminder ohne Uhrzeit", r is not None and r.clock is None and r.text == "miete", repr(r))
ok("reminder negativ", R.parse_reminder("licht an") is None)

# ── Nachricht / Sprachnachricht / Signalton ────────────────────
m = R.parse_message("Sende eine Nachricht an Anna: Ich bin gleich da")
ok("msg Ziel+Text", m is not None and m.target == "anna" and m.text == "ich bin gleich da", repr(m))
m = R.parse_message("sende nachricht an antje, das auto steht offen")
ok("msg Komma", m is not None and m.target == "antje" and m.text == "das auto steht offen", repr(m))
m = R.parse_message("sende nachricht an Anna Ich bin gleich da")
ok("msg ohne Trenner", m is not None and m.target == "anna" and "gleich da" in m.text, repr(m))
m = R.parse_message("sende eine nachricht an Anna")
ok("msg ohne Text (Rückfrage)", m is not None and m.target == "anna" and m.text == "", repr(m))
m = R.parse_message("sende sprachnachricht an das Bad Fenster zu", voice=True)
ok("voice-msg", m is not None and m.voice and m.target == "bad" and m.text == "fenster zu", repr(m))
m = R.parse_message("licht an")
ok("msg negativ", m is None)
c = R.parse_call("Rufe die Küche")
ok("call", c is not None and c.target == "kueche", repr(c))
c = R.parse_call("ruf den wohnzimmer")
ok("call 2", c is not None and c.target == "wohnzimmer", repr(c))
ok("call negativ", R.parse_call("licht an") is None)

# ── openHAB-Wert mit Einheit ───────────────────────────────────
o = R.parse_openhab_set("Setze die Heizung auf 21 Grad")
ok("oh 21 grad", o is not None and o.value == "21", repr(o))
o = R.parse_openhab_set("setze heizung auf 21 °C")
ok("oh °C", o is not None and o.value == "21", repr(o))
o = R.parse_openhab_set("setze jalousie auf 75 Prozent")
ok("oh prozent", o is not None and o.value == "75", repr(o))
o = R.parse_openhab_set("setze dimmer auf 500")
ok("oh reine Zahl", o is not None and o.value == "500", repr(o))
o = R.parse_openhab_set("setze leistung auf 500 watt")
ok("oh watt ignoriert", o is not None and o.value == "500", repr(o))
o = R.parse_openhab_set("setze timer auf 5 minuten")
ok("oh Timer-Label ausgeschlossen", o is None, repr(o))
o = R.parse_openhab_set("licht an")
ok("oh negativ", o is None)

# ── Kamera ─────────────────────────────────────────────────────
k = R.parse_camera("Zeige Kamera Garten")
ok("kamera", k is not None and k.name == "garten", repr(k))
k = R.parse_camera("show camera front")
ok("kamera EN", k is not None and k.name == "front", repr(k))
ok("kamera negativ", R.parse_camera("licht an") is None)


# ── Dispatcher mit FakeCtx (master-Modus-Simulation) ──────────
class FakeCtx(R.RuleCtx):
    """Alle Aktionen protokollieren statt auszuführen."""

    def __init__(self):
        self.mode = "master"
        self.calls = []

    def add_alarm(self, clock, day):
        self.calls.append(("add_alarm", clock, day))
        return (True, f"Wecker um {R.fmt_clock(clock)} gestellt.")

    def delete_alarm(self, clock=None, all_=False):
        self.calls.append(("delete_alarm", clock, all_))
        return (True, "Wecker geloescht.")

    def add_timer(self, seconds):
        self.calls.append(("add_timer", seconds))
        return (True, f"Timer laeuft: {seconds} s.")

    def stop_timer(self, all_=False):
        self.calls.append(("stop_timer", all_))
        return (True, "Timer gestoppt.")

    def add_reminder(self, text, clock, day):
        self.calls.append(("add_reminder", text, clock, day))
        return (True, "Erinnerung gesetzt.")

    def send_message(self, target, text):
        self.calls.append(("send_message", target, text))
        return (True, f"Nachricht an {target} gesendet.")

    def send_voice(self, target, text):
        self.calls.append(("send_voice", target, text))
        return (True, f"Sprachnachricht an {target}.")

    def call_target(self, target):
        self.calls.append(("call", target))
        return (True, f"Signal an {target}.")

    def openhab_set(self, label, value):
        self.calls.append(("openhab", label, value))
        return (True, f"{label} auf {value}.")

    def show_camera(self, name):
        self.calls.append(("camera", name))
        return (True, f"Kamera {name} wird angezeigt.")


def disp(text):
    ctx = FakeCtx()
    out = R.try_rule_intents(text, ctx)
    return out, ctx


out, ctx = disp("Stell einen Wecker auf 07:15")
ok("disp wecker", out == (True, "Wecker um 07:15 gestellt.") and ctx.calls[0][0] == "add_alarm", repr(out))
out, ctx = disp("Stell einen Timer auf 5 Minuten")
ok("disp timer", out is not None and ctx.calls[0][0] == "add_timer" and ctx.calls[0][1] == 300, repr(out))
out, ctx = disp("Erinnere mich morgen um 7:30 an die Abrechnung")
ok("disp erinnerung", out is not None and ctx.calls[0][0] == "add_reminder", repr(out))
out, ctx = disp("Sende eine Nachricht an Anna: Ich bin gleich da")
ok("disp nachricht", out is not None and ctx.calls[0][0] == "send_message", repr(out))
out, ctx = disp("Sende Sprachnachricht an Küche: Abendessen ist fertig")
ok("disp sprachnachricht", out is not None and ctx.calls[0][0] == "send_voice", repr(out))
out, ctx = disp("Rufe die Küche")
ok("disp call", out is not None and ctx.calls[0][0] == "call", repr(out))
out, ctx = disp("Setze Heizung auf 21 Grad")
ok("disp openhab", out is not None and ctx.calls[0][0] == "openhab", repr(out))
out, ctx = disp("Zeige Kamera Garten")
ok("disp kamera", out is not None and ctx.calls[0][0] == "camera", repr(out))


# openHAB-Item nicht gefunden -> None (allgemeiner openHAB-Pfad/LLM)
class FailOpenHab(FakeCtx):
    def openhab_set(self, label, value):
        return (False, "nicht gefunden")


ctx2 = FailOpenHab()
out2 = R.try_rule_intents("Setze Heizung auf 21 Grad", ctx2)
ok("disp openhab Fallback (None)", out2 is None, repr(out2))

# Regression: bestehende Befehle bleiben unangetastet
for text in ["licht an", "Flur Lampe aus", "spiele Radio", "lauter",
             "musik aus", "playliste ab", "Lautstärke 50"]:
    out, ctx = disp(text)
    ok(f"disp neutral: {text!r}", out is None, repr(out))

print()
if fails:
    print(f"{len(fails)} FEHLGESCHLAGEN:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("ALL RULEINTENT TESTS PASSED")
