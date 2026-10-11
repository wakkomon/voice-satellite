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
# .env liegt neben dem Repo-Ordner (funktioniert auf dem Pi UND lokal):
DEST = pathlib.Path(__file__).resolve().parent.parent / ".env"

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
# Defaults = Live-Werte des Pi (Stand 2026-10-11), alphabetisch sortiert.
# Secrets (TOKEN/PASS) werden NICHT geseedet – die .env bleibt dafür allein
# verantwortlich; beständige Werte aus der .env gewinnen immer (keine
# Überschreibung, kein Entfernen, nichts geht verloren).
seed = {
    "DEFAULT_IMAGE_URL": "http://192.168.1.33/weather/snap.jpg",
    "DUCK_ENABLED": "1",
    "DUCK_VOLUME": "30",
    "ENERGY_THRESHOLD": "1200",
    "FB_DEVICE": "/dev/fb1",
    "FB_FONT_SCALE": "4",
    "FB_HEIGHT": "320",
    "FB_PAGE_SEC": "5",
    "FB_TIMEOUT": "60",
    "FB_WIDTH": "480",
    "FOLLOWUP": "1",
    "FOLLOWUP_MAX": "10",
    "FOLLOWUP_SEC": "3.0",
    "IDLE_FRAME_DIR": "/data/idle",
    "IDLE_SHOW_MUSIC": "1",
    "IDLE_TICK_SEC": "30",
    "IDLE_TIMEOUT": "5",
    "LLM_ENABLE_THINKING": "false",
    "LLM_HAS_TOOLS": "auto",
    "LLM_MAX_TOKENS": "1024",
    "LLM_MODEL": "hermes-agent",
    # "LLM_TOKEN": – Secret, bleibt in der .env (kein Seed)
    "LLM_TIMEOUT": "180",
    "LLM_URL": "http://192.168.1.33:8642/v1/chat/completions",
    "LOG_LEVEL": "INFO",
    "MAX_FACTS": "100",
    "MAX_HISTORY": "20",
    "MAX_REC_SEC": "27",
    "MEMORY_DB": "/data/memory.db",
    "MIC_DEVICE": "plughw:2,0",
    "MIN_REC_SEC": "0.4",
    "MUSIC_ROOT": "/m",
    "OPENHAB_CACHE_TTL": "300",
    "OPENHAB_URL": "http://192.168.1.33:8080/rest/items",
    # "OPENHAB_TOKEN": – Secret, bleibt in der .env (kein Seed)
    "PREROLL_SEC": "0.3",
    "RULES_FILE": "/data/rules.yaml",
    "SILENCE_SEC": "1.3",
    "SPEAKER_DEVICE": "plughw:1,0",
    "STT_LANGUAGE": "de",
    "STT_MODEL": "whisper-1",
    "STT_TIMEOUT": "30",
    "STT_URL": "http://192.168.1.31:9000/v1/audio/transcriptions",
    "TONE_ALARM_HZ": "880",
    "TONE_ALARM_HZ2": "440",
    "TONE_ALARM_REPEATS": "3",
    "TONE_BLIP_SEC": "0.10",
    "TONE_BOOT2_HZ": "1320",
    "TONE_BOOT_HZ": "660",
    "TONE_END_HZ": "523",
    "TONE_ERROR_HZ": "220",
    "TONE_GAIN": "0.5",
    "TONE_SEC": "0.15",
    "TONE_WAKE_HZ": "880",
    "TTS_LANGUAGE": "German",
    "TTS_MODEL": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
    # "TTS_TOKEN": – Secret, bleibt in der .env (kein Seed)
    "TTS_TIMEOUT": "60",
    "TTS_URL": "http://192.168.1.31:7811/v1/audio/speech",
    "TTS_VOICE": "Uncle_fu",
    "VAD_AGGRESSIVENESS": "3",
    "VAD_MODE": "energy",
    "VOLUME_STEP": "10",
    "WAKE_PPN": "/app/wake/computer_raspberry-pi.ppn",
    "WAKE_SENS": "0.5",
    "WAKE_SKIP_SEC": "0.35",
    "WAKE_WORD": "Computer",
    "WEB_PORT": "8770",
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
