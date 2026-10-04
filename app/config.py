"""Konfiguration – alles per Umgebungsvariablen überschreibbar.

Werte ohne ENV-Default sind die Legacy-Werte aus dem Rhasspy-Setup.
Secrets gehören in eine .env-Datei (siehe .env.example), nicht in den Code.
"""
import os

def _load_dotenv(path):
    """Minimaler .env-Parser (KEIN python-dotenv): KEY=WERT, # Kommentare.
    Existing ENV gewinnt – compose/docker run -e bleibt maßgeblich."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass

_load_dotenv(os.environ.get("VOICE_ENV", "/app/.env"))
_load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


def _env(key, default):
    return os.environ.get(key, default)

# ── Audio ──────────────────────────────────────────────────────
MIC_DEVICE      = _env("MIC_DEVICE", "plughw:2,0")     # USB PCM2902; plughw konvertiert 44.1k->16k (virt_mic_16k rate-Plugin ist in ALSA 1.2.14 kaputt)
SPEAKER_DEVICE  = _env("SPEAKER_DEVICE", "plughw:1,0") # bcm2835 Headphones; plughw fuer Piper-22050-WAV
SAMPLE_RATE     = 16000
FRAME_SAMPLES   = 512          # = porcupine frame_length (32 ms)

# ── Wake ───────────────────────────────────────────────────────
WAKE_PPN        = _env("WAKE_PPN", "/app/wake/computer_raspberry-pi.ppn")
WAKE_SENS       = float(_env("WAKE_SENS", "0.5"))
WAKE_WORD       = _env("WAKE_WORD", "Computer")

# ── Aufnahme / VAD ────────────────────────────────────────────
VAD_MODE        = _env("VAD_MODE", "energy")           # energy | webrtc (webrtc nur x86)
VAD_AGGRESSIVEN = int(_env("VAD_AGGRESSIVENESS", "3")) # 0-3
ENERGY_THRESH   = float(_env("ENERGY_THRESHOLD", "1200"))
SILENCE_SEC     = float(_env("SILENCE_SEC", "1.3"))    # Ende der Sprache
MIN_REC_SEC     = float(_env("MIN_REC_SEC", "0.4"))
MAX_REC_SEC     = float(_env("MAX_REC_SEC", "12"))
PREROLL_SEC     = float(_env("PREROLL_SEC", "0.3"))    # Vorlauf, damit der Satzanfang nicht fehlt
WAKE_SKIP_SEC   = float(_env("WAKE_SKIP_SEC", "0.35")) # Rest des Codeworts verwerfen

# ── STT (Whisper auf .31) ─────────────────────────────────────
STT_URL         = _env("STT_URL", "http://192.168.1.31:9000/v1/audio/transcriptions")
STT_MODEL       = _env("STT_MODEL", "whisper-1")
STT_LANGUAGE    = _env("STT_LANGUAGE", "de")   # fest Deutsch vorgeben, sonst rät Whisper und liefert Englisch
STT_TIMEOUT     = float(_env("STT_TIMEOUT", "30"))

# ── openHAB Rules ─────────────────────────────────────────────
OPENHAB_URL     = _env("OPENHAB_URL", "http://openhab.example.net:8080/rest/items")
OPENHAB_TOKEN   = _env("OPENHAB_TOKEN", "")
RULES_FILE      = _env("RULES_FILE", "/data/rules.yaml")
CACHE_TTL       = float(_env("OPENHAB_CACHE_TTL", "300"))  # Item-Liste cachen (s)

# ── LLM-Fallback (Hermes .33:8642) ────────────────────────────
LLM_URL         = _env("LLM_URL", "http://192.168.1.33:8642/v1/chat/completions")
LLM_TOKEN       = _env("LLM_TOKEN", "")
LLM_MODEL       = _env("LLM_MODEL", "")
LLM_TIMEOUT     = float(_env("LLM_TIMEOUT", "120"))  # Hermes-Gateway (.33:8642) braucht teils 70-90s
SYSTEM_PROMPT   = _env(
    "SYSTEM_PROMPT",
    "Du bist ein hilfsbereiter Sprachassistent im Wohnzimmer. "
    "Antworte auf Deutsch, per Du, kurz und ohne Emojis. "
    "Maximal 3 Sätze – deine Antwort wird vorgelesen.",
)

# ── Gedächtnis ─────────────────────────────────────────────────
MEMORY_DB       = _env("MEMORY_DB", "/data/memory.db")
MAX_HISTORY     = int(_env("MAX_HISTORY", "20"))       # Nachrichten im Kurzzeitgedächtnis
MAX_FACTS       = int(_env("MAX_FACTS", "100"))
MEMORY_EXTRACT_PROMPT = (
    "Extrahiere aus diesem Gespräch maximal 3 wichtige Fakten über den Nutzer "
    "oder explizite Wünsche/Vorlieben die du dir merken solltest. "
    'Antworte NUR mit einer JSON-Liste von Strings, z.B.: '
    '["Nutzer heisst Thomas", "Nutzer mag Kaffee"]. '
    "Wenn es nichts Wichtiges zu merken gibt, antworte mit: []\n"
    "Gespräch:\nNutzer: {user}\nDu: {assistant}"
)

# ── TTS (Piper auf .33:5000) ──────────────────────────────────
TTS_URL         = _env("TTS_URL", "http://192.168.1.33:5000/v1/audio/speech")
TTS_TOKEN       = _env("TTS_TOKEN", "")
TTS_MODEL       = _env("TTS_MODEL", "jjhome")
TTS_VOICE       = _env("TTS_VOICE", "jjhome")
TTS_TIMEOUT     = float(_env("TTS_TIMEOUT", "60"))

# ── Web ────────────────────────────────────────────────────────
WEB_PORT        = int(_env("WEB_PORT", "8770"))
LOG_LEVEL       = _env("LOG_LEVEL", "INFO")
