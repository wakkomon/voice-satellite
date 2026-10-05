# Voice-Satellite

Sprachassistent auf einem Raspberry Pi (armv7l) im Docker-Container —
ersetzt Rhasspy + Supervisor + MQTT-Bus als Prozessrahmen.

**Pipeline:** Wake-Word (Porcupine) → Aufnahme + VAD → Whisper-STT →
Router (openHAB-Befehle | lokales LLM) → Piper-TTS → aplay.
Parallel erscheinen Frage und Antwort als Text auf dem
Framebuffer-Display (`/dev/fb1`, 480×320) und der Status live im
Debug-UI (`http://<pi>:8770/`).

## Features

* **Follow-up-Listening** — nach einer Antwort bleibt das Mikrofon offen
  (kein erneutes Codewort); erst **3 s Stille** (`FOLLOWUP_SEC`) beenden
  die Session, nach `FOLLOWUP_MAX` (10) Folgenfragen Pflichtpause.
* **Framebuffer-Display** — Antworten als Text auf `/dev/fb1`,
  implemented ohne Pillow/fbi: direkter RGB565-Write, 5×7-Font,
  Auto-Paginierung (`FB_PAGE_SEC=5`), Auto-Blackout (`FB_TIMEOUT=60`).
* ** Alarm-/Signaltoene** — `POST /tone` (Sirene 880/440 Hz), dazu
  wake/end/boot/error-Cues über den Speaker.
* **15 Wake-Woerter** — fertige `.ppn`-Modelle liegen in `wake/`
  (computer, alexa, jarvis, hey google, …); Auswahl per `WAKE_PPN` +
  `WAKE_WORD` in der `.env`.
* **HTTP-API** — `/speak`, `/tone`, `/intent`, `/status`, `/health`,
  SSE-`/events`; Details in [API.md](API.md).
* **openHAB-Anbindung** — Beispielskript `scripts/openhab_voice.sh`.

## Deployment (Pi)

Auf dem Pi (Docker 20.10 ohne Compose) mit `docker run` — wichtig:
`/dev/fb1` durchreichen und die video-Gruppe (GID 44) hinzufügen:

```bash
cd /home/pi/voice-satellite
sudo docker build -t voice-satellite:0.1.0 .
sudo docker rm -f voice-satellite
sudo docker run -d --name voice-satellite --restart unless-stopped \
  --device /dev/snd --device /dev/fb1 \
  --group-add audio --group-add 44 \
  -p 8770:8770 -v voice-satellite-data:/data --env-file .env \
  voice-satellite:0.1.0
```

Wo Compose verfügbar ist, genügt:

```bash
docker compose up -d --build    # compose-Datei enthält /dev/fb1 + group_add 29/44
```

Voraussetzungen: `.env` (siehe `.env.example`; AccessKeys nur dort, nie
committen), Mikrofon+Speaker in `MIC_DEVICE`/`SPEAKER_DEVICE`,
Display an `/dev/fb1`. Der Container hoert NICHT privileged — durchgereicht
werden nur `/dev/snd/*`-Audio und `/dev/fb1` (video-Gruppe per `group_add`).

## Entwicklung

```
app/
  main.py       Pipeline-Hauptloop (wake→rec→stt→router→tts→display, Follow-up)
  audio.py      arecord-MicStream, Recorder + VAD (capture, capture_followup)
  wake.py       Porcupine-Wrapper (15 ppn in wake/)
  stt.py        Whisper (lokal/HTTP)
  router.py     Intent-Regeln (openHAB) vs. LLM-Antworten
  tts.py        Piper-Synthese
  tones.py      Sinus-Cues + 2-Ton-Sirene
  display.py    /dev/fb1 RGB565-Renderer (kein Pillow)
  web.py        HTTP-Server (Debug-UI, /speak /tone /intent /status)
  http.py       HTTP-Client-Helper
  memory.py     SQLite-Gedaechtnis
```

Tests/Smoke lokal: `python3 -m py_compile app/*.py`; Display und Web
laufen ohne echte Hardware (Device-Checks intern, Fake-Framebuffer moeglich).
