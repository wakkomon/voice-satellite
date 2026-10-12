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

# 0a. Deploy-Sperre (flock): parallele Doppel-Läufe (z.B. versehentlich zwei
#     Terminals/SSH-Sessions) würden sonst denselben Container-Namen beantragen
#     -> Namens-Konflikt beim docker run nach dem zweiten Build.
#     Zweiter Lauf wartet, bis der erste fertig ist, und bricht dann
#     spurlos ab ('deploy laeuft bereits') – der erste liefert das Ergebnis.
LOCK_FILE="$(pwd)/.deploy.lock"
if [ "${DEPLOY_GUARD:-0}" != "1" ]; then
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "Deploy läuft bereits (Sperre: $LOCK_FILE) – zweiter Lauf abgebrochen."
    echo "Der laufende Deploy läuft weiter; Ergebnis dort abwarten."
    exit 0
  fi
fi

echo "=== Voice-Satellite Deploy ==="

# 0. .env vorhanden? No-Overwrite-Prüfung
if [ ! -f .env ]; then
  echo "Fehler: .env fehlt."
  echo "  python3 scripts/make_env.py && nano .env   # dann ./deploy.sh erneut"
  exit 1
fi

# 0b. portainer_agent stoppen: Er erzeugt veth-Interfaces, über die
#      connman/dhcpcd eine Default-Route mit Metrik 0 setzt (überschreibt
#      wlan0, Metrik 100) – das Docker-Build-Netzwerk bricht ab.
#      Auf display02 zusätzlich: /etc/connman/main.conf
#      NetworkInterfaceBlacklist = ...,veth (siehe docs/connman-docker.md).
#      Nach dem Build wird er wieder gestartet.
echo "[0b] portainer_agent temporär stoppen (Build-Netzwerk-Fix)..."
$DOCKER stop portainer_agent >/dev/null 2>&1 || true

# No-Overwrite-Guard: Prüft kritische Keys gegen App-Defaults.
# Nur KEYS mit abweichendem Wert (aber in .env gesetzt) werden angezeigt.
# Abweichende Werte bleiben UNBERÜHRT – Deploy nimmt sie 1:1 mit.
# (Default-Extraktion per Python, nicht grep -oP: BusyBox/Alpine-Hosts
#  haben keine PCRE, und `set -e` würde den Deploy sonst hier abbrechen.)
echo "[0/6] No-Overwrite-Prüfung: .env-Keys gegen App-Defaults..."
DRIFT_FOUND=0
while IFS=$'\t' read -r key default; do
  [ -z "$key" ] && continue
  # || true: grep ohne Treffer liefert rc=1 – unter 'set -euo pipefail'
  # würde das Assignement den Deploy hier sonst abbrechen (leere Werte sind
  # gewuenscht: Key fehlt in .env -> kein Drift).
  env_val=$(grep "^${key}=" .env 2>/dev/null | cut -d= -f2- | sed 's/^"//;s/\"$//' || true)
  # Nur anzeigen, wenn Key in .env gesetzt UND Wert != Default
  if [ -n "$env_val" ] && [ "$env_val" != "$default" ]; then
    if [ "$DRIFT_FOUND" -eq 0 ]; then
      echo "  ⚠ Abweichende Werte (werden NICHT überschrieben):"
      DRIFT_FOUND=1
    fi
    echo "    $key: LIVE=$env_val  vs  DEFAULT=$default"
  fi
done < <(python3 -c "
import re, sys
text = open('app/config.py', encoding='utf-8').read()
for m in re.finditer(r'^([A-Z][A-Z0-9_]*)\s*=\s*(?:float\(|int\()?\s*_env\(\s*[\"\x27]([A-Z0-9_]+)[\"\x27]\s*,\s*[\"\x27]([^\"\x27]*)[\"\x27]', text, re.M):
    if m.group(1) == m.group(2):
        sys.stdout.write(m.group(1) + '\t' + m.group(3) + '\n')
")
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
echo "[2/6] Alten Container entfernen...";
$DOCKER stop voice-satellite >/dev/null 2>&1 || true
$DOCKER rm -f voice-satellite 2>/dev/null || true

# 3. Musik-Bind-Mount vorhanden? (Playlists, Radio, Kamera-M3U)
if [ -d /media/private ]; then
  MUSIC_MOUNT="/media/private:/m:ro"
else
  echo "Hinweis: /media/private nicht vorhanden – Musik/Kamera-Playlists liegen"
  echo "vorerst nicht im Container. Volume mounten (SMB: mount.cifs), dann neu deployen."
  MUSIC_MOUNT=""
fi

# MCP-Port aus .env lesen (Default: 1 / 8780 – entspricht App-Defaults in app/config.py).
# MCP_ENABLED=0 -> kein Port-Mapping.
MCP_ENABLED_VAL=$(grep "^MCP_ENABLED=" .env 2>/dev/null | cut -d= -f2- | sed 's/^"//;s/"$//' || true)
MCP_PORT_VAL=$(grep "^MCP_PORT=" .env 2>/dev/null | cut -d= -f2- | sed 's/^"//;s/"$//' || true)
MCP_ENABLED_VAL=${MCP_ENABLED_VAL:-1}
MCP_PORT_VAL=${MCP_PORT_VAL:-8780}
if [ "$MCP_ENABLED_VAL" = "1" ]; then
  MCP_PORT_FLAG="-p ${MCP_PORT_VAL}:${MCP_PORT_VAL}"
  echo "[3b/6] MCP-Port ${MCP_PORT_VAL} wird gemappt"
else
  MCP_PORT_FLAG=""
  echo "[3b/6] MCP deaktiviert (MCP_ENABLED=0) – kein Port-Mapping"
fi

# 4. Image neu bauen (--no-cache: Source-Änderungen landen garantiert im Image,
#    kein stehengebliebener Build-Cache mit altem Code)
#    Offline-Modus: VS_IMAGE_TAR=<tar.gz> → docker load statt build
#    (für Geräte, deren Build-Netzwerk während langer apt/pip-Steps abbricht;
#    z.B. Pi 2 display02: dhcpcd-Default-Route über veth bricht Docker-Build-Net
#    nach ein paar Minuten weg. Image von display01 bauen + save/load übertragen.)
if [ -n "${VS_IMAGE_TAR:-}" ]; then
  if [ ! -f "$VS_IMAGE_TAR" ]; then
    echo "Fehler: VS_IMAGE_TAR=$VS_IMAGE_TAR existiert nicht."
    exit 1
  fi
  echo "[3/6] Docker-Image laden aus $VS_IMAGE_TAR (offline)..."
  $DOCKER load -i "$VS_IMAGE_TAR"
else
  echo "[3/6] Docker-Image neu bauen (ohne Cache)..."
  $DOCKER build --no-cache -t voice-satellite:latest .
fi

# 5. Container starten (Name bleibt voice-satellite)
echo "[4/6] Container starten..."
$DOCKER run -d --name voice-satellite \
  --restart unless-stopped \
  --device /dev/snd \
  --device /dev/fb1 \
  --group-add 29 --group-add 44 \
  -p 8770:8770 \
  ${MCP_PORT_FLAG} \
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

# 0c. portainer_agent wieder starten (war vor Build gestoppt)
echo "[0c] portainer_agent wieder starten..."
$DOCKER start portainer_agent >/dev/null 2>&1 || true
