#!/usr/bin/env bash
# openHAB-Beispiel: Sprachnachrichten & Alarmtöne an den Voice-Satelliten
# (Pi, 192.168.1.32:8770) senden.
#
# Aufruf aus einer openHAB-Regel (Exec-Binding) oder von der Shell:
#   ./openhab_voice.sh speak "Achtung: die Waschmaschine ist fertig."
#   ./openhab_voice.sh tone alarm 3
#   ./openhab_voice.sh tone wake
#
# openHAB-Regel-Beispiel (exec: eine Zeile shell='...' in der .rules):
#   rule "Waschmaschine fertig"
#   when Item Waschmaschine_Fertig changed to ON
#   then
#     executeCommandLine("bash",
#       "/etc/openhab/exec/voice_satellite.sh",
#       "speak", "Die Waschmaschine ist fertig.")
#     executeCommandLine("bash",
#       "/etc/openhab/exec/voice_satellite.sh", "tone", "alarm", "2")
#   end
#
# .env-Note: WEB_PORT muss auf dem Pi 8770 sein (docker-compose-Port).
set -euo pipefail

PI_HOST="${PI_HOST:-192.168.1.32}"
PI_PORT="${PI_PORT:-8770}"
BASE="http://${PI_HOST}:${PI_PORT}"

usage() {
  cat <<EOF
Usage: $(basename "$0") <command> [args]
  speak "<text>"            Text vorlesen lassen (TTS auf dem Pi)
  tone <alarm|wake|end|boot|error> [repeat]
                            Ton abspielen (Alarm = 2-Ton-Sirene)
  intent "<text>"           Frage an den LLM-Router (Antwort als JSON)
  health                    Container-Health checken
Example:
  $(basename "$0") speak "Essen ist fertig!"
  $(basename "$0") tone alarm 3
EOF
  exit 1
}

cmd="${1:-}"; [ -n "$cmd" ] || usage
# JSON-String escapen (Backslash + Anfuehrungszeichen) – nur stdtools,
# damit das Skript auch auf Hosts ohne python3 laeuft (openHAB-Container).
json_escape() { printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"; }

case "$cmd" in
  speak)
    text="${2:-}"
    [ -n "$text" ] || { echo "speak braucht einen Text" >&2; exit 1; }
    curl -sf -X POST "$BASE/speak" \
      -H 'Content-Type: application/json' \
      -d "{\"text\": $(json_escape "$text")}"
    ;;
  tone)
    tone="${2:-alarm}"
    repeat="${3:-1}"
    case "$repeat" in (*[!0-9]*|'') echo "repeat muss eine Zahl sein" >&2; exit 1;; esac
    curl -sf -X POST "$BASE/tone" \
      -H 'Content-Type: application/json' \
      -d "{\"tone\": \"$tone\", \"repeat\": $repeat}"
    ;;
  intent)
    text="${2:-}"
    [ -n "$text" ] || { echo "intent braucht einen Text" >&2; exit 1; }
    curl -sf -X POST "$BASE/intent" \
      -H 'Content-Type: application/json' \
      -d "{\"text\": $(json_escape "$text")}"
    ;;
  health)
    curl -sf "$BASE/health"
    ;;
  *)
    usage
    ;;
esac
