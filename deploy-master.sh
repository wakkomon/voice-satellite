#!/usr/bin/env bash
# Deploy Voice-Satellite Master auf dev-server (2.0.0alpha)
# Usage: ./deploy-master.sh [ssh_host]
#
# ssh_host optional, sonst 192.168.1.24 (dev-server)
# Das Skript kopiert die Master-Pakete, baut das Docker-Image
# und startet den Container über docker-compose.
set -euo pipefail

SSH_HOST="${1:-192.168.1.24}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="voice-satellite"
VERSION="2.0.0alpha"

echo "=== Voice-Satellite Master Deploy v${VERSION} ==="
echo "Ziel: ${SSH_HOST}"

# ── Build Docker Image lokal ───────────────────────────────────
echo "Bau Master-Docker-Image..."
docker build -t "voice-satellite-master:${VERSION}" \
    -f "${SCRIPT_DIR}/Dockerfile.master" \
    "${SCRIPT_DIR}" || {
    echo "FEHLER: Build fehlgeschlagen"
    exit 1
}
echo "Image gebaut: voice-satellite-master:${VERSION}"

# ── Dateien auf dev-server kopieren ───────────────────────────
# .env.master.example -> .env.master
# docker-compose.master.yml -> .env.master (wenn vorhanden)
# master/ package (bereits im Image, nur .env + compose)

REMOTE_DIR="/home/${SSH_USER:-hermes}/projects/${PROJECT}"

echo "Kopiere Konfig auf ${SSH_HOST}:${REMOTE_DIR} ..."
scp "${SCRIPT_DIR}/.env.master.example" \
    "${SSH_HOST}:${REMOTE_DIR}/.env.master" 2>/dev/null || true

scp "${SCRIPT_DIR}/docker-compose.master.yml" \
    "${SSH_HOST}:${REMOTE_DIR}/docker-compose.master.yml" 2>/dev/null || true

echo "Starte Container über docker-compose..."
ssh "${SSH_HOST}" "
    cd ${REMOTE_DIR} && \
    if [ -f .env.master ]; then
        echo '.env.master gefunden'
    else
        echo 'WARN: .env.master nicht gefunden, nutze Umgebungsvariablen'
    fi && \
    sudo docker compose -f docker-compose.master.yml up -d --pull always
"

echo "=== Deploy abgeschlossen ==="
echo "Master API: http://${SSH_HOST}:8791/api/heartbeat"
echo "Satelliten: MASTER_URL=http://${SSH_HOST}:8791/api/music setzen"
