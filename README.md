# voice-satellite

Erweiterter Pi 3 B+ Satellite (Rhasspy-2.0-Image, Python 3.11) für:

- **TTS (Qwen3-TTS) via REST API** (tts-Container, Port 7811)
- **Whisper STT via REST API** (faster-whisper, Port 9000)
- **LLM via REST API** (Ollama / vLLM / LiteLLM, Chat-Completions)
- **openHAB-REST** (Kontext fürs LLM + Smart-Home-Intent)
- **Framebuffer-Display** (LLM-Antworten als Text direkt auf `/dev/fb1`)
- **Pi-Bridge** (RTSP-Relay + Kamera-Snapshot, Port 8790)

## Features

- **Deterministische Intents ohne LLM** – Musik, Radio, Lautstärke,
  OpenHAB-Geräte, Kamera, Titel-Navigation und „Neu indizieren" werden lokal
  per Regex gematcht und sofort ausgeführt (deterministisch, schnell, kein
  LLM-Rundgang).
- **LLM-Fallback mit Gedächtnis** – alles, was lokal nicht gematcht wird, geht
  an das LLM (Hermes-Gateway) mit Kurz- + Langzeitgedächtnis.
- **Persistenter Medien-Index** – M3U-/MP3-/Verzeichnis-Index liegt auf der SSD
  (`DATA_DIR/media_index.json`, Default `/data`): **kein Neuscan beim
  Container-Start**, automatischer Re-Scan nur **stündlich** (3600 s). Neue
  Titel erscheinen spätestens nach einer Stunde – oder sofort per „Neu
  indizieren" (Stimme) / „Neu indizieren"-Button in der WebUI.
- **Case-insensitive Musiksuche** – „falco - 191" findet „Falco - 191.mp3"; der
  Direktfile-Check in `mp3/` (und eine Ebene tiefer) vergleicht
  case-insensitiv – funktioniert auf ext4 *und* CIFS-Shares.
- **Radio-/TV-Streams aus drei M3U-Dateien** – `TV/{radio,world,tv}.m3u` unter
  dem Fileserver werden indiziert (RTSP→HTTP über die Pi-Bridge); Dedup über
  alle drei Listen.
- **WebUI (Port 8770)** – Musik-Leiste (Play/Stop/Vorwärts/Rückwärts/Shuffle/
  Weiter) in einer Reihe, der aktuell abspielende Titel bleibt nach Reload
  erhalten, Shuffle-Zustand sichtbar, Index-Status (Dateien/Playlists + Alter)
  mit „Neu indizieren"-Button.
- **MCP-Server** (Port 8780) – JSON-RPC-Tools für externe Steuerung (Hermes).

## Sprachbefehle

Das Codewort „Computer" ist optional davor; die Tabelle unterscheidet, was
**lokal (ohne LLM)** gematcht wird und was **an das LLM** weitergeht.

### Lokal / ohne LLM (deterministisch)

| Bereich | Beispiel | Anmerkung |
|---|---|---|
| Playlist | „Spiel Playlist Chill", „Spiele die Playlist Rock" | M3U-Index |
| MP3 / Titel | „Spiel Falco", „Spiel 191", „Spiel die Musik von Falco" | Direktfile (case-insens.), Ordner- & Datei-Index |
| Radio / TV | „Spiel Radio Eins", „Spiel Radio SDR" | 3× M3U (radio/world/tv), RTSP via Pi-Bridge |
| Musik an/aus | „Musik an", „Spiele Musik", „Mach Musik an", „Musik aus" | Zufall-Playlist bzw. Stopp |
| Vorwärts / Rückwärts | „Nächster", „Vorwärts", „Rückwärts", „Zurück" | Titel-Wechsel |
| Weiter / Fortsetzen | „Weiter", „Fortsetzen", „Letztes" | letzte Playlist fortsetzen |
| Shuffle | „Zufallswiedergabe Playlist Rock", „Shuffle Chill" | Playlist im Zufallsmodus |
| Lautstärke (relativ) | „Lauter", „Leiser" | ±1 Stufe (`VOLUME_STEP`) |
| Lautstärke (Stufen) | „Lautstärke 3" | 5 Stufen: 0=Mute, 1–5 = 20–100 % |
| Lautstärke (Prozent) | „Lautstärke 60 Prozent" | 0–100 % |
| Kamera | „Zeig Kamera Garten", „Was ist in Kamera Garten los?" | Snapshot via Pi-Bridge |
| Neu indizieren | „Neu indizieren", „Reindex", „Reindex jetzt" | Index sofort neu bauen |
| Stopp | „Stopp", „Stopp bitte" | laufende Wiedergabe stoppen |
| OpenHAB / Smart Home | „Licht an", „Jalousie zu", … | Regeln aus `RULES_FILE` |

### Mit LLM (Fallback)

Jeder Befehl, der **nicht** lokal gematcht wird, geht an das LLM
(Hermes-Gateway, `LLM_URL`) – z. B. Freitext-Fragen, komplexe Anweisungen oder
Kontext-Befehle („Wie war das gerade wieder?" → Gedächtnis). Das LLM antwortet
per TTS und optional mit Display-Text.

> **Regel:** Musik-/Radio-/Lautstärke-/Kamera-/Stopp-/OpenHAB- und Reindex-
> Befehle laufen **ohne LLM** (deterministisch). Erst wenn nichts davon passt,
> greift das LLM.

## Installation (frisches Gerät)

```bash
git clone https://github.com/wakkomon/voice-satellite.git
cd voice-satellite
python3 scripts/make_env.py   # erzeugt .env mit Defaults (nur fehlende Keys)
nano .env                     # Secrets/Tokens eintragen: OPENHAB_TOKEN, TTS_TOKEN, LLM_TOKEN, RTSP_*
./deploy.sh
```

**Update:** `git pull && ./deploy.sh`

`deploy.sh` ist idempotent: Es baut das Image neu (`--no-cache`), entfernt den
alten Container (Name bleibt `voice-satellite`) und startet ihn mit `--env-file .env`.
Das Daten-Volume (`voice-satellite-data`) und die `.env` werden **niemals**
angefasst. `scripts/make_env.py` ergänzt bei Provision **nur fehlende** Keys
(Merge) und überschreibt eine laufende `.env **nicht**.

Alternativ via `docker-compose up -d --build` (ersetzt den Container,
gleiche Mounts/Volumes wie `deploy.sh`).

### Deploy via Portainer (Stack aus Repository)

Die `docker-compose.yml` referenziert **kein** `.env`-File — alle 72
Konfigurations-Keys aus `app/config.py` liegen als `${VAR:-…}`-Interpolationen
in der `environment`-Liste, default mit den Pi-Live-Werten. So deployt man den
Stack:

1. Portainer → **Stacks → Add stack → Web editor** → Repository-URL
   `https://<user>:<token>@github.com/wakkomon/voice-satellite.git`
   (Private-Repo: GitHub-Personal-Access-Token mit `repo`-Scope).
2. In der Stack-Ansicht unter **Variables** die Werte für das Gerät setzen:
   Pflicht sind die Secrets `OPENHAB_TOKEN`, `TTS_TOKEN`, `LLM_TOKEN`,
   `RTSP_USER`, `RTSP_PASS` — **default leer** (lokale APIs ohne Token),
   leer lassen = Feature ohne Auth/deaktiviert. Jeder weitere Key
   (`TTS_URL`, `TTS_MODEL`, `STT_URL`, `LLM_URL`, `WAKE_PPN`, …) lässt sich
   dort genauso überschreiben, Defaults gelten sonst unverändert.
   Beispiel Piper-TTS: `TTS_MODEL=piper`, `TTS_VOICE=kitt`,
   `TTS_URL=http://192.168.1.33:5000/v1/audio/speech`, `TTS_LANGUAGE=de`.
3. **Build and deploy** — Portainer baut das Image selbst (entspricht
   `deploy.sh` minus Health-Check-Output).

> Hinweis: Portainer-Stacks liegen unter `/data/compose/<id>/`; bei Updates
> „Force redeploy" nutzen, damit `git pull`/Rebuild greift.

> ⚠️ `.env` enthält Secrets → `chmod 600 .env`, **niemals** ins Git.
>
> Voraussetzungen Host: `docker` + `docker buildx` nicht benötigt (klassisches Build),
> `python3` für `make_env.py`, Audio-/Framebuffer-Geräte `/dev/snd` + `/dev/fb1`,
> optional `/media/private` als SMB-Bind-Mount für Musik-/Radio-/Kamera-M3U
> (fehlt es, läuft der Satellite, aber Playlists/Kamera sind leer – `deploy.sh`
> warnt dann).

## API

- `POST /play` `{path}` — mpg123 spielt `MUSIC_ROOT`/path (Stream/Datei)
- `POST /play/radio` `{preset}` — Preset-Playliste (`/data/playlists/preset.json`)
- `POST /stop`, `GET /state` — Playback-Steuerung
- `POST /speak` `{text}` — spricht einen Text aus (TTS)
- `POST /tone` `{type}` — Alarm-/Feedback-Töne (wake/end/boot/error/alarm)
- `POST /intent` `{text}` — Deterministic-Intent: `smart` (openHAB) oder `volume`
- `POST /volume` `{level}` — absoluter Lautstärke-Wert (0–100)
- `GET  /index` — Index-Status: `{files, playlists, age_s, building, interval_s}`
- `POST /index/rebuild` — Medien-Index sofort neu bauen („Neu indizieren")
- `GET /healthz`, `/metrics` — Monitoring

Lautstärke per Stimme: „lauter"/„leiser" ändern die Lautstärke **immer um
genau eine Stufe** (`VOLUME_STEP`, Default 10 %) — kein Absolutwert.

## Netzwerk

```
Satellite: 192.168.1.32 (Container voice-satellite, hostnet)
  ├─ openHAB      http://192.168.1.33:8080/rest
  ├─ TTS Qwen3    http://192.168.1.31:7811/v1/audio/speech
  ├─ STT Whisper  http://192.168.1.31:9000/v1/audio/transcriptions
  ├─ LLM (Hermes-Gateway) http://192.168.1.33:8642/v1/chat/completions
  └─ Pi-Bridge    http://192.168.1.32:8790
```

## ENV-Variablen (Vollreferenz)

Jede Variable wird in `app/config.py` gelesen; hier der Wert, der greift, wenn
die Variable in `.env` **nicht** gesetzt ist. `.env.example` ist die
maschinenlesbare Vorlage mit den gleichen Defaults. Alle Variablen,
gruppiert:

### Audio (ALSA)

| Variable | Default | Bedeutung |
|---|---|---|
| `MIC_DEVICE` | `plughw:2,0` | ALSA-Mikrofon (`plughw` konvertiert 44.1k→16k) |
| `SPEAKER_DEVICE` | `plughw:1,0` | ALSA-Lautsprecher (TTS-WAV) |

### Wake-Word & VAD

| Variable | Default | Bedeutung |
|---|---|---|
| `WAKE_PPN` | `/app/wake/computer_raspberry-pi.ppn` | Pfad zur Porcupine-Modelldatei |
| `WAKE_SENS` | `0.5` | Codewort-Empfindlichkeit (niedriger = empfindlicher) |
| `WAKE_WORD` | `Computer` | Anzeige-Text fürs UI (kein Technikbezug) |
| `VAD_MODE` | `energy` | `energy` \| `webrtc` (webrtc nur x86) |
| `VAD_AGGRESSIVENESS` | `3` | webrtc-VAD-Aggressivität 0–3 (nur bei `webrtc`) |
| `ENERGY_THRESHOLD` | `1200` | Energie-Schwelle für `energy`-VAD |
| `SILENCE_SEC` | `1.3` | Stille in s = Ende der Sprache |
| `MIN_REC_SEC` | `0.4` | minimale Aufnahme-Länge (s) |
| `MAX_REC_SEC` | `27` | maximale Aufnahme-Länge (s) |
| `PREROLL_SEC` | `0.3` | Vorlauf, damit Satzanfang nicht fehlt (s) |
| `WAKE_SKIP_SEC` | `0.35` | Codewort-Rest verwerfen, bevor STT startet (s) |

### STT (Whisper)

| Variable | Default | Bedeutung |
|---|---|---|
| `STT_URL` | `http://192.168.1.31:9000/v1/audio/transcriptions` | Whisper-Transkriptions-Endpunkt |
| `STT_MODEL` | `whisper-1` | Whisper-Modell-Name |
| `STT_LANGUAGE` | `de` | fest Deutsch, sonst rät Whisper → Englisch |
| `STT_TIMEOUT` | `30` | Timeout in s |

### openHAB & Regeln

| Variable | Default | Bedeutung |
|---|---|---|
| `OPENHAB_URL` | `http://192.168.1.33:8080/rest/items` | REST-API-Basis |
| `OPENHAB_TOKEN` | *(leer)* | Bearer-Token (Secret) |
| `OPENHAB_CACHE_TTL` | `300` | Item-Liste cachen (s) |
| `RULES_FILE` | `/data/rules.yaml` | Pfad zur lokalen Regelungsdatei |

### LLM

| Variable | Default | Bedeutung |
|---|---|---|
| `LLM_URL` | `http://192.168.1.33:8642/v1/chat/completions` | Chat-Completions-Endpunkt (Hermes-Gateway) |
| `LLM_TOKEN` | *(leer)* | API-Key (Gateway/Agent); Ollama leer |
| `LLM_MODEL` | `hermes-agent` | Modell (Hermes-Gateway-Agent mit Terminal) |
| `LLM_TIMEOUT` | `180` | Timeout (Gateway-Agent braucht länger) |
| `LLM_MAX_TOKENS` | `1024` | max. Antwort-Tokens (mit Thinking-aus reicht 1024) |
| `LLM_ENABLE_THINKING` | `false` | Reine Chat-Endpunkte: aus = schneller |
| `LLM_HAS_TOOLS` | `auto` | `true\|false\|auto` — erkennt Agent-Endpunkte |

### Langzeitgedächtnis (SQLite)

| Variable | Default | Bedeutung |
|---|---|---|
| `MEMORY_DB` | `/data/memory.db` | Pfad zur SQLite-DB |
| `MAX_HISTORY` | `20` | Nachrichten im Kurzzeitgedächtnis |
| `MAX_FACTS` | `100` | max. Facts im Langzeitgedächtnis |

### TTS (Qwen3-TTS)

| Variable | Default | Bedeutung |
|---|---|---|
| `TTS_URL` | `http://192.168.1.31:7811/v1/audio/speech` | Speech-Endpunkt (OpenAI-kompatibel) |
| `TTS_TOKEN` | *(leer)* | Zugriffstoken des TTS-Servers (Secret) |
| `TTS_MODEL` | `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` | TTS-Modell |
| `TTS_VOICE` | `Uncle_fu` | Stimme |
| `TTS_LANGUAGE` | `German` | Sprache; leer = Server-Default |
| `TTS_TIMEOUT` | `60` | Timeout in s |

#### Alternative: Piper TTS

Statt Qwen3-TTS kann der Satellite alternativ auf einen [Piper](https://github.com/OHF-Voice/piper1-gpl)-Server
zugreifen. In der `.env` die folgenden Werte setzen (gleiche Variablennamen):

```env
TTS_MODEL=piper
TTS_VOICE=kitt
TTS_URL=http://192.168.1.33:5000/v1/audio/speech
TTS_LANGUAGE=de
```

`TTS_LANGUAGE` wird dann als `language`-Feld in den Piper-Request übergeben
(leer = Server-Default).

### Töne / Alarm

| Variable | Default | Bedeutung |
|---|---|---|
| `TONE_GAIN` | `0.5` | Tön-Lautstärke 0–1 |
| `TONE_SEC` | `0.15` | Tonlänge (s) |
| `TONE_WAKE_HZ` | `880` | Wake-Word erkannt |
| `TONE_END_HZ` | `523` | Spracheingabe zu Ende |
| `TONE_BOOT_HZ` | `660` | Boot-Ton 1 (E5) |
| `TONE_BOOT2_HZ` | `1320` | Boot-Ton 2 (eine Oktave höher) |
| `TONE_ERROR_HZ` | `220` | Fehler (LLM-Ausfall/leere Antwort) |
| `TONE_ALARM_HZ` | `880` | Alarm-Sirene hoch (A5) |
| `TONE_ALARM_HZ2` | `440` | Alarm-Sirene tief (A4) |
| `TONE_ALARM_REPEATS` | `3` | Sirenen-Halbzyklen |
| `TONE_BLIP_SEC` | `0.10` | Kurz-Blip: Lautstärke-Feedback (Web-UI) |

### Follow-up

| Variable | Default | Bedeutung |
|---|---|---|
| `FOLLOWUP` | `1` | Nach Antwort ohne Codewort weitersprechen |
| `FOLLOWUP_SEC` | `3.0` | Stille ohne Spracheingabe → zurück zum Codewort |
| `FOLLOWUP_MAX` | `10` | max. Folgen-Runden pro Session |

### Lautstärke (amixer)

| Variable | Default | Bedeutung |
|---|---|---|
| `VOLUME_STEP` | `10` | % pro Stufe; „lauter"/„leiser" = immer genau eine Stufe |

### Musik (Ducking + Player)

| Variable | Default | Bedeutung |
|---|---|---|
| `DUCK_ENABLED` | `1` | Musik beim Codewort leiser statt stoppen |
| `DUCK_VOLUME` | `30` | Ziel-Lautstärke in % während Duck |
| `MUSIC_ROOT` | `/m` | Musik-Wurzelordner im Container (SMB-Bind-Mount) |

### Daten (Index-Persistenz)

| Variable | Default | Bedeutung |
|---|---|---|
| `DATA_DIR` | `/data` | Daten-Volume: Index-Cache (`media_index.json`), Memory-DB, Idle-Frames, Regeln |

> Der Medien-Index wird bei Start aus `DATA_DIR/media_index.json` geladen
> (kein CIFS-Neuscan beim Boot) und **stündlich** neu gebaut. „Neu indizieren"
> (Stimme) oder `POST /index/rebuild` triggern einen sofortigen Re-Scan.

### Framebuffer-Display

| Variable | Default | Bedeutung |
|---|---|---|
| `FB_DEVICE` | `/dev/fb1` | Framebuffer-Gerät (Display-Pfad) |
| `FB_WIDTH` | `480` | Breite in Pixel |
| `FB_HEIGHT` | `320` | Höhe in Pixel |
| `FB_FONT_SCALE` | `4` | Schrift-Vergrößerungsfaktor |
| `FB_TIMEOUT` | `60` | s ohne neuen Inhalt → dunkel |
| `FB_PAGE_SEC` | `5` | s pro Seite bei langen Antworten |

### Idle-Display (Wetter-/Standardbild)

| Variable | Default | Bedeutung |
|---|---|---|
| `IDLE_TICK_SEC` | `30` | s zwischen Idle-Display-Updates |
| `IDLE_FRAME_DIR` | `/data/idle` | Verzeichnis für gespeicherte `.bin`-Frames (Offline-Fallback) |
| `IDLE_TIMEOUT` | `5` | s ohne neues Frame → dunkel |
| `IDLE_SHOW_MUSIC` | `1` | 1 = Musik-Info im Idle zeigen |
| `DEFAULT_IMAGE_URL` | `http://192.168.1.33/weather/snap.jpg` | Quell-URL für das Idle-Standardbild |

> **Idle-Architektur:** Der Container lädt `DEFAULT_IMAGE_URL` selbst, decodiert es mit Pillow (JPEG → RGB565, 480×320, 3:2-cropped) und blittet es direkt. Eine lokale `.bin`-Datei in `IDLE_FRAME_DIR` wird als Offline-Fallback genutzt und nach erfolgreichem URL-Download automatisch aktualisiert. Kein Host-Script, kein Cron nötig.

### Pi-Bridge (RTSP)

| Variable | Default | Bedeutung |
|---|---|---|
| `BRIDGE_HOST` | `192.168.1.32` | Host der Pi-Bridge |
| `BRIDGE_PORT` | `8790` | Port der Pi-Bridge |
| `RTSP_USER` | *(leer)* | RTSP-Kamera-Login (Secret) |
| `RTSP_PASS` | *(leer)* | RTSP-Kamera-Passwort (Secret) |

### Verhalten

| Variable | Default | Bedeutung |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Log-Level |
| `WEB_PORT` | `8770` | HTTP-Interface (Debug-UI, `/speak`, `/tone`, `/intent`) |

### MCP (Remote-Steuerung)

Der Satellite stellt einen MCP-Server (Model Context Protocol,
JSON-RPC-over-HTTP) bereit, über den ein AI-Agent Töne abspielen und
Bilder auf dem Display anzeigen kann. Läuft ohne externes `mcp`-Paket
(eigener `http.server`, nur Python-Standardlib).

| Variable | Default | Bedeutung |
|---|---|---|
| `MCP_ENABLED` | `1` | `0` = MCP-Server deaktiviert |
| `MCP_HOST` | `0.0.0.0` | Bind-Adresse des MCP-HTTP-Servers |
| `MCP_PORT` | `8780` | HTTP-Port des MCP-Endpunkts |

**Endpunkte:**

- `GET  /mcp/tools` — Tool-Discovery (Schema + Liste aller Tools)
- `POST /mcp` — Tool-Aufruf, JSON-RPC 2.0:

```bash
# Ton abspielen
curl -X POST http://192.168.1.32:8780/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"play_tone","params":{"name":"boot"}}'

# Text auf dem Display
curl -X POST http://192.168.1.32:8780/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"show_text","params":{"heading":"Hallo","body":"Welt"}}'
```

**Verfügbare Tools:** `play_tone` (boot/wake/end/error/alarm/blip),
`show_text`, `show_weather`, `show_music_info`, `set_alarm` (Minuten +
optionaler Label-Text), `get_tones`.

> ⚠️ Port-Kollision: `MCP_PORT` darf sich nicht mit `WEB_PORT` (8770) oder
> `BRIDGE_PORT` (8790) überschneiden — der Satellite läuft mit `hostnet`.

### STT-Logging

Das erkannte STT-Text liegt in der Konsole/Log als **Volltext** (`STT: …`)
ohne Kürzung; der Debug-Log `STT erkannt: …` ist ebenfalls ungeschnitten.
