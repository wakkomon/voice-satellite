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


def _env(key, default, fallback_empty=False):
    """ENV-Wert holen. fallback_empty: leerer Wert gilt als 'nicht gesetzt'.
    Noetig, weil .env-Zeilen wie 'LLM_MODEL=' sonst mit leerem String siegen
    und z.B. Ollama mit model='' ablehnt."""
    v = os.environ.get(key, default)
    if fallback_empty and not (v or "").strip():
        return default
    return v

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

# ── LLM-Fallback (Qwen3.8-27B via LiteLLM .33:4000) ───────────
# .33:8642 (Hermes-Gateway) ist ein zustandsbehafteter Agent: haelt Aktionen
# zurueck ("Freigabe erforderlich") und halluziniert bei offenen Fragen –
# fuer einen Voice-Satelliten falsch. :4000 ist das reine 27B-Chat-Modell.
# Qwen3.8 ist ein Thinking-Modell: mit Thinking verbruet es ~1500 Tokens (26s)
# oder liefert leeres content. Mit enable_thinking=false antwortet es in 2-4s,
# diese vLLM-Variante legt die Antwort dann ABER in reasoning_content,
# content bleibt null -> router.ask_llm liest beide Felder.
LLM_ENABLE_THINKING = _env("LLM_ENABLE_THINKING", "false") == "true"
LLM_URL         = _env("LLM_URL", "http://192.168.1.33:4000/v1/chat/completions",
                       fallback_empty=True)
LLM_TOKEN       = _env("LLM_TOKEN", "")            # LiteLLM: sk-Key; Ollama: leer
LLM_MODEL       = _env("LLM_MODEL", "Qwen3.8-27B",  # 27B via LiteLLM; lokal: qwen2.5:3b
                       fallback_empty=True)
LLM_TIMEOUT     = float(_env("LLM_TIMEOUT", "60", fallback_empty=True))  # Kaltstart (Modell-Load) darf ~35s brauchen
LLM_MAX_TOKENS  = int(_env("LLM_MAX_TOKENS", "1024", fallback_empty=True))  # mit Thinking-aus reicht 1024 fuer Voice-Antworten
SYSTEM_PROMPT   = _env(
    "SYSTEM_PROMPT",
    "Du bist ein hilfsbereiter Sprachassistent im Wohnzimmer. "
    "Du hast keine Werkzeuge und kannst nichts im Haus schalten – "
    "erfinde niemals Aktionen, Fragen oder Freigaben, sondern antworte nur "
    "aus deinem Wissen. Antworte auf Deutsch, per Du, kurz und ohne Emojis. "
    "Maximal 3 Sätze – deine Antwort wird vorgelesen.",
)

# ── Gedächtnis ─────────────────────────────────────────────────
MEMORY_DB       = _env("MEMORY_DB", "/data/memory.db", fallback_empty=True)
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
TTS_MODEL       = _env("TTS_MODEL", "kitt")
TTS_VOICE       = _env("TTS_VOICE", "kitt")
TTS_TIMEOUT     = float(_env("TTS_TIMEOUT", "60"))

# ── Feedback-Töne ──────────────────────────────────────────────
TONE_GAIN     = float(_env("TONE_GAIN", "0.25"))     # Lautstärke 0..1
TONE_SEC      = float(_env("TONE_SEC", "0.15"))      # Tonlänge (s)
TONE_WAKE_HZ  = float(_env("TONE_WAKE_HZ", "880"))   # höher: Wake-Word erkannt
TONE_END_HZ   = float(_env("TONE_END_HZ", "523"))    # tiefer: Spracheingabe zu Ende
TONE_BOOT_HZ  = float(_env("TONE_BOOT_HZ", "660"))   # Boot-Ton 1: mittel (E5), beim Container-Start
TONE_BOOT2_HZ = float(_env("TONE_BOOT2_HZ", "1320")) # Boot-Ton 2: eine Oktave hoeher – zweifaches Signal
TONE_ERROR_HZ = float(_env("TONE_ERROR_HZ", "220"))  # Fehler:  tief  (A3), LLM-Ausfall/leere Antwort

# ── Web ────────────────────────────────────────────────────────
WEB_PORT        = int(_env("WEB_PORT", "8770"))
LOG_LEVEL       = _env("LOG_LEVEL", "INFO")
