#!/bin/bash
cd /home/pi/voice-satellite
dk() { sudo docker "$@"; }
dk create --name voice-satellite --restart unless-stopped -p 8770:8770 \
  --env-file /home/pi/voice-satellite/.env \
  -e BRIDGE_HOST=192.168.1.32 -e BRIDGE_PORT=8790 \
  -e MASTER_URL=http://192.168.1.24:8791 \
  -v /media/private:/m:ro \
  --device /dev/snd:/dev/snd --device /dev/fb1:/dev/fb1 \
  voice-satellite:latest
echo "CREATE_EXIT: $?"
dk start voice-satellite
echo "START_EXIT: $?"
sleep 3
dk ps --filter name=voice-satellite --format "{{.Names}} {{.Status}} {{.Ports}}"
dk logs --tail 20 voice-satellite
