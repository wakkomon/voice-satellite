# pidisplay-voice-satellite

Voice-Satellite auf dem Pi 3 (PIDisplay HAT, 2,8" SPI-Display) betreiben.

## Hardware/Adresse
- Host: pi3 (192.168.1.32)
- SSH: hermes / [entfernt]
- Display: 2,8" SPI-Display HAT (fb1, 320x240)
- Mic: USB-Mikrofon
- Speaker: USB-Speaker

## Deployment
```bash
ssh hermes@192.168.1.32 "cd /opt/data/projects/pidisplay-voice/voice-satellite && docker compose pull && docker compose up -d"
```

## Log-Analyse
```bash
ssh hermes@192.168.1.32 "docker logs pidisplay-voice-satellite 2>&1 | tail -100"
ssh hermes@192.168.1.32 "docker compose logs -f --tail=200"
```

## MCP-Schnittstelle (Remote-Steuerung)

Voice-Satellite startet einen MCP-Server (stdio), der folgende Tools
bietet:

### play_tone(name: str)
Einen vordefinierten Klingelton abspielen.
- Verfügbare Töne: boot, wake, end, error, alarm, blip

### show_text(heading: str, body: str = "")
Text auf dem PIDisplay anzeigen.
- heading: Überschrift (max. ~20 Zeichen)
- body: Textkörper, optional

### show_weather()
Wetter-Bild auf dem PIDisplay anzeigen.

### show_music_info(artist: str, title: str)
Musik-Info (Künstler/Title) als Overlay auf dem Wetter-Bild anzeigen.

### set_alarm(minutes: int, label: str = "")
Einen Wecker setzen, der nach n Minuten mit Alarmton abstimmt.

### get_tones()
Alle verfügbaren Töne mit Beschreibung auflisten.

### Konfiguration
Der MCP-Server wird von main.py aus automatisch gestartet (stdio-Modus).
Wenn das `mcp`-Paket nicht installiert ist, startet der Server nicht
(crashfrei mit Warnung).

## Display-Modus (`DISPLAY_MODE` in .env)
- `WEATHER` (Standard): Wetter-Bild mit Text-Overlay
- `BLACK`: Schwarzer Bildschirm (Strom sparen)
- `CUSTOM`: Benutzerdefiniertes Bild (/data/custom_bg.png)

## Töne (`app/tones.py`)
- boot: Zweiton-Harmonie (440+880 Hz) — Systemstart
- wake: 880 Hz, 0,4 s — Codewort erkannt
- end: 660 Hz, 0,3 s — Stille erkannt
- error: 220 Hz, 0,5 s — LLM/Sprachfehler
- alarm: Sirenen-Wechselton (880/440 Hz)
- blip: 880 Hz, 0,1 s — kurzes Feedback

## Debugging
```bash
# Display-Status prüfen
ssh hermes@192.168.1.32 "cat /sys/class/backlight/spi0.0/brightness"

# Audio-Devices
ssh hermes@192.168.1.32 "arecord -l && aplay -l"

# Display-Modus umschalten
ssh hermes@192.168.1.32 "cd /opt/data/projects/pidisplay-voice/voice-satellite && docker compose exec voice-satellite python -c \"from app.config import DISPLAY_MODE; print(DISPLAY_MODE)\""
```

## Bekannte Issues
- BrokenPipeError beim Web-Snapshot: Client trennt sich während
  PNG-Encoding — fix mit try/except in `_send_snapshot()`
- Display-Update-Verzögerung: PIL-Bildkonversion im Worker-Thread kann
  blockieren — Wetter-Rendering läuft jetzt asynchron in
  Hintergrund-Thread, Haupt-Thread wartet nur kurz nach
