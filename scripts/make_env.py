#!/usr/bin/env python3
"""Erzeugt/ergänzt /home/pi/voice-satellite/.env aus den alten Rhasspy-Skripten.
Laeuft AUF dem Pi; Secrets bleiben dort und werden nur maskiert ausgegeben.

MERGE-Verhalten (wichtig): Ein erneutes Deploy/Provision setzt die ENV-
Variablen NICHT mehr zurück.
  * Bestehende .env-Werte behalten Vorrang und werden NIE überschrieben.
  * Nur noch fehlende Keys werden aus dem Seed-Dict unten ergänzt.
  * Alle zusätzlichen, manuell gepflegten Keys der .env bleiben erhalten.
"""
import pathlib
import re
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


# Seed-Werte: werden NUR angelegt, wenn der Key in der .env noch fehlt.
# Sind alte Defaults — eine laufende .env mit korrekten Werten gewinnt.
seed = {
    "OPENHAB_TOKEN": grab("openhab_switch.py", "OPENHAB_TOKEN"),
    "LLM_TOKEN": grab("ollama.py", "TOKEN"),
    "LLM_MODEL": "",                       # Hermes-Gateway braucht keinen Modelnamen
    "TTS_TOKEN": grab("tts_piper.py", "API_KEY"),
    "TTS_MODEL": "piper",
    "TTS_VOICE": grab("tts_piper.py", "VOICE"),
    "TTS_LANGUAGE": "de",
    # plughw konvertiert softwareseitig (virt_mic_16k/hw sind auf trixie armv7l
    # kaputt: Container-ALSA 1.2.14, rate-Plugin 'Unknown field rate'):
    "MIC_DEVICE": "plughw:2,0",
    "SPEAKER_DEVICE": "plughw:1,0",
    "WAKE_SENS": "0.5",
    "VAD_MODE": "energy",
    "SILENCE_SEC": "1.3",
    "LOG_LEVEL": "INFO",
}


# Bestehende .env einlesen — alle Werte + Reihenfolge bleiben unverändert.
existing_lines = []
existing_keys = set()
if DEST.exists():
    raw = DEST.read_text(encoding="utf-8", errors="replace")
    for line in raw.splitlines():
        existing_lines.append(line)
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.partition("=")[0].strip()
            if k:
                existing_keys.add(k)

# Nur fehlende Seed-Keys ergänzen (Werte NIE überschreiben, nichts entfernen).
new_lines = list(existing_lines)
added = []
for k, v in seed.items():
    if k not in existing_keys:
        new_lines.append("%s=%s" % (k, v))
        added.append(k)

DEST.parent.mkdir(parents=True, exist_ok=True)
DEST.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
DEST.chmod(stat.S_IRUSR | stat.S_IWUSR)

total = len(existing_keys) + len(added)
for k in (existing_keys | set(added)):
    marker = "  (neu ergänzt)" if k in added else ""
    print("%-15s <set>%s" % (k, marker))
print("\nfertig: %d Keys in .env, davon %d neu ergänzt, %d bestehende UNVERÄNDERT"
      % (total, len(added), len(existing_keys)))
