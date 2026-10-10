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

### `GET /events` — SSE
`text/event-stream`, Snapshot alle 2 s: `{state, stats, uptime_s, events}`.
Client dedupliziert per `id:` (Sequenznummer).

## POST-Endpoints

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

## offene Fragen / Edge-Cases
- `repeat` wird auf max. 20 begrenzt; hohere Werte werden gekappt statt abgelehnt.
- `/speak` und `/tone` blockieren nicht — der Speaker ist mit dem
  Pipeline-Audio geteilt; gleichzeitige Ausgaben werden von ALSA
  (`plughw`, SPEAKER_DEVICE) gemischt bzw. sequenziell gespielt.
