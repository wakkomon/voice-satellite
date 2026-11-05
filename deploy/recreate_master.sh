#!/bin/bash
# Master-Container neu starten mit dem neuen Image
set -eu
cd /home/hermes/voice-satellite

# Version: Build-Artefakt master/_version.py (gen_version.py, vor dem Build
# im Checkout laufen lassen), Fallback master/__init__.py (dort ist der
# Fallback-Wert EINGEZENTERT — daher indent-tolerantes sed)
VERSION=""
[ -f master/_version.py ] && \
  VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' master/_version.py | head -1)
[ -z "$VERSION" ] && \
  VERSION=$(sed -n 's/^[[:space:]]*__version__ = "\(.*\)"/\1/p' master/__init__.py | head -1)
if [ -z "$VERSION" ]; then echo "FEHLER: __version__ nicht gefunden (_version.py / __init__.py)" >&2; exit 1; fi
echo "Deploy Version: $VERSION"

# Altes Image als Rollback-Marke sichern, bevor der Tag ueberschrieben wird.
docker tag "voice-satellite-master:${VERSION}" "voice-satellite-master:rollback-$(date -u +%Y%m%d-%H%M)" 2>/dev/null || true

docker rm -f voice-satellite-master >/dev/null

# Identische Parameter wie der bisherige Container (docker-run-Setup),
# nur das neue Image $VERSION
docker run -d \
  --name voice-satellite-master \
  --restart unless-stopped \
  -p 8791:8791 \
  -v /media/fileserver:/m:ro \
  -v /home/hermes/voice-satellite/data:/data \
  -v /home/hermes/voice-satellite/master/m:/app/master/m \
  -v /home/hermes/voice-satellite/master/.ssh:/app/master/.ssh \
  --env-file /home/hermes/voice-satellite/.env.master \
  "voice-satellite-master:${VERSION}"

sleep 8
echo "=== Status ==="
docker ps --filter name=voice-satellite-master --format '{{.Names}}|{{.Status}}'
echo "=== Logs (letzte 20) ==="
docker logs voice-satellite-master 2>&1 | tail -20
echo "=== Healthcheck ==="
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8791/healthz || echo "healthz noch nicht ready"
echo "=== Token im Container ==="
docker exec voice-satellite-master printenv MASTER_TOKEN >/dev/null 2>&1 \
  && echo "MASTER_TOKEN gesetzt" || echo "MASTER_TOKEN fehlt (auf .env.master pruefen)"
