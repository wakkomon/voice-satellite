"""Klingel-Töne als akustisches Feedback.

- "wake": Wake-Word erkannt – Aufnahme beginnt
- "end":  Ende der Spracheingabe erkannt – Befehl wird verarbeitet

Reine Sinus-Synthese (Stdlib), ein 10-ms-Rampen-Fade gegen Klickgeräusche.
Die WAVs werden einmal erzeugt und gecacht. play() wirft nie – Feedback
darf die Pipeline nicht umbringen.
"""
import io
import math
import struct
import wave

from . import audio, config

_cache = {}


def _sine(freq, seconds, rate, gain):
    n = max(1, int(rate * seconds))
    fade = max(1, int(rate * 0.010))  # 10 ms ein/ausblenden
    out = bytearray()
    for i in range(n):
        amp = gain
        if i < fade:
            amp *= i / fade
        elif i > n - fade:
            amp *= (n - i) / fade
        s = int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))
        out += struct.pack("h", max(-32768, min(32767, s)))
    return bytes(out)


def _wav(pcm, rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def get(kind):
    """WAV-Bytes für "wake" | "end" (gecacht)."""
    if kind not in _cache:
        if kind == "wake":
            freq = config.TONE_WAKE_HZ
        else:
            freq = config.TONE_END_HZ
        pcm = _sine(freq, config.TONE_SEC, config.SAMPLE_RATE, config.TONE_GAIN)
        _cache[kind] = _wav(pcm, config.SAMPLE_RATE)
    return _cache[kind]


def play(kind):
    """Ton über aplay abspielen; Fehler werden verschluckt."""
    try:
        audio.play_wav(get(kind))
    except Exception:
        pass
