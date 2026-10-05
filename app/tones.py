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
    """WAV-Bytes für "wake" | "end" | "boot" (gecacht).

    "boot" ist ein zweistufiges Signal: zwei kurze Töne hintereinander
    (660 Hz -> 1320 Hz), damit der Start klar von den Einzeltönen
    unterscheidbar ist. "error" ist ein tiefer Warnton (220 Hz) für
    LLM-Ausfälle.
    """
    if kind not in _cache:
        if kind == "boot":
            pcm = (_sine(config.TONE_BOOT_HZ, config.TONE_SEC, config.SAMPLE_RATE, config.TONE_GAIN)
                   + _sine(config.TONE_BOOT2_HZ, config.TONE_SEC, config.SAMPLE_RATE, config.TONE_GAIN))
        elif kind == "error":
            pcm = _sine(config.TONE_ERROR_HZ, config.TONE_SEC, config.SAMPLE_RATE, config.TONE_GAIN)
        elif kind == "wake":
            pcm = _sine(config.TONE_WAKE_HZ, config.TONE_SEC, config.SAMPLE_RATE, config.TONE_GAIN)
        elif kind == "alarm":
            # Sirenen-Wechselton: hoch (880) und tief (440) im Wechsel,
            # ALARM_REPEATS Halbzyklen – lautes, eindeutig anderes Signal.
            step = config.TONE_SEC * 3
            pcm = b""
            for _ in range(config.TONE_ALARM_REPEATS):
                pcm += _sine(config.TONE_ALARM_HZ, step, config.SAMPLE_RATE, config.TONE_GAIN)
                pcm += _sine(config.TONE_ALARM_HZ2, step, config.SAMPLE_RATE, config.TONE_GAIN)
        else:
            pcm = _sine(config.TONE_END_HZ, config.TONE_SEC, config.SAMPLE_RATE, config.TONE_GAIN)
        _cache[kind] = _wav(pcm, config.SAMPLE_RATE)
    return _cache[kind]


def play(kind):
    """Ton über aplay abspielen; Fehler werden verschluckt."""
    try:
        audio.play_wav(get(kind))
    except Exception:
        pass
