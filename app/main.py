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
import contextlib
import logging
import os
import re
import signal
import sys
import time

from . import audio, config, display, duck, memory, playback, router, stt, tones, tts, wake, web

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
        playback.player.set_on_event(self._on_music_event)
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
        self._music_held = False    # Pipeline haelt Geraet (Aufnahme/Töne/TTS)
        self._duck_mine = False     # diese Session hat das Ducking gestartet
        self._stop_flag = False     # Thread-safe STOP (von Web-UI/Debug)
        # Idle-Display-Alternierung: weather <-> music info
        self._idle_at = 0.0           # Zeitstempel: letzte idle-Aktion
        self._idle_mode = "none"      # "weather" | "music" | "none"

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
        display.show_text("Bereit", f"Codewort: {config.WAKE_WORD}")

    def stop_pipeline(self):
        """STOP: Pipeline sofort unterbrechen und auf 'wake' zurücksetzen."""
        self._stop_flag = True
        self.state = "wake"
        self._to_wake()
        self.ev("STOP – Pipeline zurückgesetzt")

    def manual_listen(self):
        """MANUAL LISTEN: Starte manuelle Aufnahme vom Web-UI."""
        if self.state != "wake":
            return False, "Gerät gerade belegt"
        self.state = "recording"
        self.ev("Manuelle Aufnahme gestartet")
        try:
            self._acquire_device()
            self._play_tone("wake")
            pcm = self.rec.capture_followup(config.FOLLOWUP_SEC)
            if pcm is None:
                return False, "Kein Audio erkannt"
            self._respond(pcm)
            return True, "Aufnahme verarbeitet"
        except Exception as e:
            log.exception("Manuelle Aufnahme fehlgeschlagen")
            return False, str(e)
        finally:
            if self.state != "wake":
                self._to_wake()
            self._release_device()

    def drain_mic(self):
        """Vom Web-Audiothread aufgerufen (nach /speak, /tone): markiert den
        Mic-Stream zum Leeren. Der Wake-Loop-Thread vollzieht den restart()
        beim naechsten durchlauf – am arecord sitzt immer nur EIN Leser.
        Ohne Leeren hoert der Satellit sein eigenes Web-TTS und Whisper
        haengt es beim naechsten Wake an die gesprochene Frage an."""
        self._mic_restart_requested = True

    def duck_music(self):
        """Musik leiser (nicht stoppen): Lautstaere auf DUCK_VOLUME senken,
        solange die Unterhaltung laeuft – TTS spricht ueber die leise
        Musik, am Session-Ende stellt exit_() den alten Wert wieder her.
        Gibt True, wenn dieser Aufruf das Ducking GESTARTET hat (und es
        am Ende zuruecksetzen MUSS) – verschachtelte Halter (Konversation
        + Web-TTS) ducken sonst nicht doppelt."""
        if duck.enter():
            self.ev("Musik leiser (Ducking)")
            return True
        return False

    def unduck_music(self, mine=False):
        """Gegenteil von duck_music(): stellt die gemerkte Lautstaere
        wieder her – nur, wenn genau dieser Halter das Ducking
        gestartet hat (mine=True)."""
        if mine:
            duck.exit_()

    def _play_tone(self, name):
        """Kontrollton über die Pause: Ducking senkt die Systemlautstärke
        auf DUCK_VOLUME (30 %), dort wären die Töne (Tone-Gain 0.5)
        praktisch unhörbar. Deshalb während des Tons die gemerkte
        Pre-Duck-Lautstärke, danach zurück aufs Duck-Level – exakt wie
        TTS in speak(). Nur wirksam, wenn Ducking aktiv ist."""
        duck.restore_for_tts()
        try:
            tones.play(name)
        finally:
            duck.duck_again()

    # ── Idle-Display-Alternierung ─────────────────────────────
    def tick_idle(self):
        """Im Wake-Word-Loop aufgerufen: wechselt zwischen
        Weather-Frame und Musik-Info-Overlay."""
        if self.state != "wake":
            return
        now = time.time()
        if now - self._idle_at < config.IDLE_TICK_SEC:
            return
        self._idle_at = now

        # Prüfe ob Musik läuft
        status = playback.player.status()
        playing = bool(status.get("playing"))

        if playing:
            # Musik-Info anzeigen (Overlay ueber Weather-Hintergrund)
            title = status.get("title") or "Musik"
            display.show_music_info("Musik", title)
            self._idle_mode = "music"
            self.ev(f"Idle: Musik-Info – {title}")
        elif config.IDLE_SHOW_MUSIC:
            # Weather-Frame anzeigen (nicht loggen: smärt das Log)
            display.show_weather()
            self._idle_mode = "weather"
        else:
            # Nur Text-Display
            display.show_text("Bereit", f"Codewort: {config.WAKE_WORD}")
            self._idle_mode = "none"

    def _on_music_event(self, msg):
        """Musik-Player-Events (aus dem Playback-Thread) in den Feed +
        Display bringen: Trackwechsel/Stop erscheinen live, ohne dass der
        Wake-Loop-Thread beteiligt ist."""
        self.ev(msg)
        if msg.startswith("Laueft:") or msg.startswith("Radio:"):
            title = msg.split(":", 1)[1].strip()
            display.show_music_info("Musik", title)
        elif msg.startswith("Wiedergabe beendet"):
            display.reset_music_info()
            display.show_text("Musik", "Beendet")
        elif msg.startswith("Wiedergabe fehlgeschlagen"):
            display.reset_music_info()
            display.show_text("Musik", "Fehler")
        elif msg.startswith("Musik gestoppt"):
            display.reset_music_info()
            display.show_text("Musik", "Gestoppt")

    # ── Zyklus nach dem Codewort ──────────────────────────────
    def conversation(self):
        """Ein kompletter Frage-Antwort-Zyklus inkl. optionaler
        Follow-up-Session. Drei Wiedergabe-Fenster (bcm2835 ohne dmix):
        1. Mic-Lesefenster (Wake-Ton, capture, Follow-up): Geraet wird
           ueber den Hold-Counter freigegeben – Duck-Musik waere sonst
           im Mic zu hoeren und der VAD muesste auf MAX_REC_SEC laufen.
        2. STT + LLM (Thread blockiert, Mic nicht mehr gelesen): Hold
           wird gelöst, Musik läuft auf dem Duck-Level weiter (keine
           tote Stille mehr).
        3. Töne/TTS: Hold erneut, TTS spricht im Normalpegel (Pre-Duck-
           Lautstärke) über die Pause, danach zurück auf Duck-Level.
        Am Session-Ende: Loudness wiederhergestellt (inkl. Mute-Status)
        und Geraet dauerhaft freigegeben – die Musik setzt an der
        gemerkten Position ein und läuft im alten Pegel weiter.
        Stellt am Ende sicher, dass der Zustand zurück auf 'wake' geht –
        sonst wird der Satellit taub."""
        playback.player.pause()
        self._music_held = True
        self._duck_mine = self.duck_music()
        self.state = "recording"
        self.stats["wakes"] += 1
        self._play_tone("wake")     # hohes Signal: Codewort erkannt, Aufnahme an
        try:
            pcm = self.rec.capture()
            if self._respond(pcm) is True:
                if config.FOLLOWUP:
                    self._followup()
        finally:
            if self.state != "wake":
                self._to_wake()
            self.unduck_music(mine=self._duck_mine)
            self._release_device()   # no-op, wenn speak() schon gelöst hat

    def _release_device(self):
        """Hold lösen: Musik läuft auf dem Duck-Level weiter (STT/LLM-
        Fenster – Geraet ist frei, VAD ist inaktiv)."""
        if self._music_held:
            self._music_held = False
            playback.player.resume()

    def _acquire_device(self):
        """Hold erneuten: Geraet für Töne/TTS freibekommen (Duck-Musik
        bleibt leise stehen, bis resume() sie an der Position weiter-
        laufen lässt)."""
        if not self._music_held:
            self._music_held = True
            playback.player.pause()

    def _respond(self, pcm):
        """Audio -> STT -> Router -> TTS -> Display.
        True, wenn eine Antwort gesprochen wurde (Follow-up offen);
        "ack" bei kurzen Abschlüssen ("Danke") – Session zu, kein
        Follow-up, direkt zurück zum Wake-Word; sonst False."""
        if pcm is None:
            self._play_tone("error")  # tiefes Signal: keine Sprache erkannt
            self._fail("Keine Sprache erkannt")
            return False
        dur = len(pcm) / 2 / config.SAMPLE_RATE
        self.ev(f"{dur:.1f}s Audio aufgenommen")
        self._acquire_device()      # Ton "end" erklingt ueber die Pause
        self._play_tone("end")   # tiefes Signal: Sprachende, wird verarbeitet
        self._release_device()   # STT+LLM: Musik läuft leise weiter
        text = stt.transcribe(audio.to_wav(pcm))
        if not text:
            self._acquire_device()
            self._play_tone("error")  # tiefes Signal: STT hat nichts erkannt
            self._fail("STT leer oder nicht erreichbar")
            return False
        log.info("STT erkannt: %s", text)
        self.ev(f"STT: {text}")
        if _is_ack(text):
            # "Danke" & aehnliches: Abschlusston, Session zu – nicht
            # weiter zuhoeren, direkt auf Wake-Word warten.
            log.info("Abschluss: %r – zurück zum Wake-Word", text)
            self.ev(f"Abschluss: {text[:40]} – zurück zum Wake-Word")
            self._acquire_device()
            self._play_tone("end")
            return "ack"
        self._release_device()   # no-op – ist vor dem STT schon gelöst
        self.state = "processing"
        display.show_text("Frage", text)
        t0 = time.time()
        answer, kind, llm_ok = router.handle(text)
        self.stats["commands" if kind == "smarthome" else "llm"] += 1
        dt = time.time() - t0
        log.info("Antwort (%s, %.1fs): %s", kind, dt, answer[:160])
        self.ev(f"{kind} {dt:.1f}s -> {answer}")
        if not llm_ok:
            self._acquire_device()   # Fehlerton erklingt ueber die Pause
            self._play_tone("error")  # tiefes Signal: LLM-Ausfall/leere Antwort
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
            self._acquire_device()   # Ton + Mic-Lesen: Gerät halten
            self.state = "followup"
            self.ev("Follow-up: lausche auf Folgefrage")
            display.show_text("Hört zu", "")
            self._play_tone("wake")   # Signal: jetzt bitte weitersprechen
            self.mic.restart()          # TON-ECHO aus dem Mic-Stream verwerfen
            self.vad.reset()
            pcm = self.rec.capture_followup(config.FOLLOWUP_SEC)
            if pcm is None:
                self.ev(f"Follow-up: {config.FOLLOWUP_SEC:.0f} s Stille – "
                        "Codewort wieder fällig")
                self._play_tone("end")  # tiefes Signal: ich hör nicht mehr zu
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
        self._play_tone("end")          # tiefes Signal: Follow-up-Pause zu Ende

    def speak(self, answer):
        """TTS-Antwort im Normalpegel: erst Geraet freibekommen (Duck-
        Musik leise stehen lassen), dann die gemerkte Pre-Duck-Laut-
        staere zuruecksetzen, damit die Antwort laut ueber die Pause
        erklingt. Nach dem Sprechen zurueck auf Duck-Level und das
        Geraet wieder freigeben, damit die Musik beim naechsten
        Hold-Release leise an der gemerkten Position weiterlaeuft.
        Mic-ECHO wird erst NACH dem fertigen Sprechen verworfen –
        die Pipe waehrend der Wiedergabe fuellt sich mit der eigenen
        TTS-Stimme, und die Follow-up-Aufnahme lasst sie sofort als
        neue Sprache erscheinen."""
        self._acquire_device()
        wav = tts.synthesize(answer)
        if not wav:
            self._fail("TTS lieferte kein Audio")
            self._release_device()
            return
        self.state = "speaking"
        duck.restore_for_tts()       # TTS im Normalpegel (Pre-Duck)
        try:
            audio.play_wav(wav)
        finally:
            # Mic-ECHO verwerfen: Stream leeren, VAD zuruecksetzen.
            self.mic.restart()
            self.vad.reset()         # Geräuschuntergrenze neu lernen
            self.state = "wake"
            duck.duck_again()        # zurück auf Duck-Level
            self._release_device()   # Gerät frei, Musik leise weiter

    # ── Hauptloop ─────────────────────────────────────────────
    def run(self):
        log.info("Voice-Satellite bereit – Codewort: %r (Frame %d Bytes)",
                 config.WAKE_WORD, self.frame_bytes)
        self.ev("gestartet")
        self._play_tone("boot")   # Startton: Satellit ist online
        display.init()              # /dev/fb1, falls vorhanden
        try:
            while True:
                # STOP-Check: sofortiger Ausstieg aus Warteschleife
                if self._stop_flag:
                    self._stop_flag = False
                    self._to_wake()
                    continue
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
                self.tick_idle()     # Idle-Display: Wetter / Musik-Info
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
