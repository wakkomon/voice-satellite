"""Voice-Satellite-Hauptloop.

Wake('Computer') -> Aufnahme+VAD -> Whisper-STT -> Router (openHAB | LLM) ->
Piper-TTS -> aplay. Ein Thread, ein arecord-Leser (wake und Recorder lesen
dieselben Frames nacheinander, nie parallel). Ersetzt Rhasspy + Supervisor +
MQTT-Bus als Prozessrahmen.

Zwei Erweiterungen laufen hier zusammen:
* Follow-up-Listening – nach einer Antwort wird ohne erneutes Codewort
  direkt weitergelauscht; erst FOLLOWUP_SEC (3 s) Stille ohne Audio-Input
  beendet die Session.
* Framebuffer-Display – Frage und Antwort erscheinen als Text auf /dev/fb1
  (app/display.py); ohne Device bleibt alles ein No-Op.
"""
import logging
import os
import re
import signal
import sys
import time

from . import audio, config, display, memory, router, stt, tones, tts, wake, web

log = logging.getLogger("satellite")

# Kurze Abschluesse: danach ist das Gespräech zu – kein Follow-up-
# Listening, direkt zurück zum Wake-Word.
_ACK_WORDS = {"danke", "danke dir", "danke schön", "danke schoen",
              "ok", "okay", "okey", "alles klar", "gut", "sehr gut",
              "genau", "super", "perfekt"}


def _is_ack(text):
    """'Danke' & aehnliches als gesprochene Eingabe erkennen (komplett
    normalisiert, ohne Satzzeichen). Nur exakte Kurz-Abschluesse –
    'Danke, mach noch X' ist KEIN Abschluss."""
    t = re.sub(r"[^\wäöüß]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", t).strip() in _ACK_WORDS


class Pipeline:
    def __init__(self):
        self.wake = wake.WakeWord(config.WAKE_PPN, config.WAKE_SENS)
        self.frame_bytes = self.wake.frame_length * 2
        self.mic = audio.MicStream(self.frame_bytes)
        self.vad = audio.make_vad()
        self.rec = audio.Recorder(self.mic, self.vad, self.frame_bytes)
        self.state = "wake"           # wake | recording | processing | speaking | followup
        self.web_audio_busy = False   # Web-TTS/Ton läuft -> Wake-Loop taub stellen
        self._mic_restart_requested = False
        self.events = []              # Ring-Puffer für /status & SSE-Feed
        self.seq = 0                  # monotone Event-Nummer (SSE-Reconnect)
        self.stats = {"wakes": 0, "commands": 0, "llm": 0, "errors": 0,
                      "followups": 0, "started": time.time()}
        self._last_wake = 0.0

    def ev(self, msg):
        self.seq += 1
        self.events.append({"ts": time.strftime("%H:%M:%S"), "seq": self.seq,
                            "msg": msg})
        del self.events[:-60]

    def _fail(self, msg):
        self.stats["errors"] += 1
        self.ev(msg)
        log.warning(msg)

    def _to_wake(self):
        """Zurück in die Wake-Word-Bereitschaft. Stale Frames aus
        STT/LLM/TTS-Laufzeit verwerfen, sonst feuert Porcupine/VAD auf dem
        Echo der eigenen Ausgabe."""
        self.mic.restart()
        self.vad.reset()
        self.state = "wake"

    def drain_mic(self):
        """Vom Web-Audiothread aufgerufen (nach /speak, /tone): markiert den
        Mic-Stream zum Leeren. Der Wake-Loop-Thread vollzieht den restart()
        beim naechsten durchlauf – am arecord sitzt immer nur EIN Leser.
        Ohne Leeren hoert der Satellit sein eigenes Web-TTS und Whisper
        haengt es beim naechsten Wake an die gesprochene Frage an."""
        self._mic_restart_requested = True

    # ── Zyklus nach dem Codewort ──────────────────────────────
    def conversation(self):
        """Ein kompletter Frage-Antwort-Zyklus inkl. optionaler
        Follow-up-Session. Stellt am Ende sicher, dass der Zustand zurück
        auf 'wake' geht – sonst wird der Satellit taub."""
        self.state = "recording"
        self.stats["wakes"] += 1
        tones.play("wake")          # hohes Signal: Codewort erkannt, Aufnahme an
        try:
            pcm = self.rec.capture()
            if self._respond(pcm) is True:
                if config.FOLLOWUP:
                    self._followup()
        finally:
            if self.state != "wake":
                self._to_wake()

    def _respond(self, pcm):
        """Audio -> STT -> Router -> TTS -> Display.
        True, wenn eine Antwort gesprochen wurde (Follow-up offen);
        "ack" bei kurzen Abschlüssen ("Danke") – Session zu, kein
        Follow-up, direkt zurück zum Wake-Word; sonst False."""
        if pcm is None:
            tones.play("error")     # tiefes Signal: keine Sprache erkannt
            self._fail("Keine Sprache erkannt")
            return False
        dur = len(pcm) / 2 / config.SAMPLE_RATE
        self.ev(f"{dur:.1f}s Audio aufgenommen")
        tones.play("end")       # tiefes Signal: Sprachende, wird verarbeitet
        text = stt.transcribe(audio.to_wav(pcm))
        if not text:
            tones.play("error")     # tiefes Signal: STT hat nichts erkannt
            self._fail("STT leer oder nicht erreichbar")
            return False
        log.info("STT erkannt: %s", text)
        self.ev(f"STT: {text[:80]}")
        if _is_ack(text):
            # "Danke" & aehnliches: Abschlusston, Session zu – nicht
            # weiter zuhören, direkt auf Wake-Word warten.
            log.info("Abschluss: %r – zurück zum Wake-Word", text)
            self.ev(f"Abschluss: {text[:40]} – zurück zum Wake-Word")
            tones.play("end")
            return "ack"
        self.state = "processing"
        display.show_text("Frage", text)
        t0 = time.time()
        answer, kind, llm_ok = router.handle(text)
        self.stats["commands" if kind == "smarthome" else "llm"] += 1
        dt = time.time() - t0
        log.info("Antwort (%s, %.1fs): %s", kind, dt, answer[:160])
        self.ev(f"{kind} {dt:.1f}s -> {answer}")
        if not llm_ok:
            tones.play("error")   # tiefes Signal: LLM-Ausfall/leere Antwort
            self._fail("LLM-Fehler – Fehlerton abgespielt")
        display.show_text("Antwort", answer)
        self.speak(answer)
        return True                 # Antwort wurde gesprochen -> Follow-up offen

    def _followup(self):
        """Direkt weitersprechen, ohne Codewort. Vor jeder Folgeaufnahme
        erklingt der "ich höre zu"-Ton (wie beim Wake-Word) – damit man
        hören kann, wann wieder gelauscht wird. Der Ton-Echo wird danach aus
        dem Mic-Stream gelehrt und der VAD zurückgesetzt, damit der Ton die
        Folgeaufnahme nicht selbst triggert. Erst wenn FOLLOWUP_SEC Sekunden
        (Default: 3) kein Audio-Eingang kommt, zurück in die
        Wake-Word-Bereitschaft."""
        rounds = 0
        while rounds < config.FOLLOWUP_MAX:
            self.state = "followup"
            self.ev("Follow-up: lausche auf Folgefrage")
            display.show_text("Hört zu", "")
            tones.play("wake")          # Signal: jetzt bitte weitersprechen
            self.mic.restart()          # TON-ECHO aus dem Mic-Stream verwerfen
            self.vad.reset()
            pcm = self.rec.capture_followup(config.FOLLOWUP_SEC)
            if pcm is None:
                self.ev(f"Follow-up: {config.FOLLOWUP_SEC:.0f} s Stille – "
                        "Codewort wieder fällig")
                tones.play("end")       # tiefes Signal: ich hör nicht mehr zu
                return
            rounds += 1
            self.stats["followups"] += 1
            self.state = "recording"
            if self._respond(pcm) == "ack":
                # "Danke" & aehnliches: Session zu – kein "ich hoere zu"-
                # Ton, kein erneutes Lauschen, zurück zum Wake-Word.
                self.ev("Abschluss erkannt – Session beendet")
                return
        self.ev(f"Follow-up: nach {rounds} Runden beendet")
        tones.play("end")               # tiefes Signal: Follow-up-Pause zu Ende

    def speak(self, answer):
        wav = tts.synthesize(answer)
        if not wav:
            self._fail("TTS lieferte kein Audio")
            return
        self.state = "speaking"
        try:
            audio.play_wav(wav)
        finally:
            # Mic-ECHO erst NACH dem fertigen Sprechen verwerfen.
            # Bisher lag mic.restart() VOR play_wav: die Pipe füllte sich
            # während der Wiedergabe mit der eigenen TTS-Stimme, und die
            # Follow-up-Aufnahme las sie sofort als neue Sprache – der Agent
            # hörte sich selbst und antwortete zu früh. Erst wenn play_wav()
            # zurück ist (Lautsprecher still), Stream leeren und lauschen.
            self.mic.restart()
            self.vad.reset()         # Geräuschuntergrenze neu lernen
            self.state = "wake"

    # ── Hauptloop ─────────────────────────────────────────────
    def run(self):
        log.info("Voice-Satellite bereit – Codewort: %r (Frame %d Bytes)",
                 config.WAKE_WORD, self.frame_bytes)
        self.ev("gestartet")
        tones.play("boot")          # Startton: Satellit ist online
        display.init()              # /dev/fb1, falls vorhanden
        try:
            while True:
                if self._mic_restart_requested:
                    # Vom Web-Audiothread gesetzt (drain_mic) NACH dessen
                    # Wiedergabe: arecord neu starten und die Pipe leeren.
                    # Der Restart passiert immer hier im Pipeline-Thread –
                    # am arecord sitzt genau EIN Leser (Audio-Modul-Doku).
                    self._mic_restart_requested = False
                    self.mic.restart()
                    self.vad.reset()
                frame = self.mic.read()
                if frame is None:
                    log.warning("arecord beendet – Neustart in 2s")
                    time.sleep(2)
                    self.mic.restart()
                    continue
                if self.state != "wake" or self.web_audio_busy:
                    # Web-TTS/Ton aus dem Lautsprecher waehrend Wiedergabe:
                    # Frames verwerfen, sonst feuert Porcupine/VAD auf dem
                    # eigenen Echo und Whisper haengt es an die naechste Frage
                    continue
                if (self.wake.feed(frame)
                        and time.time() - self._last_wake >= 2.0):
                    self._last_wake = time.time()
                    log.info("WAKE – %s erkannt", config.WAKE_WORD)
                    self.ev("Codewort erkannt")
                    display.show_text("Hört zu", "")
                    try:
                        self.conversation()
                    except Exception as e:
                        log.exception("Zyklus-Fehler")
                        self._fail(f"Fehler: {e}")
                        self.state = "wake"
        finally:
            display.shutdown()


def _setup_logging():
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main():
    _setup_logging()
    os.makedirs(os.path.dirname(config.MEMORY_DB) or ".", exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    p = Pipeline()
    web.start(p)                    # Debug-/Health-HTTP im Hintergrundthread
    router.warm_async()             # LLM-Modell im Hintergrund vorwärmen
    p.run()


if __name__ == "__main__":
    main()
