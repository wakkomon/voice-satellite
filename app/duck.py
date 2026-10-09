"""Musik-Ducking: bei Sprachausgabe nur die Lautstaere runter, nicht den
Stream stoppen.

Der bcm2835-Karten-Player kann TTS und Musik nicht parallel auf demselben
Gerät abspielen (kein dmix), deshalb pausiert der Player weiterhin physisch –
aber die LAUTSTAERE bleibt während der ganzen Unterhaltung auf dem Duck-Level:
Wake-Word -> Musik leiser (config.DUCK_VOLUME), TTS spricht über die Pause,
Musik setzt leise an, Session-Ende -> vorherige Lautstärke wird wiederher-
gestellt.

enter()/exit_() sind idempotent und thread-sicher: verschachtelte Halter
(Konversation + Web-TTS) ducken nur ein Mal, und wer das Ducking begonnen
hat, stellt am Ende die gemerkte Lautstärke wieder her.
"""
import logging
import threading

from . import config, volume

log = logging.getLogger("duck")

_lock = threading.Lock()
_active = False
_prev = None


def enter():
    """Lautstärke auf das Duck-Level senken. Gibt True, wenn DIESES Aufruf
    das Ducking gestartet hat (und es am Ende wieder rückgängig machen
    muss), sonst False (schon aktiv, nichts zu ducken oder deaktiviert)."""
    global _active, _prev
    with _lock:
        if _active:
            return False
        try:
            st = volume.get()
            if st is None or not config.DUCK_ENABLED:
                return False
            prev_vol, prev_muted = st
            if prev_muted or prev_vol == config.DUCK_VOLUME:
                return False
            volume.set_volume(config.DUCK_VOLUME, muted=prev_muted)
            _prev = (prev_vol, prev_muted)
            _active = True
            log.info("Ducking: Musik von %d%% auf %d%%",
                     prev_vol, config.DUCK_VOLUME)
            return True
        except Exception as e:
            log.warning("Ducking nicht möglich: %s", e)
            return False


def exit_():
    """Gegenteil von enter(): stellt die gemerkte Lautstärke wieder her
    (nur, wenn dieser Aufruf das Ducking gestartet hat)."""
    global _active, _prev
    with _lock:
        if not _active or _prev is None:
            return
        vol, muted = _prev
        try:
            volume.set_volume(vol, muted=muted)
            log.info("Ducking zurück: Musik wieder auf %d%%", vol)
        except Exception as e:
            log.warning("Duck-Zurücksetzung fehlgeschlagen: %s", e)
        finally:
            _active = False
            _prev = None


def restore_for_tts():
    """Temporäre Lautstärke-Rücksetzung für die TTS-Antwort: stellt die
    gemerkte Pre-Duck-Lautstärke wieder her, damit die Sprachausgabe im
    Normalpegel erklingt und nicht auf dem leisen Duck-Level.
    Nur wirksam, wenn Ducking aktiv ist; ändert _active/_prev nicht."""
    with _lock:
        if not _active or _prev is None:
            return
        vol, muted = _prev
        try:
            volume.set_volume(vol, muted=muted)
            log.info("TTS: Lautstärke zurück auf %d%%", vol)
        except Exception as e:
            log.warning("TTS-Volumen nicht möglich: %s", e)


def duck_again():
    """Nach TTS: zurück auf das Duck-Level, damit die Musik beim nächsten
    Hold-Release (resume) leise weiterläuft.
    Nur wirksam, wenn Ducking aktiv ist."""
    with _lock:
        if not _active:
            return
        try:
            volume.set_volume(config.DUCK_VOLUME, muted=False)
            log.info("TTS fertig: zurück auf Duck-Level %d%%",
                     config.DUCK_VOLUME)
        except Exception as e:
            log.warning("Duck-Zurück nicht möglich: %s", e)


class ducked:
    """Kontext: `with ducked():` duckt beim Betreten und stellt beim
    Verlassen die gemerkte Lautstärke wieder her. Nesting-sicher: nur der
    äusserte Block stellt wieder her (enter() liefert sonst False)."""

    def __init__(self):
        self._mine = False

    def __enter__(self):
        self._mine = enter()
        return self

    def __exit__(self, *exc):
        if self._mine:
            exit_()
        return False
