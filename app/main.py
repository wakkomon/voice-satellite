"""Voice-Satellite-Hauptloop.

Wake('Computer') -> Aufnahme+VAD -> Whisper-STT -> Router (openHAB | LLM) ->
Piper-TTS -> aplay. Ein Thread, ein arecord-Leser (wake und Recorder lesen
dieselben Frames nacheinander, nie parallel). Ersetzt Rhasspy + Supervisor +
MQTT-Bus als Prozessrahmen.
"""
import logging
import os
import signal
import sys
import time

from . import audio, config, memory, router, stt, tones, tts, wake, web

log = logging.getLogger("satellite")


class Pipeline:
    def __init__(self):
        self.wake = wake.WakeWord(config.WAKE_PPN, config.WAKE_SENS)
        self.frame_bytes = self.wake.frame_length * 2
        self.mic = audio.MicStream(self.frame_bytes)
        self.vad = audio.make_vad()
        self.rec = audio.Recorder(self.mic, self.vad, self.frame_bytes)
        self.state = "wake"           # wake | recording | processing | speaking
        self.events = []              # Ring-Puffer für /status & SSE-Feed
        self.seq = 0                  # monotone Event-Nummer (SSE-Reconnect)
        self.stats = {"wakes": 0, "commands": 0, "llm": 0, "errors": 0,
                      "started": time.time()}
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

    # ── Zyklus nach dem Codewort ──────────────────────────────
    def conversation(self):
        """Ein kompletter Frage-Antwort-Zyklus. Stellt am Ende sicher, dass
        der Zustand zurück auf 'wake' geht – sonst wird der Satellit taub."""
        self.state = "recording"
        self.stats["wakes"] += 1
        tones.play("wake")          # hohes Signal: Codewort erkannt, Aufnahme an
        try:
            pcm = self.rec.capture()
            if pcm is None:
                self._fail("Keine Sprache erkannt")
                return
            dur = len(pcm) / 2 / config.SAMPLE_RATE
            self.ev(f"{dur:.1f}s Audio aufgenommen")
            tones.play("end")       # tiefes Signal: Sprachende, wird verarbeitet
            text = stt.transcribe(audio.to_wav(pcm))
            if not text:
                self._fail("STT leer oder nicht erreichbar")
                return
            log.info("STT erkannt: %s", text)
            self.ev(f"STT: {text[:80]}")
            self.state = "processing"
            t0 = time.time()
            answer, kind = router.handle(text)
            self.stats["commands" if kind == "smarthome" else "llm"] += 1
            dt = time.time() - t0
            log.info("Antwort (%s, %.1fs): %s", kind, dt, answer[:160])
            self.ev(f"{kind} {dt:.1f}s -> {answer[:80]}")
            self.speak(answer)
        finally:
            if self.state != "wake":
                # Stale Frames aus STT/LLM-Laufzeit verwerfen, sonst feuert
                # Porcupine gleich nochmal auf dem Echo der eigenen Aufnahme.
                self.mic.restart()
                self.vad.reset()
                self.state = "wake"

    def speak(self, answer):
        wav = tts.synthesize(answer)
        if not wav:
            self._fail("TTS lieferte kein Audio")
            return
        self.state = "speaking"
        self.mic.restart()          # nichts Aufgestautes (inkl. eigener Stimme) lesen
        try:
            audio.play_wav(wav)
        finally:
            self.vad.reset()         # Geräuschuntergrenze neu lernen
            self.state = "wake"

    # ── Hauptloop ─────────────────────────────────────────────
    def run(self):
        log.info("Voice-Satellite bereit – Codewort: %r (Frame %d Bytes)",
                 config.WAKE_WORD, self.frame_bytes)
        self.ev("gestartet")
        tones.play("boot")          # Startton: Satellit ist online
        while True:
            frame = self.mic.read()
            if frame is None:
                log.warning("arecord beendet – Neustart in 2s")
                time.sleep(2)
                self.mic.restart()
                continue
            if self.state != "wake":
                continue
            if (self.wake.feed(frame)
                    and time.time() - self._last_wake >= 2.0):
                self._last_wake = time.time()
                log.info("WAKE – %s erkannt", config.WAKE_WORD)
                self.ev("Codewort erkannt")
                try:
                    self.conversation()
                except Exception as e:
                    log.exception("Zyklus-Fehler")
                    self._fail(f"Fehler: {e}")
                    self.state = "wake"


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
