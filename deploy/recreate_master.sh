#!/bin/bash
# Master-Container neu starten mit dem neuen Image
set -eu
cd /home/hermes/voice-satellite

docker rm -f voice-satellite-master >/dev/null

# Identische Parameter wie der bisherige Container (docker-run-Setup),
# nur das neue Image 2.0.0alpha
docker run -d \
  --name voice-satellite-master \
  --restart unless-stopped \
  -p 8791:8791 \
  -v /media/fileserver:/m:ro \
  -v /home/hermes/voice-satellite/data:/data \
  -v /home/hermes/voice-satellite/master/m:/app/master/m \
  -v /home/hermes/voice-satellite/master/.ssh:/app/master/.ssh \
  --env-file /home/hermes/voice-satellite/.env.master \
  voice-satellite-master:2.0.0alpha

sleep 8
echo "=== Status ==="
docker ps --filter name=voice-satellite-master --format '{{.Names}}|{{.Status}}'
echo "=== Logs (letzte 20) ==="
docker logs voice-satellite-master 2>&1 | tail -20
echo "=== Healthcheck ==="
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8791/healthz || echo "healthz noch nicht ready"
