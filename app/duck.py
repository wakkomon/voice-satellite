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
_offset = 10  # Duck-Abstand zur Nutzerstufe (bei Start: prev - DUCK_VOLUME)


def _duck_level(user_pct):
    """Duck-Level relativ zur gemerkten Nutzerstufe: immer _offset Prozent-
    punkte darunter (Start: 40%% Nutzer -> 30%% Duck bei DUCK_VOLUME=30).
    Das erhaelt die Relation, wenn der Nutzer waehrend des Duckings
    'lauter'/'leiser' sagt; duck_again() wuerde sonst auf den festen
    DUCK_VOLUME-Wert zurueckfallen und die Nutzer-Stufe verschlucken."""
    return max(0, user_pct - _offset)


def enter():
    """Lautstärke auf das Duck-Level senken. Gibt True, wenn DIESES Aufruf
    das Ducking gestartet hat (und es am Ende wieder rückgängig machen
    muss), sonst False (schon aktiv, nichts zu ducken oder deaktiviert)."""
    global _active, _prev, _offset
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
            _offset = max(1, prev_vol - config.DUCK_VOLUME)
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
        _active = False
        _prev = None
    try:
        volume.set_volume(vol, muted=muted)
        log.info("Ducking zurück: Musik wieder auf %d%%", vol)
    except Exception as e:
        log.warning("Duck-Zurücksetzung fehlgeschlagen: %s", e)


def active():
    """True, waehrend Ducking aktiv ist (Nutzerbasis = _prev, nicht der
    gerade geltende Duck-Level)."""
    with _lock:
        return _active and _prev is not None


def effective_volume():
    """Fuer Anzeigen/Status: die Nutzer-Lautstaere (Pre-Duck), nicht der
    Duck-Level. Ausserhalb des Duckings die echte Hardware-Lautstaere."""
    with _lock:
        if _active and _prev is not None:
            return _prev[0], _prev[1]
    return volume.get()


def adjust_user_volume(delta):
    """'lauter'/'leiser' WAHREND des Duckings: der Schritt gilt auf der
    gemerkten Nutzer-Grundlage (_prev), nicht auf dem Duck-Level —
    sonst wuerde der Wert beim naechsten duck_again()/exit_() wieder
    weggebuegelt. Duck-Level = neue Nutzerstufe - Duck-Abstand (Kopplung
    wie beim normalen Ducking). Gibt (neu, alt) auf Nutzerbasis zurueck
    oder None, wenn kein Ducking aktiv ist (Aufrufer faellt auf
    volume.step() zurueck)."""
    global _prev
    with _lock:
        if not _active or _prev is None:
            return None
        old_vol, old_muted = _prev
        new = max(0, min(100, old_vol + int(delta)))
        _prev = (new, old_muted)
        duck_level = _duck_level(new)
    volume.set_volume(duck_level, muted=old_muted)
    log.info("Ducking: Nutzer-Lautstaere von %d%% auf %d%% "
             "(Duck-Level %d%%)", old_vol, new, duck_level)
    return new, (old_vol, old_muted)


def set_user_volume(pct):
    """'Lautstaerke auf X' WAHREND des Duckings: setzt die gemerkte
    Nutzer-Grundlage und leitet den Duck-Level neu ab (X - Duck-Abstand).
    Gibt (alt_pct, neu_pct) zurueck oder None ohne aktives Ducking."""
    global _prev
    with _lock:
        if not _active or _prev is None:
            return None
        old_vol, old_muted = _prev
        _prev = (int(pct), old_muted)
        duck_level = _duck_level(int(pct))
    volume.set_volume(duck_level, muted=old_muted)
    log.info("Ducking: Nutzer-Lautstaere auf %d%% gesetzt (Duck-Level %d%%)",
             int(pct), duck_level)
    return old_vol, int(pct)


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
    Hold-Release (resume) leise weiterläuft. Das Level leitet sich von der
    (ggf. während der Session geänderten) Nutzerstufe ab, nicht vom
    festen DUCK_VOLUME.
    Nur wirksam, wenn Ducking aktiv ist."""
    with _lock:
        if not _active or _prev is None:
            return
        level = _duck_level(_prev[0])
        prev_muted = _prev[1]
    try:
        volume.set_volume(level, muted=prev_muted)
        log.info("TTS fertig: zurück auf Duck-Level %d%%", level)
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
