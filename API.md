# Voice-Satellite — HTTP-API

Der Satellit (Pi, `192.168.1.32`) hoert auf **Port 8770** (`WEB_PORT`).
Alle Antworten sind JSON (`Content-Type: application/json; charset=utf-8`).
Kurzform: `PI=http://192.168.1.32:8770`

## State-Machine

```
wake --(Wake-Word)--> recording --(VAD-Ende)--> processing --> speaking
 ^                                                            |
 |                  3 s ohne neue Sprache (FOLLOWUP_SEC=3.0)  |
 +----------------------------------------------------------- +
      nach FOLLOWUP_MAX (10) Folgenfragen: Pflichtpause bis zum
      naechsten Wake-Word
```

|| state        | Bedeutung                                        |
||--------------|--------------------------------------------------|
|| `wake`       | lauscht auf das Wake-Word ("Bereit")             |
|| `recording`  | nimmt Sprache auf                                |
|| `processing` | STT + Router/LLM arbeiten                        |
|| `speaking`   | Antwort wird vorgelesen                          |
|| `followup`   | Antwort fertig — Mikrofon bleibt offen, keine    |
||              | erneutes Wake-Word noetig; 3 s Stille -> `wake`  |

## GET-Endpoints

### `GET /` — Debug-UI
Live-Statusseite (HTML, SSE-Stream, zuletzt auch `followup` als Zustand).

### `GET /health`
```json
{"ok": true}
```

### `GET /status`
```json
{
  "state": "followup",
  "stats": {"wakes": 12, "commands": 8, "llm": 27, "errors": 1,
            "followups": 31, "started": 1759564800.0},
  "uptime_s": 3600,
  "events": [{"ts": "20:15:03", "seq": 84, "msg": "llm 2.1s -> Es ist 20:15."}, ...]
}
```

### `GET /state` — Playback-Status (WebUI)
Musik-Status inkl. Shuffle-Zustand für die Web-UI:
```json
{"ok": true, "music": {"playing": true, "title": "..." , "source": "..."},
 "shuffle": true, "shuffle_enable": true, "title": "..."}
```

### `GET /events` — SSE
`text/event-stream`, Snapshot alle 2 s: `{state, stats, uptime_s, events}`.
Client dedupliziert per `id:` (Sequenznummer).

### `GET /index` — Medien-Index-Status
```json
{"files": 68765, "playlists": 8, "building": false, "files_seen": 68765,
 "interval_s": 3600}
```
Der Index wird beim Start aus `/data/media_index.json` geladen (persistiert)
und alle 3600 s automatisch neu gebaut. Es gibt **kein Zeitbudget** — der
Scan läuft immer vollständig. Während eines (Re-)Builds: `building: true`,
`files_seen` zählt die laufend indizierten Dateien mit, und `files` spiegelt
den Live-Stand des laufenden Builds (Endstand erscheint erst mit Abschluss).

## POST-Endpoints

### `POST /index/rebuild` — Medien-Index sofort neu bauen
Entspricht dem Sprachbefehl „Neu indizieren" bzw. dem Web-UI-Button.
Startet den Re-Scan asynchron, die Antwort kommt sofort:
```bash
curl -X POST $PI/index/rebuild
```
Antwort: `{"ok": true, "index": {...}}` (Index-Status zum Startmoment).

### `POST /speak` — Text direkt vorlesen (TTS)
```bash
curl -X POST $PI/speak -H 'Content-Type: application/json' \
     -d '{"text": "Das Abendessen ist fertig."}'
```
Antwort: `{"ok": true, "bytes": 24576}` (202-artig: Antwort kommt sofort,
Audio laeuft asynchron im Hintergrund-Thread). Fehler: `400 text fehlt`,
`502 TTS leer`.

### `POST /tone` — Alarm-/Signalton abspielen
```bash
curl -X POST $PI/tone -H 'Content-Type: application/json' \
     -d '{"tone": "alarm", "repeat": 3}'
```
|| Feld     | Werte                                  | Default |
||----------|----------------------------------------|---------|
|| `tone`   | `alarm` \| `wake` \| `end` \| `boot` \| `error` | `alarm` |
|| `repeat` | 1..20 (nur bei `alarm` sinnvoll; Alarm = 2-Ton-Sirene 880/440 Hz) | 1 |

Antwort: `{"ok": true, "tone": "alarm", "repeat": 3}` — Ton spielt
asynchron. Fehler: `400 unknown tone ...`, `400 repeat muss eine Zahl sein`.

### `POST /intent` — Router/LLM testen (ohne Mikrofon)
```bash
curl -X POST $PI/intent -H 'Content-Type: application/json' \
     -d '{"text": "seit wann laeuft der Container?"}'
```
Antwort: `{"answer": "...", "route": "llm"}` (`route`: Regel-/Intent-Kategorie
des Routers). Antwort wird NICHT gesprochen.

## Musik-Wiedergabe (`/play`)

### Datei-/Ordnerpfade und `/m`

`POST /play {"path": "mp3/artist/album.mp3"}` spielt Dateien ab, deren
Pfade relativ zu `MUSIC_ROOT` sind (Default `/m`).

**Was ist `/m`?**

Ein read-only Bind-Mount des Fileserver-Shares in den Container —
wird beim `docker run` eingebunden (Muster: `-v <host_share>:/m:ro`).
Der konkrete Inhalt richtet sich nach dem jeweils eingebundenen Share
(typische Ordner: `mp3/`, `Record/`, `Bilder/`, `download/` u.a.) —
im Container immer unter `/m/` erreichbar.

**Pfad-Notation:** relative Pfade, z.B. `mp3/Beispiel/track.mp3`.
Playlist-Dateien (.m3u/.m3u8) werden automatisch geparst.
Ordner-Pfade werden per walk gesammelt und sortiert wiedergegeben.

**Umgebung:** überschreibbar per `MUSIC_ROOT=…` in der `.env`. Ohne
Mount (Desktop-Tests) zeigt `/play` 'Musikordner nicht erreichbar'.

### Ordner-Wiedergabe

`POST /play {"path": "mp3/mein_ordner"}` spielt alle Dateien im
Ordner sortiert ab (rekursiv nur erste Ebene, keine Unterordner).

## Beispiel: Sprachnachricht + Alarm aus openHAB

Exec-Binding (Regel) – das Skript `scripts/openhab_voice.sh` auf den
openHAB-Host kopieren (`/etc/openhab/misc/`); es kuemmert sich um
JSON-Escaping und Fehlerbehandlung:
```java
val pi = "192.168.1.32"
executeCommandLine("/etc/openhab/misc/openhab_voice.sh " + pi + " speak 'Achtung, Rauchmelder!'")
executeCommandLine("/etc/openhab/misc/openhab_voice.sh " + pi + " tone alarm 3")
```

Direkt per curl in der Regel (Argumente mit `@@` getrennt, wie im
openHAB-Exec-Binding ueblich; JSON-Quateszen escaped):
```java
executeCommandLine("curl@@-sf@@-X@@POST@@http://192.168.1.32:8770/tone@@-H@@Content-Type: application/json@@-d@@{\"tone\":\"alarm\",\"repeat\":3}")
```

Shell-Variante mit JSON-Escaping und Fehlerbehandlung:
`scripts/openhab_voice.sh` (im Repo; z.B. via Exec-Handler oder Cron):
```bash
./scripts/openhab_voice.sh speak 'Achtung, Rauchmelder!'
./scripts/openhab_voice.sh tone alarm 3
```

## Zentrales Gedaechtnis (Master)

Im Master-Modus liegt das Gedaechtnis ausschliesslich am Master
(`CHAT_MEMORY_DB`, Default `<CHAT_DIR>/memory.db`), pro Geraet getrennt —
die Geraete selbst (`app/memory.py`) schreiben im Master-Modus nichts
mehr und senden nur API-Aufrufe (Beweis: Fakten + Verlauf sind im
Master-WebUI unter Konfiguration -> Gedächtnis editierbar).

| Endpoint | Berechtigung | Beschreibung |
|---|---|---|
| `POST /api/memory/sat/exchange` | Bearer MASTER_TOKEN | `{mac, channel, user, assistant}` — Kurzzeit-Austausch des Geraets speichern |
| `POST /api/memory/sat/extract` | Bearer MASTER_TOKEN | `{mac, user, assistant, model}` — Fakten extrahieren (der Master macht es, entlastet den Pi) |
| `GET /api/memory/sat/facts?mac=` | Bearer MASTER_TOKEN | Fakten des Geraets |
| `GET /api/memory/sat/history?mac=&channel=` | Bearer MASTER_TOKEN | Kurzzeit-Verlauf des Geraets (`channel` voice/web) |
| `GET /api/memory` | Admin (Web-Login oder MASTER_TOKEN) | Snapshot: Fakten + Verlauf aller Geraete ("web" + Registry) |
| `PUT /api/memory/facts` | Admin | `{key, facts: [...]}` — Faktenliste eines Geraets ersetzen |
| `POST /api/memory/history/clear` | Admin | `{key, channel?}` — Verlauf loeschen (Fakten bleiben) |

`key` = kanonischer Registry-Key (Host-IP oder MAC); `web` = Browser-Chat.
Die alte globale Fakten-Tabelle wird beim naechsten Start einmal migriert
(Bestehende Fakten -> `web`).

```bash
# Fakten eines Geraets setzen (Tooling mit MASTER_TOKEN)
curl -X PUT http://MASTER:8791/api/memory/facts \
  -H "Authorization: Bearer ***" -H "Content-Type: application/json" \
  -d '{"key":"192.168.82.32","facts":["Wohnt in der Rosenstrasse 12"]}'

# Gedaechtnis aller Geraete anzeigen
curl -s http://MASTER:8791/api/memory -H "Authorization: Bearer ***" | jq
```

## offene Fragen / Edge-Cases

- `repeat` wird auf max. 20 begrenzt; hohere Werte werden gekappt statt abgelehnt.
- `/speak` und `/tone` blockieren nicht — der Speaker ist mit dem
  Pipeline-Audio geteilt; gleichzeitige Ausgaben werden von ALSA
  (`plughw`, SPEAKER_DEVICE) gemischt bzw. sequenziell gespielt.


---

## Master-Steuerung (openHAB / curl)

Außerhalb des einzelnen Satelliten kann der **Master** alle (oder einen)
Satelliten steuern – ideal, wenn openHAB auf dem Master läuft oder vom Master
aus erreichbar ist. Der Master leitet die Befehle auf den Web-Dienst des
jeweiligen Satelliten weiter (Port aus Heartbeat, sonst `8770`).

`MASTER=http://<master-ip>:<master-port>` · `TOKEN=<MASTER_TOKEN>`

Alle Steuer-Endpunkte unten akzeptieren `Authorization: Bearer <TOKEN>`
(Master-Token, aus der WebUI „Secrets"); die WebUI-Buttons brauchen einen
Web-Login (Rolle Admin).

### `POST /api/broadcast` — Durchsage (wichtigster Fall)
{text, sat?, vol?} → spricht ohne Wake-Word/LLM. `sat` leer = **alle**
bekannten, online Satelliten (Haus-Durchsage); sonst MAC/IP/Hostnamen.
```bash
# Alle Räume:
curl -s -X POST $MASTER/api/broadcast \
     -H "Authorization: Bearer $TOKEN" \
     -d '{"text":"Alle ans Esszimmer, bitte!"}'
# Ein Raum:
curl -s -X POST $MASTER/api/broadcast \
     -H "Authorization: Bearer ***" \
     -d '{"sat":"wohnzimmer","text":"Essen ist fertig"}'
```
openHAB-Regel (Exec-Binding) für Durchsage + Bild:
```java
val master = "http://192.168.1.24:8791"
val token  = "<MASTER_TOKEN>"
// Rauchmelder: Durchsage an alle + Bild der Rauchmelder-Kamera 2 Minuten
executeCommandLine("curl@@-sf@@-X@@POST@@" + master + "/api/broadcast@@-H@@Authorization: Bearer " + token + "@@-d@@{\"text\":\"Achtung, Rauchmelder!\"}")
executeCommandLine("curl@@-sf@@-X@@POST@@" + master + "/api/image@@-H@@Authorization: Bearer " + token + "@@-d@@{\"url\":\"http://192.168.1.33/kamera/rauchmelder/snapshot.jpg\",\"ttl\":120}")
```

### Web-Chat: Antwort an Zielgerät (`out`)
Die Chat-WebUI (`/chat.html`) und `POST /api/chat/send` nehmen ein
**Zielgerät** für die *Antwort* entgegen — unabhängig vom Eingabegerät
(Quellgerät). `out` bestimmt, **wo** die LLM-Antwort gesprochen wird:

- `out` leer / `"web"` / `"browser"` → Browser-Ausgabe: der Master
  synthetisiert die Antwort als Aufnahme (`rec`), die im Chat abspielbar ist.
- `out` = Satellit (MAC/IP/Hostnamen) → der ausgewählte Satellit spricht die
  Antwort über `/speak` (`wait=false`, **feuer-und-vergiss** — die
  Chat-Antwort blockiert nicht, solange das Gerät TTS braucht). Antwort-Text
  und Zielgerät erscheinen als Ausgabetext im Chat.

- `out` = "all" (auch "*") → **alle** bekannten, online
  Satelliten gleichzeitig (wie `POST /api/broadcast` ohne `sat` —
  Hausdurchsage). Die Chat-UI bietet dafür „📢📢 Alle Geräte“ in der
  Auswahl „Abspielen auf“.

`POST /api/chat/send` akzeptiert:
`{text, mac?, speak?, out?}` → Antwort `{answer, rec, ts, out, broadcast}`
(`broadcast` = `"started"`, wenn die Antwort an einen Satelliten gebroadcastet
wurde, sonst `null`). Die ausgewählte `out` wird pro `assistant`-Eintrag im
Chat-Store persistiert (`out`), die UI zeigt sie als Zielgerät-Badge
(`🔊` Browser, `🔊📢` + „· Broadcast“ bei Satelliten).

```bash
# Antwort in den Browser (Aufnahme) — out leer:
curl -s -X POST $MASTER/api/chat/send \
     -H "Authorization: Bearer ***" \
     -d '{"text":"Wie ist die Zeit?"}'
# Antwort laut am Zielgerät (display01):
curl -s -X POST $MASTER/api/chat/send \
     -H "Authorization: Bearer ***" \
     -d '{"text":"Wie ist die Zeit?","out":"display01"}'
# Durchsage an alle onlineen Geräte (Broadcast an alle):
curl -s -X POST $MASTER/api/chat/send \
     -H "Authorization: Bearer <MASTER_TOKEN>" \
     -d '{"text":"Das Essen ist fertig.","out":"all"}'
```

> Hinweis: Beim Satelliten-Ziel macht der Master **keine** zweite TTS-Aufnahme
> (die spricht das Gerät selbst). `speak=false` unterdrückt die Sprachausgabe
> vollständig (nur Text).

### Lautstärke
```bash
# Setzen (0-100) / stummschalten:
curl -s -X POST $MASTER/api/satellite/wohnzimmer/volume \
     -H "Authorization: Bearer $TOKEN" -d '{"volume":40}'
curl -s -X POST $MASTER/api/satellite/wohnzimmer/volume \
     -H "Authorization: Bearer $TOKEN" -d '{"volume":0,"muted":true}'
# Abfragen:
curl -s $MASTER/api/satellite/wohnzimmer/volume -H "Authorization: Bearer $TOKEN"
```

### Audio-Ausgabe (Gerät)
```bash
# Umschalten:
curl -s -X POST $MASTER/api/satellite/wohnzimmer/output \
     -H "Authorization: Bearer $TOKEN" -d '{"out":"plughw:1,0"}'
# Aktuelle + verfügbare Geräte:
curl -s $MASTER/api/satellite/wohnzimmer/output -H "Authorization: Bearer $TOKEN"
```

### `POST /api/image` — Bild an die Geräte pushen
`{sat?, url, ttl?, text?, src?}` → Apps (Android) zeigen das Bild für
`ttl` Sekunden (Default **60**, Range 5-600) groß an; `text` (optional)
wird zusätzlich gesprochen. `sat` leer = alle online App-Geräte, sonst
MAC/IP/Hostnamen. Deaktivierte Geräte werden übersprungen. Pi-Satelliten
haben (noch) keine Bildanzeige und werden im `targets`-Report gemeldet.
```bash
# Wetter- oder Kamera-Bild für 1 Minute an ein Tablet:
curl -s -X POST $MASTER/api/image \
     -H "Authorization: Bearer ***" \
     -d '{"sat":"badezimmer","url":"http://192.168.1.33/kamera/snapshot.jpg","ttl":60,"text":"Kuehlschrank steht offen"}'
# An alle App-Geräte:
curl -s -X POST $MASTER/api/image \
     -H "Authorization: Bearer ***" \
     -d '{"url":"http://192.168.1.33/kamera/snapshot.jpg"}'
```
Antwort: `{"ok": true, "targets": [{"sat": "<key>", "ok": true, "detail": "vorgemerkt …"}]}`.

### `POST /api/satellite/{key}/disable` · `/enable` — Satellit an-/abschalten
Key = MAC/IP/Hostnamen/custom-Name (Admin bzw. Master-Token). Ein
**deaktivierter** Satellit (egal ob Pi oder App):
- bekommt **keine Pushes** mehr (Broadcast, Direktnachricht, Chat-Ausgabe,
  Bild-Push, MCP-Steuerung — Passthrough antwortet mit `target disabled`),
- kann **nichts mehr an den Master senden** (Heartbeat, Chat-Push, DM
  antworten mit `403 {"error": "deaktiviert"}`) und fällt im Dashboard
  auf „offline“.

Persistiert in `sat_disabled.json` (Config-Volume, überlebt Rebuilds).
```bash
curl -s -X POST $MASTER/api/satellite/guest-tablet/disable -H "Authorization: Bearer ***"
curl -s -X POST $MASTER/api/satellite/guest-tablet/enable  -H "Authorization: Bearer ***"
```
WebUI: in der Satelliten-Tabelle Button „Deaktivieren"/„Aktivieren“
(nur Admin-Rolle).
```java
// openHAB-Beispiel: Gast-Tablet am Wochenende abschalten
executeCommandLine("curl@@-sf@@-X@@POST@@http://192.168.1.24:8791/api/satellite/guest-tablet/disable@@-H@@Authorization: Bearer <TOKEN>")
// ...und wieder aktivieren
executeCommandLine("curl@@-sf@@-X@@POST@@http://192.168.1.24:8791/api/satellite/guest-tablet/enable@@-H@@Authorization: Bearer <TOKEN>")
```

### `POST /api/satellite/{mac}/dm` — Geräte-zu-Gerät-Direktnachricht
`{to, text}` (Token des **sendenden** Geräts) → reine Textnachricht
(kein LLM, kein TTS) in die Pull-Warteschlange des Ziels; das Ziel zeigt
sie mit der Quelle („Von: …") im Chat an. `to` = MAC/IP/Hostnamen oder
`all` (alle anderen online Geräte).
```bash
curl -s -X POST $MASTER/api/satellite/<mac-des-senders>/dm \
     -H "Authorization: Bearer <TOKEN-DES-SENDERS>" \
     -d '{"to":"wohnzimmer","text":"Abendessen in 20 Minuten"}'
```

### MCP-Endpunkt am Master (zentrale Steuerung)
Der Master bündelt die MCP-Tools **aller Satelliten** (Display, Töne,
Wecker — Durchreichung, `target`/Pfad bestimmt den Empfänger) **und**
eigene Master-Tools für die zentralen Funktionen. Ein einziger
MCP-Client (z.B. Hermes) steuert so das ganze Haus.

- `GET  /mcp/tools` — alle Tools (Master-Tools: `mac: "master"`);
- `POST /mcp` — JSON-RPC 2.0; `method` = Tool-Name.

**Master-Tools:**

| Tool | Parameter | Wirkung |
|---|---|---|
| `list_satellites` | – | Status/IP/Aktivität/Version aller Satelliten |
| `broadcast` | `text`, `sat?`, `wait?`, `vol?` | TTS-Durchsage ohne Wake/LLM; `sat` leer = alle |
| `chat_send` | `text`, `out?`, `speak?`, `mac?` | LLM-Chat (Intent-Bridge), Antwort in Browser/am Ziel/`all` |
| `send_dm` | `to`, `text` | Direktnachricht (reiner Text) an ein Gerät/`all` |
| `push_image` | `url`, `sat?`, `ttl?`, `text?` | Bild an App-Geräte (Default 60 s) |
| `set_volume` / `get_volume` | `sat`, `volume?`, `muted?` | Lautstärke |
| `set_output` / `get_output` | `sat`, `out?` | Audio-Ausgabe umschalten/lesen |
| `disable_satellite` / `enable_satellite` | `sat` | Satellit an-/abschalten (persistiert) |
| `set_satellite_name` | `sat`, `name` | Anzeigename |
| `satellite_restart` / `satellite_pause` / `satellite_unpause` | `sat` | Container-Aktion (SSH+Docker) |

**Satellite-Tools** (Durchreichung, `target` = MAC/IP/Name): `play_tone`,
`show_text`, `show_weather`, `show_music_info`, `set_alarm`, `get_tones`.
```bash
# Master-Tool: Hausdurchsage
curl -X POST http://192.168.1.24:8791/mcp \
  -H "Authorization: Bearer ***" \
  -d '{"jsonrpc":"2.0","id":1,"method":"broadcast","params":{"text":"Alle ans Esszimmer, bitte!"}}'

# Master-Tool: Bild an ein Tablet (1 Minute)
curl -X POST http://192.168.1.24:8791/mcp \
  -H "Authorization: Bearer ***" \
  -d '{"jsonrpc":"2.0","id":2,"method":"push_image","params":{"sat":"wohnzimmer","url":"http://192.168.1.33/kamera/snapshot.jpg","ttl":60}}'

# Satellite-Tool: Text auf dem Display (target = konkretes Gerät)
curl -X POST http://192.168.1.24:8791/mcp \
  -H "Authorization: Bearer ***" \
  -d '{"jsonrpc":"2.0","id":3,"method":"show_text","target":"192.168.1.32","params":{"heading":"Hallo","body":"Welt"}}'
```
Deaktivierte Satelliten antworten im Passthrough mit `-32003 target
disabled` und tauchen nicht mehr in `GET /mcp/tools` auf.

### Automatische Versionsnummer
`scripts/gen_version.py` leitet bei jedem Build aus dem letzten Git-Tag
(`vMA.MI.PA`) + Anzahl der Commits seitdem die laufende Patch-Nummer ab
(`v2.0.0` + 12 Commits → `2.0.12`) und schreibt sie in `app/_version.py` /
`master/_version.py` (Build-Artefakt, in `.gitignore`). `deploy.sh`,
`deploy-master.sh` und `scripts/release.sh` rufen es automatisch auf.
