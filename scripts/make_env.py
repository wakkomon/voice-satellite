#!/usr/bin/env python3
"""Erzeugt /home/pi/voice-satellite/.env aus den alten Rhasspy-Skripten.
Laeuft AUF dem Pi; Secrets bleiben dort und werden nur maskiert ausgegeben."""
import re
import pathlib
import stat

PROF = pathlib.Path("/home/pi/rhasspy/profiles/de")
DEST = pathlib.Path("/home/pi/voice-satellite/.env")

# Achtung: [\x27"] als Zeichenklasse, NICHT [^['"]]+ (Verschachtelung kaputt)
PATTERN = r"\b%s\s*=\s*[\x27\"]([^\x27\"]+)[\x27\"]"


def grab(fname, var):
    pat = re.compile(PATTERN % var)
    try:
        text = (PROF / fname).read_text(errors="replace")
    except OSError:
        return ""
    m = pat.search(text)
    return m.group(1) if m else ""


values = {
    "OPENHAB_TOKEN": grab("openhab_switch.py", "OPENHAB_TOKEN"),
    "LLM_TOKEN": grab("ollama.py", "TOKEN"),
    "LLM_MODEL": "",                       # Hermes-Gateway braucht keinen Modelnamen
    "TTS_TOKEN": grab("tts_piper.py", "API_KEY"),
    "TTS_MODEL": grab("tts_piper.py", "MODEL"),
    "TTS_VOICE": grab("tts_piper.py", "VOICE"),
    "MIC_DEVICE": "virt_mic_16k",
    "SPEAKER_DEVICE": "hw:1,0",
    "WAKE_SENS": "0.5",
    "VAD_MODE": "energy",
    "SILENCE_SEC": "1.3",
    "LOG_LEVEL": "INFO",
}

DEST.parent.mkdir(parents=True, exist_ok=True)
DEST.write_text("\n".join("%s=%s" % kv for kv in values.items()) + "\n", encoding="utf-8")
DEST.chmod(stat.S_IRUSR | stat.S_IWUSR)

for k, v in values.items():
    print("%-15s %s" % (k, ("<set len=%d>" % len(v)) if v else "<empty>"))
