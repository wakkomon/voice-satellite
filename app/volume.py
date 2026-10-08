"""Lautstaere ueber ALSA (amixer) – beide Audio-Karten synchron.

Card 0: Pi-Codec (PCM), Card 1: bcm2835-Headphones (PCM) – die tatsaechliche
Ausgabekarte liegt in SPEAKER_DEVICE (z.B. plughw:1,0). Beide werden gesetzt,
damit Tons und Musik zusammenfallen; die aktuelle Anzeige kommt von der
Speaker-Karte. Kein alsa-lib-Python: amixer-CLI, stdout-Parsing.
"""
import logging
import re
import subprocess

from . import config

log = logging.getLogger("volume")

MUTE_RE = re.compile(r"\[(\d+)%\](?:\s*\[-?[\d.]+dB\])?\s*(\[(on|off)\])?")


def _parse(amix_out):
    """Amixer-Status -> (prozent, gemuetet) oder None.
    echtes Format:  'Mono: Playback -3536 [63%] [-35.36dB] [off]'
    das [off] steht NACH dem dB-Teil; ohne [off] = nicht gemuetet."""
    m = MUTE_RE.search(amix_out or "")
    if not m:
        return None
    return int(m.group(1)), m.group(3) == "off"


def _card_of(device):
    """plughw:1,0 -> 1, plughw:1 -> 1, hw:0,0 -> 0, sonst 0."""
    m = re.search(r"[:/](\d+)", device or "")
    return int(m.group(1)) if m else 0


def _cards():
    """Alle Karten, die amixer kennen (dedup, sortiert) – min. Speaker-Karte."""
    cards = []
    for c in range(0, 4):
        r = subprocess.run(["amixer", "-c", str(c), "sget", "PCM"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            cards.append(c)
    if _card_of(config.SPEAKER_DEVICE) not in cards:
        cards.append(_card_of(config.SPEAKER_DEVICE))
    return sorted(set(cards))


def get():
    """Aktuelle Lautstaere: (prozent, stumm) oder None wenn unbekannt."""
    card = _card_of(config.SPEAKER_DEVICE)
    r = subprocess.run(["amixer", "-c", str(card), "sget", "PCM"],
                       capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        return None
    p = _parse(r.stdout)
    if p is None:
        return None
    vol, muted = p
    return vol, muted


def set_volume(pct=None, muted=None):
    """Lautstaere 0..100 auf allen Karten setzen; optional Mute-Status.
    pct=None: Lautstaere nicht anfassen (nur Mute). muted=None: nicht anfassen.
    Mindestens eines muss gesetzt sein. Gibt die neue (prozent, stumm) zurueck."""
    if pct is not None:
        try:
            pct = int(pct)
        except (TypeError, ValueError):
            raise ValueError("Lautstaere muss eine Zahl 0-100 sein")
        if not 0 <= pct <= 100:
            raise ValueError("Lautstaere muss 0-100 sein")
    if pct is None and muted is None:
        raise ValueError("volume oder muted fehlt")
    for c in _cards():
        if pct is not None:
            a = ["amixer", "-q", "-c", str(c), "sset", "PCM", f"{pct}%"]
        else:
            a = ["amixer", "-q", "-c", str(c), "sset", "PCM"]
        if muted is True:
            a.append("mute")
        elif muted is False:
            a.append("unmute")
        elif pct is not None:
            # Neue Lautstärke bedeutet "das soll hörbar sein" -> entmuten.
            a.append("unmute")
        subprocess.run(a, capture_output=True, timeout=5)
    st = get()
    if st is None:
        if pct is None:
            raise RuntimeError("Lautstaere nicht ermittelbar")
        return (pct, bool(muted))
    vol, mute = st
    if muted is not None:
        mute = bool(muted)
    return (vol, mute)


def step(delta):
    """Lautstaere um delta Prozent schalten (klammert 0..100). Gibt (neu, alt)."""
    cur = get()
    if cur is None:
        raise RuntimeError("Lautstaere nicht ermittelbar")
    vol, mute = cur
    if mute:
        vol = 50  # nach Mute-Release sinnvoller Startpunkt
    new = max(0, min(100, vol + int(delta)))
    set_volume(new, muted=False if mute else None)
    return new, (vol, mute)
