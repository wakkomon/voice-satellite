#!/bin/bash
# .env.master um Proxy-Backend-URLs ergänzen (falls nicht schon vorhanden)
set -u
cd /home/hermes/voice-satellite
cp .env.master .env.master.bak.$(date +%s)

append() {
  local key="$1" val="$2"
  if grep -qE "^${key}=" .env.master; then
    echo "  skip (schon da): ${key}"
  else
    echo "${key}=${val}" >> .env.master
    echo "  added: ${key}=${val}"
  fi
}

echo "=== Proxy-Backend-URLs ergänzen ==="
append TTS_URL "http://192.168.1.31:7811/v1/audio/speech"
append TTS_MODEL "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
append TTS_VOICE "Uncle_fu"
append TTS_LANGUAGE "German"
append STT_URL "http://192.168.1.31:9000/v1/audio/transcriptions"
append STT_MODEL "whisper-1"
append STT_LANGUAGE "de"
append LLM_URL "http://192.168.1.33:8642/v1/chat/completions"
append LLM_MODEL "hermes-agent"
append LLM_ENABLE_THINKING "false"
append LLM_HAS_TOOLS "auto"
append OPENHAB_URL "http://192.168.1.33:8080/rest/items"
append BRIDGE_HOST "192.168.1.24"
append BRIDGE_PORT "8792"

echo
echo "=== Ergebnis (nur Key=Wert-Format) ==="
grep -E "^(TTS|STT|LLM|OPENHAB)_" .env.master | sed 's/=\(.\).*/=\1.../' 2>/dev/null || grep -E "^(TTS|STT|LLM|OPENHAB)_" .env.master
