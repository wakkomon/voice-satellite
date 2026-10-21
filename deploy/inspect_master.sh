#!/bin/bash
# Master-Container-Details für das Re-Deploy auslesen
set -u
echo "=== ENV-Keys des laufenden Containers ==="
docker inspect voice-satellite-master --format '{{range .Config.Env}}{{println .}}' | sort
echo "=== CMD ==="
docker inspect voice-satellite-master --format '{{json .Config.Cmd}} {{json .Config.Entrypoint}}'
echo "=== Netzwerk ==="
docker inspect voice-satellite-master --format '{{json .NetworkSettings.Networks}}'
echo "=== .env.master Keys (Werte redigiert) ==="
python3 - <<'PY'
for line in open('/home/hermes/voice-satellite/.env.master'):
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    if '=' in line:
        k, v = line.split('=', 1)
        print(f'{k}={"<set>" if v else "<leer>"}')
PY
