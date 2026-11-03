# voice-satellite

> **Hinweis:** Dieses Projekt wurde teilweise KI-generiert — wesentliche Teile des
> Quellcodes (Master- und Satellite-Module, Deploy-Skripte) wurden von einem
> KI-Agenten (Hermes Agent, Nous Research) in Zusammenarbeit mit dem Betreiber
> erstellt und getestet.

Erweiterter Pi 3 B+ Satellite (Rhasspy-2.0-Image, Python 3.11) für:

- **TTS (Qwen3-TTS) via REST API** (tts-Container, Port 7811)
- **Whisper STT via REST API** (faster-whisper, Port 9000)
- **LLM via REST API** (Ollama / vLLM / LiteLLM, Chat-Completions)
- **openHAB-REST** (Kontext fürs LLM + Smart-Home-Intent)
- **Framebuffer-Display** (LLM-Antworten als Text direkt auf `/dev/fb1`)
- **Pi-Bridge** (RTSP-Relay + Kamera-Snapshot, Port 8790)

## Architektur: Master + Satelliten (2.0)

Das System besteht aus zwei Seiten:

- **Satellite** (Pi 3 B+, Rhasspy-Image) – das Sprach-Endgerät. Audio
  rein/raus, Wake-Word, VAD, deterministische Intents, Display, Töne.
  Die Backends (TTS/STT/LLM/openHAB) erreicht er **ausschließlich über den
  zentralen Proxy des Masters** – in der Satellite-Konfiguration stehen
  daher **nur** `MASTER_URL` + `MASTER_TOKEN`.
- **Master** (x86-Server, Container `voice-satellite-master`, Port 8791) –
  zentraler API-Proxy, Medien-Indexer, Konfigurations-Store,
  Live-Dashboard/MCP. Die echten Backend-Ziele und **alle** API-Tokens
  liegen nur hier (ENV `.env.master` bzw. Config-Store in der Master-WebUI).

Die genaue Trennung (wer was wo konfiguriert) steht unter
[Master/Satellite-Trennung](#mastersatellite-trennung-und-konfiguration) und
[Master-ENV](#master-env-variablen-master-container).

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

### Satellite (Pi)

```bash
git clone https://github.com/wakkomon/voice-satellite.git
cd voice-satellite
python3 scripts/make_env.py   # erzeugt .env mit Defaults (nur fehlende Keys)
nano .env                     # MASTER_URL/MASTER_TOKEN + lokale Secrets (RTSP_*) eintragen
./deploy.sh
```

**Update:** `git pull && ./deploy.sh`

Wichtig für die Trennung: Der Satellite kennt **weder** die Backend-URLs
(TTS/STT/LLM/openHAB) **noch** deren Tokens. In der Satellite-`.env` stehen
dazu nur `MASTER_URL` (Basisadresse des Masters) und `MASTER_TOKEN`
(Zugang zum Master, derselbe Wert wie im Master-Config-Store, Bereich
„Secrets"). Backend-URLs und -Tokens liegen ausschließlich am Master.

### Master (dev-server)

Der Master läuft einmalig zentral (Standard: dev-server, Container
`voice-satellite-master`, Port 8791). Deploy:

```bash
git clone https://github.com/wakkomon/voice-satellite.git
cd voice-satellite
nano .env.master.example       # Backend-URLs, MASTER_TOKEN eintragen
./deploy-master.sh [ssh_host]  # Default-Ziel: 192.168.1.24
```

`deploy-master.sh` baut das Master-Image (`Dockerfile.master`), kopiert
`.env.master.example` als `.env.master` + `docker-compose.master.yml` auf den
Ziel-Server und startet den Container per `docker compose up -d`. Die zentrale Satelliten-Konfig
liegt im Master-Volume unter `/data/config/satellites.json`
(`VS_CONFIG_DIR`) und überlebt Rebuilds.

Alle Satellite-Werte, die nicht geräte-lokal sind (TTS-Modell/Stimme,
LLM-Modell, Timeouts, Musik-Root, Display-URL, …), gehören **nicht** in die
`.env` des Pi, sondern in den **Master-Config-Store** (Master-WebUI:
globale Werte + per-Satellite-Overrides). Die Satelliten ziehen die Werte
per Heartbeat (`GET /api/config/apply`) und wenden sie **ohne Neustart** an.

`deploy.sh` ist idempotent: Es baut das Image neu (`--no-cache`), entfernt den
alten Container (Name bleibt `voice-satellite`) und startet ihn mit `--env-file .env`.
Das Daten-Volume (`voice-satellite-data`) und die `.env` werden **niemals**
angefasst. `scripts/make_env.py` ergänzt bei Provision **nur fehlende** Keys
(Merge) und überschreibt eine laufende `.env **nicht**.

Alternativ via `docker-compose up -d --build` (ersetzt den Container,
gleiche Mounts/Volumes wie `deploy.sh`).

### Deploy via Portainer (Stack aus Repository)

Die `docker-compose.yml` referenziert **kein** `.env`-File — alle
Konfigurations-Keys aus `app/config.py` liegen als `${VAR:-…}`-Interpolationen
in der `environment`-Liste, default mit den Pi-Live-Werten. So deployt man
den Stack:

1. Portainer → **Stacks → Add stack → Web editor** → Repository-URL
   `https://<user>:<token>@github.com/wakkomon/voice-satellite.git`
   (Private-Repo: GitHub-Personal-Access-Token mit `repo`-Scope).
2. In der Stack-Ansicht unter **Variables** die Werte für das Gerät setzen.
   Das sind die **Satellite-seitigen** Keys – insbesondere
   `MASTER_URL` + `MASTER_TOKEN` (Zugang zum Master) und die lokalen
   Secrets `RTSP_USER`/`RTSP_PASS`. Die Backend-URLs und -Tokens stehen dort
   **nicht**; sie werden zentral am Master gepflegt (siehe
   [Master-ENV](#master-env-variablen-master-container)). Jeder weitere Key
   (`TTS_MODEL`, `TTS_VOICE`, `WAKE_PPN`, …) lässt sich dort genauso
   überschreiben, Defaults gelten sonst unverändert.
   (Empfehlung: TTS-/LLM-Modelle etc. lieber im Master-Config-Store statt
   pro Stack setzen – die Overrides wirken dann auf alle Satelliten und
   ohne Rebuild.)
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
- `GET  /index` — Index-Status: `{files, playlists, building, files_seen, interval_s}` (Build läuft: `files` zählt live mit)
- `POST /index/rebuild` — Medien-Index sofort neu bauen („Neu indizieren")
- `GET /healthz`, `/metrics` — Monitoring

Lautstärke per Stimme: „lauter"/„leiser" ändern die Lautstärke **immer um
genau eine Stufe** (`VOLUME_STEP`, Default 10 %) — kein Absolutwert.

## Netzwerk

```
Satellite (Pi, hostnet)
  └─ MASTER_URL  http://192.168.1.24:8791   → Master (Port 8791, Container-Netz)
                                                        │
Master-Proxy (jeder Call mit Bearer `MASTER_TOKEN`)      ▼
  ├─ /proxy/tts     → TTS-Backend    http://192.168.1.31:7811/v1/audio/speech
  ├─ /proxy/stt     → STT-Backend    http://192.168.1.31:9000/v1/audio/transcriptions
  ├─ /proxy/llm     → LLM (Hermes-Gateway) http://192.168.1.33:8642/v1/chat/completions
  ├─ /proxy/openhab → openHAB       http://192.168.1.33:8080/rest
  └─ /proxy/music   → Musik-Dateien (CIFS-Share, Range-Requests)

Satellite-own (direkt, ohne Master):
  ├─ Web-UI/MCP     http://<pi>:8770 / http://<pi>:8780
  └─ Pi-Bridge      http://192.168.1.32:8790  (RTSP-Relay + Kamera-Snapshot)
```

Der Satellite spricht also **einzig** die Basisadresse `MASTER_URL` an –
die echten Backend-Adressen kennen nur der Master (ENV `*_BACKEND_URL` in
`.env.master`). `MASTER_TOKEN` (identisch auf beiden Seiten) ist der **einzige**
Token, den der Satellite hält: er sichert **alle** `/proxy/*`-Calls, Heartbeat,
Config-Pull und Medien-Stream. Der Proxy ersetzt ihn bei jedem Aufruf durch den
jeweiligen Backend-Token (Reihenfolge Config-Store → ENV) und gibt ihn erst dann
an das Backend weiter – dieser landet nie auf dem Satellite.

## Master/Satellite-Trennung und Konfiguration

2.0-Regel: **Der Satellite kennt keine Backends und keine Backend-Tokens.**
Für TTS/STT/LLM/openHAB und Musik spricht er ausschließlich den
Master-Proxy unter `MASTER_URL` an; der Proxy injiziert die Backend-Tokens,
damit sie nie auf dem Satellite landen.

| Ebene | Inhalt | Konfiguriert in |
|---|---|---|
| **Satellite – ENV** | Hardware, Audio, Wake/VAD, Töne, Display, Bridge, lokale Pfade, Identität (`SATELLITE_MAC`, `SAT_NAME`), Master-Zugang (`MASTER_URL`, `MASTER_TOKEN`), Heartbeat-Schalter | `.env` des Pi (gelesen von `app/config.py`) |
| **Master – ENV** | Server (`API_PORT`, …), Medien-Index (`MUSIC_ROOT`, `CACHE_DIR`), Heartbeat-Grenzen, **echte Backend-Ziele** (`TTS_BACKEND_URL`, `STT_BACKEND_URL`, `LLM_BACKEND_URL`, `OPENHAB_BACKEND_URL`), **Backend-Tokens** (`TTS_TOKEN`, `STT_TOKEN`, `LLM_TOKEN`, `OPENHAB_TOKEN`), `MASTER_TOKEN` (ENV-Fallback) | `.env.master` am Master (gelesen von `master/config.py` / `master/proxy.py`) |
| **Zentrale Satellite-Konfig (Config-Store)** | Alle „weichen" Satellite-Werte: TTS-Modell/-Stimme/-Sprache, STT-Modell/-Sprache, LLM-Modell/-Timeouts, `MUSIC_ROOT`, `BRIDGE_*`, `DEFAULT_IMAGE_URL`, `MEMORY_DB`, Geräte-/Display-Keys, `RULES_FILE`, … – global + per-Satellite-Override (nach MAC) | Master-WebUI (Bereich „Config"); die Satelliten ziehen die Werte per `GET /api/config/apply` beim Heartbeat und wenden sie **ohne Neustart** an |

Konsequenzen:

- Die API-Adressen `TTS_URL`, `STT_URL`, `LLM_URL`, `OPENHAB_URL` sind keine
  einstellbaren Satellite-ENV-Werte: `app/config.py` leitet sie aus
  `MASTER_URL` ab (`…/proxy/tts`, `…/proxy/stt`, `…/proxy/llm`,
  `…/proxy/openhab`).
- Die Backend-Tokens `TTS_TOKEN`, `STT_TOKEN`, `LLM_TOKEN`, `OPENHAB_TOKEN`
  gehören **nur zum Master** (Config-Store „Secrets" bzw. ENV-Fallback);
  der Proxy injiziert sie.
- Der **einzige** Token auf beiden Seiten ist `MASTER_TOKEN` – auf dem
  Satellite als ENV, auf dem Master im Config-Store (Priorität) bzw.
  `.env.master` (Fallback). Gleicher Wert, zwei Orte.
- Identitäts-Keys (`SATELLITE_MAC`, `MASTER_URL`) überschreibt der
  Config-Store nicht.

## ENV-Variablen — Satellite (`.env` des Pi)

Jede Variable wird in `app/config.py` gelesen; die Spalte **Default** zeigt
den in `app/config.py` hinterlegten Wert, der greift, wenn die Variable in
`.env` **nicht** gesetzt ist. Viele „weiche" Werte (TTS-/STT-/LLM-Modell,
Stimme, Sprache, `RULES_FILE`, …) sind dort bewusst **leer**, weil der Master
sie über den **Config-Store** zentral setzt und der Satellite sie per Heartbeat
übernimmt. Die im Live-System tatsächlich eingesetzten Werte stehen in der
Klammer als *Live:*. `.env.example` ist die maschinenlesbare Vorlage.
Alle Variablen, gruppiert:

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
| `STT_URL` | aus `MASTER_URL` abgeleitet | wird aus `MASTER_URL` abgeleitet (`/proxy/stt`); nicht als ENV einstellbar. Echter STT-Endpunkt + `STT_TOKEN`: **nur am Master** (`STT_BACKEND_URL`, Config-Store) |
| `STT_MODEL` | *leer* (Live: `SenseVoiceSmall`) | SenseVoice: `SenseVoiceSmall`, `SenseVoice` (542M), `SenseVoiceLarge`; OpenAI-kompatibel: `whisper-1`, `gpt-4o-transcribe` … |
| `STT_LANGUAGE` | `de` | Sprache fürs Sprachmodell. OpenAI: `auto`, `de`, `en`, …; Qwen: `chinese`, `german`, `english`, `multilingual` |
| `STT_TIMEOUT` | `30` | Timeout in s |

### openHAB & Regeln

| Variable | Default | Bedeutung |
|---|---|---|
| `OPENHAB_URL` | aus `MASTER_URL` abgeleitet | wird aus `MASTER_URL` abgeleitet (`/proxy/openhab`); nicht als ENV einstellbar. Echter openHAB-Endpunkt + `OPENHAB_TOKEN`: **nur am Master** (`OPENHAB_BACKEND_URL`, Config-Store) |
| `OPENHAB_CACHE_TTL` | `300` | Item-Liste cachen (s) |
| `RULES_FILE` | *leer* (Live: `/data/rules.yaml`) | Pfad zur lokalen Regelungsdatei |

### LLM

| Variable | Default | Bedeutung |
|---|---|---|
| `LLM_URL` | aus `MASTER_URL` abgeleitet | wird aus `MASTER_URL` abgeleitet (`/proxy/llm`); nicht als ENV einstellbar. Echter Chat-Endpunkt + `LLM_TOKEN`: **nur am Master** (`LLM_BACKEND_URL`, Config-Store) |
| `LLM_MODEL` | *leer* (Live: `hermes-agent`) | Modell (Hermes-Gateway-Agent mit Terminal) |
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
| `TTS_URL` | aus `MASTER_URL` abgeleitet | wird aus `MASTER_URL` abgeleitet (`/proxy/tts`); nicht als ENV einstellbar. Echter TTS-Endpunkt + `TTS_TOKEN`: **nur am Master** (`TTS_BACKEND_URL`, Config-Store) |
| `TTS_MODEL` | *leer* (Live: `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice`) | TTS-Modell |
| `TTS_VOICE` | *leer* (Live: `Uncle_fu`) | Stimme |
| `TTS_LANGUAGE` | `German` | Sprache; leer = Server-Default |
| `TTS_TIMEOUT` | `60` | Timeout in s |

#### Alternative: Piper TTS

Statt Qwen3-TTS kann der Satellite alternativ einen [Piper](https://github.com/OHF-Voice/piper1-gpl)-Server
nutzen. `TTS_MODEL`/`TTS_VOICE`/`TTS_LANGUAGE` setzt man wie bei Qwen — im
Master-Config-Store (global oder per Satellite):

```env
TTS_MODEL=piper
TTS_VOICE=kitt
TTS_LANGUAGE=de
```

Die Piper-**URL** selbst ist ein Master-Backend: dort in
`.env.master` als `TTS_BACKEND_URL=http://<piper>:5000/v1/audio/speech`
setzen (bzw. Config-Store → „Secrets"/Proxy). Der Satellite selbst bekommt
diese Adresse nicht.

`TTS_LANGUAGE` wird als `language`-Feld in den TTS-Request übergeben
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

## ENV-Variablen — Master (`.env.master`, Master-Container)

Der Master-Container (`voice-satellite-master`, Port 8791) liest seine ENV
aus `.env.master` (via `docker-compose.master.yml`). Gelesen werden sie von
`master/config.py` (Server, Indexer, Limits), `master/satconfig.py`
(Config-Store) und `master/proxy.py` (Backend-Ziele/-Tokens).

### Master-Betrieb (`master/config.py`, `satconfig.py`, `api.py`)

| Variable | Default | Bedeutung |
|---|---|---|
| `API_HOST` | `0.0.0.0` | Bind-Adresse des Master-APIs |
| `API_PORT` | `8791` | HTTP-Port (Proxy + Config-Store + WebUI + Heartbeat + Medien-Stream) |
| `LOG_LEVEL` | `INFO` | Log-Level am Master |
| `MUSIC_ROOT` | `/m` | Musik-Root am Master (CIFS-Mount); `/proxy/music` liefert daraus die Dateien |
| `CACHE_DIR` | `/data/vs-index` | Ordner für Musik-/Poster-Index |
| `INDEX_RESCAN_SEC` | `21600` | Abstand automatischer Re-Scans des Musikordners (s) |
| `VS_CONFIG_DIR` | `<repo>/data` | Config-Store-Verzeichnis; enthält `satellites.json` |
| `MASTER_TOKEN` | leer | ENV-Fallback; **Vorrang hat der Wert im Config-Store** (WebUI „Secrets") – identischer Wert in jeder Satellite-`.env` |
| `AUTH_TOKEN` | leer | *optional*: eigener Zugangs-Token nur für die Proxy-Endpunkte (`/proxy/*`); leer = Proxy offen |

Das Volume `/data` (bzw. `VS_CONFIG_DIR`) hält `satellites.json` – den zentralen
Config-Store. Musik liegt am Master unter `MUSIC_ROOT` (Standard `/m`), nicht
unter einem `DATA_DIR`; eine Variable `DATA_DIR` liest der Master-Code **nicht**.

Die Herzschlag-Parameter sind pro Satellit im Config-Store editierbar; ENV
(`master/manager.py`) liefert nur den Global-Default: `SATELLITE_HEARTBEAT_SEC`
(Standard `60`) und `SATELLITE_MAX_MISSED` (Standard `5`).

`master/manager.py` (Deploy-Fernsteuerung der Pi3) liest zusätzlich:
`SATELLITES` (Semikolon-getrennte Hosts), `SSH_USER` (Standard `pi`), `SSH_PKEY`.

### Backend-Ziele (`master/proxy.py`) — nur am Master

Der Proxy stellt den eigentlichen Upstream fest. Der Satellite sieht davon nichts.

| Proxy-Endpunkt | Ziel (ENV-Priorität) | Built-in-Fallback | Token (Reihenfolge) |
|---|---|---|---|
| `/proxy/tts` | `TTS_BACKEND_URL` → `TTS_URL` | `…:7811/v1/audio/speech` | Store → `TTS_BACKEND_TOKEN` → `TTS_TOKEN` |
| `/proxy/stt` | `STT_BACKEND_URL` → `STT_URL` | *(keiner → 502 wenn ungesetzt)* | Store → `STT_BACKEND_TOKEN` → `STT_TOKEN` |
| `/proxy/llm` | `LLM_BACKEND_URL` → `LLM_URL` | *(keiner → 502 wenn ungesetzt)* | Store → `LLM_BACKEND_TOKEN` → `LLM_TOKEN` |
| `/proxy/openhab` | `OPENHAB_BACKEND_URL` → `OPENHAB_URL` | *(keiner → 502 wenn ungesetzt)* | Store → `OPENHAB_TOKEN` |
| `/proxy/music` | `MUSIC_ROOT` (Dateisystem) | – | `MASTER_TOKEN` |

- **URL vs. Backend-URL (Loop-Guard):** Im Config-Store bezeichnet `TTS_URL`
  (etc.) die **Proxy-Adresse** `…/proxy/tts`, die an die Satelliten verteilt
  wird. Der **Proxy selbst** darf dort sein eigenes Ziel nicht lesen – er würde
  sich sonst selbst anrufen. Darum liest der Proxy sein Ziel ausschließlich aus
  `TTS_BACKEND_URL` (bzw. ENV-`TTS_URL`), **nie** aus dem Store-Wert. Ein
  Config-Store-Wert `*_URL`, der auf den eigenen Master zeigt, wird aktiv
  abgelehnt (`backend_loop_reject`). `*_BACKEND_URL` existiert daher bewusst nur
  in `.env.master`.
- **Token-Injektion:** Der Proxy hängt den Backend-Token selbst als
  `Authorization: Bearer …` an den Upstream. Reihenfolge: Config-Store-Secret
  („Secrets") → ENV `*_BACKEND_TOKEN` → ENV `*_TOKEN`. Auf dem Satelliten
  landen Backend-Tokens **nie**.
- Der Master macht **keine** eigenen TTS/STT/LLM/openHAB-Calls – er ist reiner
  Proxy (außer `POST /index/rebuild` auf den eigenen Indexer).
- **Nicht** überschreiben: `MCP_ENABLED`, `MCP_HOST`, `MCP_PORT`, `WEB_PORT` –
  das sind Satellite-Keys, sie werden vom Master nicht gelesen.

> **Tipp:** Im Config-Store („Secrets", pro Satellit editierbar) lassen sich
> Backend-Tokens/-URLs gezielt pro Satellit überschreiben, ohne `.env.master`
> zu berühren – so fährt der Master mehrere Backends pro Satelliten-Gruppe.

## Satellite-Zugang zum Master (Kurzfassung)

Ein Satellite braucht in der `.env` exakt zwei Master-Keys:

```env
MASTER_URL=http://192.168.1.24:8791   # Basisadresse des Master-APIs
MASTER_TOKEN=<gleicher Wert wie im Master-Config-Store „Secrets">
```

Daraus leitet der Satellite ab: `TTS_URL=$MASTER_URL/proxy/tts`,
`STT_URL=$MASTER_URL/proxy/stt`, `LLM_URL=$MASTER_URL/proxy/llm`,
`OPENHAB_URL=$MASTER_URL/proxy/openhab`, Medien-Stream
`$MASTER_URL/api/satellites/{mac}/stream`. Heartbeat + Config-Pull laufen
über `$MASTER_URL/api/heartbeat` bzw. `/api/config/apply` (Token als
`Authorization: Bearer`). `SATELLITE_MAC` (MAC des Pi, `XX:XX:XX:XX:XX:XX`)
identifiziert den Satellite beim Master; `SAT_NAME` (optional) ist der
Anzeige-/Alias-Name.
