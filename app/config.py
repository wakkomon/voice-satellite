"""Konfiguration – alles per Umgebungsvariablen überschreibbar.

Werte ohne ENV-Default sind die Legacy-Werte aus dem Rhasspy-Setup.
Secrets gehören in eine .env-Datei (siehe .env.example), nicht in den Code.
"""
import json
import logging
import os
import sys
import time

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
SPEAKER_DEVICE  = _env("SPEAKER_DEVICE", "plughw:1,0") # bcm2835 Headphones; plughw als Fallback
SAMPLE_RATE     = 16000
FRAME_SAMPLES   = 512          # = porcupine frame_length (32 ms)

# ── Wake ───────────────────────────────────────────────────────
# WAKE_PPN: Pfad zur .ppn-Datei – alle 15 Raspberry-Pi-Wake-Words liegen
# unter /app/wake/ (computer, alexa, ...). Auswahl via ENV:
#   WAKE_PPN=/app/wake/bumble_bee_raspberry-pi.ppn
# WAKE_WORD ist nur Anzeige-Text (UI/Logs), kein technischer Bezug.
WAKE_PPN        = _env("WAKE_PPN", "/app/wake/computer_raspberry-pi.ppn")
WAKE_SENS       = float(_env("WAKE_SENS", "0.5"))
WAKE_WORD       = _env("WAKE_WORD", "Computer")

# ── Aufnahme / VAD ────────────────────────────────────────────
VAD_MODE        = _env("VAD_MODE", "energy")           # energy | webrtc (webrtc nur x86)
VAD_AGGRESSIVEN = int(_env("VAD_AGGRESSIVENESS", "3")) # 0-3
ENERGY_THRESH   = float(_env("ENERGY_THRESHOLD", "1200"))
SILENCE_SEC     = float(_env("SILENCE_SEC", "1.3"))    # Ende der Sprache
MIN_REC_SEC     = float(_env("MIN_REC_SEC", "0.4"))
MAX_REC_SEC     = float(_env("MAX_REC_SEC", "27"))
PREROLL_SEC     = float(_env("PREROLL_SEC", "0.3"))    # Vorlauf, damit der Satzanfang nicht fehlt
WAKE_SKIP_SEC   = float(_env("WAKE_SKIP_SEC", "0.15")) # Rest des Codeworts verwerfen (verkürzt für schnellere Reaktion)

# ── STT (Whisper auf .31) ─────────────────────────────────────
STT_URL         = _env("STT_URL", "")
STT_MODEL       = _env("STT_MODEL", "")
STT_LANGUAGE    = _env("STT_LANGUAGE", "de")   # fest Deutsch vorgeben, sonst rät Whisper und liefert Englisch
STT_TIMEOUT     = float(_env("STT_TIMEOUT", "30"))

# ── openHAB Rules ─────────────────────────────────────────────
OPENHAB_URL     = _env("OPENHAB_URL", "")
OPENHAB_TOKEN   = _env("OPENHAB_TOKEN", "")
RULES_FILE      = _env("RULES_FILE", "")
CACHE_TTL       = float(_env("OPENHAB_CACHE_TTL", "300"))  # Item-Liste cachen (s); numerisch -> Default muss bleiben (crashed sonst beim Start)

# ── LLM-Fallback (Hermes-Gateway-Agent .33:8642) ──────────────
# Live-Default: Hermes-Gateway (:8642, Modell hermes-agent) – ein echter
# Agent MIT Terminal. Reine Chat-Endpunkte (vLLM/Ollama) werden
# unterstuetzt: mit enable_thinking=false antworten sie schnell; die Antwort
# kann in content oder reasoning_content liegen -> router.ask_llm liest beide.
LLM_ENABLE_THINKING = _env("LLM_ENABLE_THINKING", "false") == "true"
LLM_URL         = _env("LLM_URL", "",
                       fallback_empty=True)
LLM_TOKEN       = _env("LLM_TOKEN", "")            # Gateway: Client-Secret; Ollama: leer
LLM_MODEL       = _env("LLM_MODEL", "",  # Hermes-Gateway-Agent
                       fallback_empty=True)
LLM_TIMEOUT     = float(_env("LLM_TIMEOUT", "180", fallback_empty=True))  # Kaltstart (Gateway-Agent braucht länger); numerisch -> Default muss bleiben (crashed sonst beim Start)
LLM_MAX_TOKENS  = int(_env("LLM_MAX_TOKENS", "1024", fallback_empty=True))  # mit Thinking-aus reicht 1024 fuer Voice-Antworten; numerisch -> Default muss bleiben
SYSTEM_PROMPT   = _env(
    "SYSTEM_PROMPT",
    "Du bist ein hilfsbereiter Sprachassistent im Wohnzimmer. "
    "Du hast keine Werkzeuge und kannst nichts im Haus schalten – "
    "erfinde niemals Aktionen, Fragen oder Freigaben, sondern antworte nur "
    "aus deinem Wissen. Antworte auf Deutsch, per Du, kurz und ohne Emojis. "
    "Maximal 3 Sätze – deine Antwort wird vorgelesen.",
)

# ── Agent-Werkzeuge (Hermes-Gateway) ──────────────────────────
# Das "keine Werkzeuge"-Systemprompt oben ist nur fuer das reine 27B-Chat-
# Modell sinnvoll (sonst halluziniert es Licht-Schaltaktionen). Am Hermes-
# Gateway (:8642, Modell hermes-agent) laeuft ein echter Agent MIT Terminal
# – dort wuerde derselbe Text ihn werkzeuglos machen: er raet dann z.B. die
# Uhrzeit statt sie per `date` zu ermitteln. Deshalb hier trennen.
LLM_HAS_TOOLS   = _env("LLM_HAS_TOOLS", "")  # true | false | auto

def llm_has_tools():
    v = str(LLM_HAS_TOOLS).strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    # auto: Gateway-URL oder Agent-Modell => Werkzeug-Modus
    return "8642" in LLM_URL or "hermes" in LLM_MODEL.lower()

AGENT_SYSTEM_PROMPT = _env(
    "AGENT_SYSTEM_PROMPT",
    "Du bist ein Sprachassistent im Wohnzimmer mit echten Werkzeugen "
    "(Terminal, Skripte, Suche). Bei Fragen nach Uhrzeit, Datum oder anderen "
    "flichtigen Fakten: fuehre IMMER zuerst das passende Kommando aus "
    "(z.B. `date`) und nenne das tatsaechliche Ergebnis – niemals raten oder "
    "aus dem Modellgedaechtnis antworten. Antworte auf Deutsch, per Du, kurz "
    "und ohne Emojis. Maximal 3 Saetze – deine Antwort wird vorgelesen.\n\n"
    "MUSIK: Wenn der Nutzer Musik oder Radio hoeren will ('spiele ...', 'spiel ...', "
    "'putz dir ... auf', 'mach Jazz an', Radio-Sender), suchst du die Quelle "
    "im Fileserver und spielst sie PER HTTP AN DEN PI AN – OHNE JEDWEDE BESTÄTIGUNG:\n"
    "  1. Mediathek: MP3-Ordner liegen unter /fileserver/mp3/<Genre>/<Künstler>/ "
    "(z.B. Jazz, Rock, Deutsch, Heavy Metal, Klassik). Mit `ls`/`find` den "
    "passenden Titel oder Ordner suchen – bei Genres/Alben eine einzelne "
    "Datei oder einen zufaellig gewaehlten Titel auswaehlen (z.B. mit `shuf`).\n"
    "  2. Feste Playlists (Dateien) liegen in /fileserver/mp3/, z.B. "
    "Carmix.m3u8, Anime.m3u8, Filmmusik.m3u8, Weihnachtsmusik.m3u8, "
    "trancedance.m3u8, Disco/pop-mixes – der Nutzer nennt sie mit Namen.\n"
    "  3. Radio: Sendernamen ueber die Radio-Browser-API auflösen, z.B. "
    "  curl -s 'https://all.api.radio-browser.info/json/stations/search?"
    "name=<Sender>&limit=1' | python3 -c \"import sys,json;print(json.load(sys.stdin)[0]['url_resolved'])\"\n"
    "  4. Wiedergabe: curl -s -X POST http://192.168.1.32:8770/play "
    "  -H 'Content-Type: application/json' "
    "  -d '{\"path\": \"mp3/<Genre>/<Titel>.mp3\"}'        (Mediathek, relativ zu /fileserver/mp3)\n"
    "  oder -d '{\"url\": \"<Stream-URL>\", \"title\": \"<Sender>\"}' (Radio)\n"
    "  Stoppen: curl -s -X POST http://192.168.1.32:8770/stop\n\n"
    "ABSOLUT REGEL FÜR MUSIK:\n"
    "  - Spiel IMMER SOFORT ab. KEINE Rückfrage. KEINE Bestätigung mit 'Soll ich...?'\n"
    "  - Es gibt KEINE App. Keine 'Freigabe in der App'. Dieses Konzept existiert nicht.\n"
    "  - Frag NIEMALS 'Soll ich spielen?', 'Möchtest du...', 'Freigabe nötig'\n"
    "  - Nach dem curl play: sag nur 'Klar, [Genre/Song] läuft.' und ENDE.\n"
    "  - Wenn der Pi nicht antwortet: sag 'Konnte den Pi nicht erreichen.' und ENDE.\n"
    "  - VERBOTEN: Warten, Bestätigen, Rückfragen, 'ich warte auf Freigabe'\n",
)

# ── Gedächtnis ─────────────────────────────────────────────────
MEMORY_DB       = _env("MEMORY_DB", "/data/memory.db", fallback_empty=True)  # sqlite-Pfad -> Default muss bleiben; kein Master-Override-Key
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

# ── TTS (Qwen3-TTS auf .31:7811) ──────────────────────────────
TTS_URL         = _env("TTS_URL", "", fallback_empty=True)
TTS_TOKEN       = _env("TTS_TOKEN", "")
TTS_MODEL       = _env("TTS_MODEL", "")
TTS_VOICE       = _env("TTS_VOICE", "")
# Sprache der Synthese (API-Feld "language"); leer = Server-Default.
TTS_LANGUAGE    = _env("TTS_LANGUAGE", "German")
TTS_TIMEOUT     = float(_env("TTS_TIMEOUT", "60"))

# ── Feedback-Töne ──────────────────────────────────────────────
TONE_GAIN     = float(_env("TONE_GAIN", "0.5"))      # Lautstärke 0..1
TONE_SEC      = float(_env("TONE_SEC", "0.15"))      # Tonlänge (s)
TONE_WAKE_HZ  = float(_env("TONE_WAKE_HZ", "880"))   # höher: Wake-Word erkannt
TONE_END_HZ   = float(_env("TONE_END_HZ", "523"))    # tiefer: Spracheingabe zu Ende
TONE_BOOT_HZ  = float(_env("TONE_BOOT_HZ", "660"))   # Boot-Ton 1: mittel (E5), beim Container-Start
TONE_BOOT2_HZ = float(_env("TONE_BOOT2_HZ", "1320")) # Boot-Ton 2: eine Oktave hoeher – zweifaches Signal
TONE_ERROR_HZ = float(_env("TONE_ERROR_HZ", "220"))  # Fehler:  tief  (A3), LLM-Ausfall/leere Antwort
TONE_ALARM_HZ = float(_env("TONE_ALARM_HZ", "880"))  # Alarmton: hoch (A5)
TONE_ALARM_HZ2 = float(_env("TONE_ALARM_HZ2", "440"))# Alarmton: tief (A4) – Wechselton
TONE_ALARM_REPEATS = int(_env("TONE_ALARM_REPEATS", "3"))  # Sirenen-Halbzyklen
TONE_BLIP_SEC = float(_env("TONE_BLIP_SEC", "0.10"))  # Kurz-Blip: Lautstärke-Feedback (Web-UI)

# ── Follow-up-Modus ────────────────────────────────────────────
# Nach einer Antwort ohne erneutes Wake-Word weitersprechen: Das Mikrofon
# lauscht direkt weiter, erst nach FOLLOWUP_SEC Stille ohne Spracheingabe
# gibt es auf und wartet wieder auf das Codewort.
FOLLOWUP        = _env("FOLLOWUP", "1") in ("1", "true", "yes", "on")
FOLLOWUP_SEC    = float(_env("FOLLOWUP_SEC", "3.0"))
FOLLOWUP_MAX    = int(_env("FOLLOWUP_MAX", "10"))  # Runden, dann Pflichtpause

# ── Musik-Ducking (Wake/TTS) ───────────────────────────────────
# Beim Wake-Word und während LLM-Antworten per TTS abgespielt werden, wird
# die MUSIK leiser statt gestoppt: Lautstärke auf DUCK_VOLUME gesenkt,
# nach Session-Ende auf die vorherige Lautstärke zurück. DUCK_ENABLED=0
# deaktiviert das Ducking (Musik läuft ungedrückt weiter).
DUCK_ENABLED   = _env("DUCK_ENABLED", "1") in ("1", "true", "yes", "on")
DUCK_VOLUME    = int(_env("DUCK_VOLUME", "30"))   # Ziel-Lautstärke in %

# ── Lautstärke (amixer) ───────────────────────────────────────
# Prozent-Schritt pro Stufe: "lauter"/"leiser" ändern die Lautstärke immer
# um genau EINE Stufe (nicht auf einen absoluten Wert).
VOLUME_STEP    = int(_env("VOLUME_STEP", "10"))

# ── Framebuffer-Display (LLM-Antworten als Text) ───────────────
# /dev/fb1 wird direkt beschrieben (16-Bit BGR565, wie fbi es rendert).
# Ohne /dev/fb1 im Container bleibt das Display stumm – Feature optional.
FB_DEVICE    = _env("FB_DEVICE", "/dev/fb1")
FB_WIDTH     = int(_env("FB_WIDTH", "480"))
FB_HEIGHT    = int(_env("FB_HEIGHT", "320"))
FB_FONT_SCALE = int(_env("FB_FONT_SCALE", "4"))  # Schrift-Vergrößerungsfaktor
FB_TIMEOUT   = float(_env("FB_TIMEOUT", "60"))   # s ohne neuen Inhalt, dann dunkel
FB_PAGE_SEC  = float(_env("FB_PAGE_SEC", "5"))   # s pro Seite bei langen Antworten
IDLE_TICK_SEC  = float(_env("IDLE_TICK_SEC", "30"))     # s zwischen Idle-Display-Updates
IDLE_FRAME_DIR = _env("IDLE_FRAME_DIR", "/data/idle")   # shared volume dir
IDLE_TIMEOUT   = float(_env("IDLE_TIMEOUT", "5"))       # s ohne neues Frame → dunkel
IDLE_SHOW_MUSIC = int(_env("IDLE_SHOW_MUSIC", "1"))     # 1 = Musik-Info im Idle zeigen
# Quell-URL für das Idle-Standardbild. Der Container selbst lädt NICHT:
# das Host-Skript render_idle.py (Root-Cron) lädt diese URL, rendert sie
# nach RGB565 nach IDLE_FRAME_DIR und der Container blittet das Frame.
# Hier definiert, damit URL und Container dieselbe Quelle (shared .env) nutzen.
DEFAULT_IMAGE_URL = _env("DEFAULT_IMAGE_URL", "http://192.168.1.33/weather/snap.jpg")

# ── Musik-Player (mpg123: Dateien vom SMB-Share + Radio-Streams) ──
# MUSIC_ROOT: Wurzelordner aller Musik-/Medien-Dateien IM CONTAINER.
#
# Standard: /m — read-only Bind-Mount des Fileserver-Share, der beim
# docker run eingebunden wird (Muster: -v <host_share>:/m:ro).
# Der Inhalt richtet sich nach dem geteilten Fileserver-Share — typische
# Struktur: mp3/ (Musik), Record/ (Aufnahmen), Bilder/, download/ u.a.
#
# Relative Pfade in POST /play {path: "mp3/artist/album.mp3"} beziehen
# sich auf MUSIC_ROOT (/m). Ohne Mount (Desktop-Tests) → /play meldet
# 'Musikordner nicht erreichbar'. Überschreibbar per MUSIC_ROOT=… in .env.
MUSIC_ROOT      = _env("MUSIC_ROOT", "/m")
DATA_DIR        = _env("DATA_DIR", "/data")   # persistente Daten (Index-Cache, …)

# ── Pi-Bridge (RTSP-Relay + Kamera-Snapshot) ──────────────────
# Bridge-Daemon auf Pi: RTSP→MP3-Relay + Kamera-Snapshot-HTTP
BRIDGE_HOST     = _env("BRIDGE_HOST", "192.168.1.32")
BRIDGE_PORT     = int(_env("BRIDGE_PORT", "8790"))
BRIDGE_URL      = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"
RTSP_USER       = _env("RTSP_USER", "", fallback_empty=True)
RTSP_PASS       = _env("RTSP_PASS", "", fallback_empty=True)

# ── Web ────────────────────────────────────────────────────────
WEB_PORT        = int(_env("WEB_PORT", "8770"))

# ── MCP (HTTP-Remote-Steuerung) ──────────────────────────────
MCP_ENABLED     = _env("MCP_ENABLED", "1") in ("1", "true", "yes", "on")
MCP_HOST        = _env("MCP_HOST", "0.0.0.0")
MCP_PORT        = int(_env("MCP_PORT", "8780"))

LOG_LEVEL       = _env("LOG_LEVEL", "INFO")

# ── Master (2.0.0alpha) ─────────────────────────────────────────
# Indexing wandert in den Master-Container (dev-server, x86): er
# scannert CIFS und liefert den fertigen Index per HTTP. Der
# Satellite scannt CIFS nur noch als Fallback, wenn MASTER_URL
# leer ist oder der Master nicht erreichbar.
# Beispiel: MASTER_URL=http://192.168.1.24:8791
MASTER_URL      = _env("MASTER_URL", "http://192.168.1.24:8791")
MASTER_POLL_SEC = int(_env("MASTER_POLL_SEC", "300"))  # Index-Polling-Intervall

# Zentraler Musik-Stream: /proxy/music?path=<relativ> liefert Musikdateien
# direkt vom Master-Share — der Satellit braucht dafuer keinen Fileserver-Mount
# (/m) mehr. Optionaler Authorization-Header-Wert (z.B. "Bearer <token>") fuer
# einen evtl. auth-geschuetzten Master. Leer = Proxy aus (nur lokaler Mount).
MASTER_MUSIC_PROXY_URL = _env("MASTER_MUSIC_PROXY_URL", MASTER_URL).strip().rstrip("/")
MASTER_MUSIC_AUTH      = _env("MASTER_MUSIC_AUTH", "").strip()

# ── Satellite Identifikation ──────────────────────────────────
SATELLITE_MAC = _env("SATELLITE_MAC", "").strip() or "00:00:00:00:00:00"


# ── Heartbeat (Satellit → Master) ─────────────────────────────
HEARTBEAT_ENABLED = int(_env("HEARTBEAT_ENABLED", "1"))
HEARTBEAT_SEC     = int(_env("HEARTBEAT_SEC", "30"))

# ── Zentrale Konfiguration (Master → Satellite-Push) ──────────
# config_version wird bei jedem apply_override erhöht. Der Heartbeat
# pollt GET /api/config/apply?mac=… und wendet Änderungen ohne
# Neustart an (dynamische Config-Konsum in tts.py/stt.py/router.py).
#
# Persistenz: Jedes erfolgreich angewendete Config-Snapshot landet in
# SATELLITE_CONFIG_DIR/master_config.json. Startet der Container und
# ist der Master gerade nicht erreichbar, werden die zuletzt
# angewendeten Values als Fallback geladen (load_local_config).
_CONFIG_VERSION = 0

# Lokales Fallback-Snapshot der letzten Master-Config (persistiert).
SATELLITE_CONFIG_DIR = _env("SATELLITE_CONFIG_DIR", "/data/config")
_LOCAL_CONFIG_PATH = os.path.join(SATELLITE_CONFIG_DIR, "master_config.json")


def load_local_config() -> None:
    """Liest das lokal gesicherte Master-Config-Snapshot und wendet es an.

    Startet der Satellit, bevor der Master antwortet (z.B. Master noch
    nicht erreichbar), werden die zuletzt bekannten Values aus der
    Datei übernommen, damit TTS/STT/LLM/OPENHAB mit Proxy-URLs arbeiten
    können statt mit leeren/Default-Werten.
    """
    global _CONFIG_VERSION
    try:
        with open(_LOCAL_CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        logging.warning("local_config: kein/ungültiges Snapshot (%s): %s",
                        _LOCAL_CONFIG_PATH, e)
        return
    overrides = data.get("overrides") or {}
    if not overrides:
        return
    applied, ignored = apply_overrides(overrides)
    if applied:
        _CONFIG_VERSION += 1
        logging.info(
            "local_config: %d Werte aus %s geladen (Master-Config v%s vom %s)",
            len(applied), _LOCAL_CONFIG_PATH,
            data.get("version"), data.get("saved_at", "?"),
        )


def save_local_config(overrides: dict, master_version: int) -> None:
    """Speichert ein angewendetes Master-Config-Snapshot lokal (atomar)."""
    try:
        os.makedirs(SATELLITE_CONFIG_DIR, exist_ok=True)
    except OSError as e:
        logging.warning("local_config: Verzeichnis nicht anlegbar: %s", e)
        return
    payload = {
        "version": master_version,
        "saved_at": time.time(),
        "overrides": overrides,
    }
    tmp = _LOCAL_CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _LOCAL_CONFIG_PATH)
    except OSError as e:
        logging.warning("local_config: Persistenz fehlgeschlagen: %s", e)


def bump_config_version() -> None:
    """Version hochzählen (wird vom Heartbeat-Master-Apply gesetzt)."""
    global _CONFIG_VERSION
    _CONFIG_VERSION += 1


def config_version() -> int:
    """Aktuelle Config-Version (für UI-Status / Log)."""
    return _CONFIG_VERSION


def _pull_config_from_master(mac: str, version: int) -> dict:
    """Holt Config-Overrides vom Master-Proxy via GET /api/config/apply.
    
    Gibt dict mit Keys zurück, die gesetzt werden sollen (oder {} bei Fehler).
    """
    import urllib.request
    import json
    import logging
    
    master_url = MASTER_URL.rstrip('/')
    if not master_url:
        logging.warning("config_pull: MASTER_URL nicht gesetzt")
        return {}
    
    url = f"{master_url}/api/config/apply?mac={mac}&version={version}"
    logging.info("config_pull: Fetch config v%s from %s", version, url)
    
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                data = json.loads(resp.read())
                overrides = data.get('overrides', {})
                if data.get('changed'):
                    logging.info("config_pull: Config changed, %d keys", len(overrides))
                return overrides
            else:
                logging.warning("config_pull: HTTP %d", resp.status)
    except Exception as e:
        logging.error("config_pull: Failed to fetch config: %s", e)
    
    return {}


# Keys, die der Master in den Satelliten zieht (einmalig beim Boot).
# Nur diese dürfen überschrieben werden (keine Hardware-Keys
# wie MIC_DEVICE/FB_DEVICE). Die 7 "globalen" Keys (Timeouts, Limits,
# Defaults) sollen nicht pro Container gesetzt werden, sondern zentral
# über den Master verwaltet werden.
_OVERWRITABLE = {
    "TTS_URL", "TTS_MODEL", "TTS_VOICE", "TTS_LANGUAGE",
    "STT_URL", "STT_MODEL", "STT_LANGUAGE",
    "LLM_URL", "LLM_MODEL", "LLM_HAS_TOOLS", "LLM_ENABLE_THINKING",
    "OPENHAB_URL", "MUSIC_ROOT", "BRIDGE_HOST", "BRIDGE_PORT",
    "MASTER_MUSIC_PROXY_URL", "MASTER_MUSIC_AUTH",
    # Globale Settings (Timeouts, Limits, Defaults):
    "STT_TIMEOUT", "TTS_TIMEOUT", "LLM_TIMEOUT",
    "LLM_MAX_TOKENS", "OPENHAB_CACHE_TTL",
}


def apply_overrides(d: dict) -> tuple[list[str], list[str]]:
    """Wende Master-Config-Änderungen dynamisch an.

    Gibt (applied_keys, ignored_keys) zurück. Nur Keys aus _OVERWRITABLE
    werden gesetzt; bools werden aus "true"/"false"/1/0 konvertiert.
    """
    import types  # noqa: F401  (nur für Typ-Hinweise; wird nicht gebraucht)
    applied, ignored = [], []
    for k, v in (d or {}).items():
        if k not in _OVERWRITABLE:
            ignored.append(k)
            continue
        if isinstance(v, bool) or (isinstance(v, str) and v.lower() in ("true", "false")):
            v = (v if isinstance(v, bool) else str(v).lower() == "true")
        elif v is None or v == "":
            v = ""
        elif isinstance(v, str) and v.lstrip("-").isdigit():
            v = int(v)
        setattr(sys.modules[__name__], k, v)
        applied.append(k)
    return applied, ignored
