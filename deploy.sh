#!/usr/bin/env bash
# deploy.sh — Voice-Satellite deployen/aktualisieren (idempotent, frisches Gerät & Update)
#
# Frisches Gerät:
#   git clone https://github.com/wakkomon/voice-satellite.git
#   cd voice-satellite
#   python3 scripts/make_env.py   # erzeugt .env (nur fehlende Keys, nie überschrieben)
#   nano .env                     # Secrets/Tokens eintragen (OPENHAB_TOKEN, TTS_TOKEN, LLM_TOKEN, RTSP_*)
#   ./deploy.sh
#
# Update:
#   git pull && ./deploy.sh
#
# ENV-Variablen bleiben über Deploy-Zyklen erhalten: .env wird NICHT angefasst.
set -euo pipefail
cd "$(dirname "$0")"

echo "=== Voice-Satellite Deploy ==="

# 0. .env vorhanden?
if [ ! -f .env ]; then
  echo "Fehler: .env fehlt."
  echo "  python3 scripts/make_env.py && nano .env   # dann ./deploy.sh erneut"
  exit 1
fi

# 1. Daten-Volume erhalten (nie löschen!)
if ! docker volume inspect voice-satellite-data >/dev/null 2>&1; then
  echo "[1/6] Daten-Volume anlegen: voice-satellite-data"
  docker volume create voice-satellite-data
else
  echo "[1/6] Daten-Volume voice-satellite-data vorhanden (wird nicht angefasst)"
fi

# 2. Alten Container entfernen (Volume bleibt erhalten)
echo "[2/6] Alten Container entfernen..."
docker rm -f voice-satellite 2>/dev/null || true

# 3. Musik-Bind-Mount vorhanden? (Playlists, Radio, Kamera-M3U)
if [ -d /media/private ]; then
  MUSIC_MOUNT="/media/private:/m:ro"
else
  echo "Hinweis: /media/private nicht vorhanden – Musik/Kamera-Playlists liegen"
  echo "vorerst nicht im Container. Volume mounten (SMB: mount.cifs), dann neu deployen."
  MUSIC_MOUNT=""
fi

# 4. Image neu bauen (--no-cache: Source-Änderungen landen garantiert im Image,
#    kein stehengebliebener Build-Cache mit altem Code)
echo "[3/6] Docker-Image neu bauen (ohne Cache)..."
docker build --no-cache -t voice-satellite:latest .

# 5. Container starten (Name bleibt voice-satellite)
echo "[4/6] Container starten..."
docker run -d --name voice-satellite \
  --restart unless-stopped \
  --device /dev/snd \
  --device /dev/fb1 \
  --group-add 29 --group-add 44 \
  -p 8770:8770 \
  -v voice-satellite-data:/data \
  ${MUSIC_MOUNT:+-v "$MUSIC_MOUNT"} \
  --env-file "$(pwd)/.env" \
  voice-satellite:latest

# 6. Health-Check + Status
echo "[5/6] Warten auf Health-Check (20 s)..."
sleep 20
docker ps --filter name=voice-satellite --format '  {{.Names}}  {{.Status}}'
echo "[6/6] Logs (letzte 5 Zeilen):"
docker logs voice-satellite 2>&1 | tail -n 5

echo ""
echo "=== Deploy abgeschlossen ==="
echo "Version (Container): $(docker exec voice-satellite python3 -c 'from app import __version__; print(__version__)' 2>/dev/null || echo 'n/a')"
echo "Web-UI: http://<pi-ip>:8770  ·  Logs: docker logs -f voice-satellite"
