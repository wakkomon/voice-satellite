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

# Docker-Berechtigung: User in docker-Gruppe -> direkt, sonst via sudo.
if docker version >/dev/null 2>&1; then
  DOCKER="docker"
else
  DOCKER="sudo docker"
fi

echo "=== Voice-Satellite Deploy ==="

# 0. .env vorhanden? No-Overwrite-Prüfung
if [ ! -f .env ]; then
  echo "Fehler: .env fehlt."
  echo "  python3 scripts/make_env.py && nano .env   # dann ./deploy.sh erneut"
  exit 1
fi

# No-Overwrite-Guard: Prüft kritische Keys gegen App-Defaults.
# Nur KEYS mit abweichendem Wert (aber in .env gesetzt) werden angezeigt.
# Abweichende Werte bleiben UNBERÜHRT – Deploy nimmt sie 1:1 mit.
echo "[0/6] No-Overwrite-Prüfung: .env-Keys gegen App-Defaults..."
DRIFT_FOUND=0
# App-Defaults aus app/config.py extrahieren (Zeilen wie: KEY = _env("KEY", "default"))
while IFS= read -r line; do
  key=$(echo "$line" | grep -oP '_env\s*\(\s*"\K[A-Z0-9_]+')
  default=$(echo "$line" | grep -oP '_env\s*\([^,]+,\s*"\K[^"]+')
  if [ -n "$key" ] && [ -n "$default" ]; then
    env_val=$(grep "^${key}=" .env 2>/dev/null | cut -d= -f2- | sed 's/^"//;s/"$//')
    # Nur anzeigen, wenn Key in .env gesetzt UND Wert != Default
    if [ -n "$env_val" ] && [ "$env_val" != "$default" ]; then
      if [ "$DRIFT_FOUND" -eq 0 ]; then
        echo "  ⚠ Abweichende Werte (werden NICHT überschrieben):"
        DRIFT_FOUND=1
      fi
      echo "    $key: LIVE=$env_val  vs  DEFAULT=$default"
    fi
  fi
done < <(grep '_env(' app/config.py 2>/dev/null)
if [ "$DRIFT_FOUND" -eq 1 ]; then
  echo "  ✅ Bestehende .env-Werte bleiben UNVERÄNDERT."
else
  echo "  ✅ Alle .env-Keys entsprechen App-Defaults."
fi

# 1. Daten-Volume erhalten (nie löschen!)
if ! $DOCKER volume inspect voice-satellite-data >/dev/null 2>&1; then
  echo "[1/6] Daten-Volume anlegen: voice-satellite-data"
  $DOCKER volume create voice-satellite-data
else
  echo "[1/6] Daten-Volume voice-satellite-data vorhanden (wird nicht angefasst)"
fi

# 2. Alten Container entfernen (Volume bleibt erhalten)
echo "[2/6] Alten Container entfernen..."
$DOCKER rm -f voice-satellite 2>/dev/null || true

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
$DOCKER build --no-cache -t voice-satellite:latest .

# 5. Container starten (Name bleibt voice-satellite)
echo "[4/6] Container starten..."
$DOCKER run -d --name voice-satellite \
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
$DOCKER ps --filter name=voice-satellite --format '  {{.Names}}  {{.Status}}'
echo "[6/6] Logs (letzte 5 Zeilen):"
$DOCKER logs voice-satellite 2>&1 | tail -n 5

echo ""
echo "=== Deploy abgeschlossen ==="
echo "Version (Container): $($DOCKER exec voice-satellite python3 -c 'from app import __version__; print(__version__)' 2>/dev/null || echo 'n/a')"
echo "Web-UI: http://<pi-ip>:8770  ·  Logs: docker logs -f voice-satellite"
