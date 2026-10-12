"""MCP (Model Context Protocol) – HTTP-Remote-Steuerung für voice-satellite.

Startet einen eigenen HTTP-Server auf MCP_PORT und unterstützt MCP-over-JSON-RPC
über HTTP POST. Ein MCP-Client kann Töne abspielen und Bilder anzeigen.

Verwendet MCP-Standard-Transport (JSON-RPC over HTTP/POST).

Tools:
    - play_tone      : Ton abspielen (boot/wake/end/error/alarm/blip)
    - show_text      : Text auf PIDisplay anzeigen
    - show_weather   : Wetter-Bild auf Display
    - show_music_info: Musik-Info-Overlay auf Display
    - set_alarm      : Wecker setzen
    - get_tones      : Alle Töne auflisten
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

log = logging.getLogger("voice-satellite.mcp")

# ── Ton-Konfiguration ───────────────────────────────────────────────
SOUND_NAMES = [
    ("boot", "Startton: Satellit ist online"),
    ("wake", "Codewort erkannt, Aufnahme beginnt"),
    ("end", "Spracheingabe erkannt, Verarbeitung läuft"),
    ("error", "Fehler: keine Sprache oder LLM-Ausfall"),
    ("alarm", "Alarm-Sirene (hoch/tief Wechsel)"),
    ("blip", "Kurzes Bestätigungssignal"),
]


# ── Tool-Implementierungen ──────────────────────────────────────────
def _play_tone(name: str) -> str:
    """Einen vordefinierten Klingelton abspielen."""
    valid = {n for n, _ in SOUND_NAMES}
    if name not in valid:
        return f"Ungültiger Ton '{name}' – verfügbare: {', '.join(sorted(valid))}"
    try:
        from app.tones import play as play_tone_impl
        play_tone_impl(name)
        return f"Ton '{name}' abgespielt"
    except Exception as e:
        return f"Ton-Abspiel-Fehler: {e}"


def _show_text(heading: str, body: str = "") -> str:
    """Text auf dem PIDisplay anzeigen."""
    try:
        from app.display import show_text
        show_text(heading, body)
        return f"Display: '{heading}' {body or '(leer)'}"
    except ImportError:
        return "Display-Modul nicht verfügbar"
    except Exception as e:
        return f"Display-Fehler: {e}"


def _show_weather() -> str:
    """Wetter-Bild auf dem PIDisplay anzeigen."""
    try:
        from app.display import show_weather as show_w
        show_w()
        return "Wetter-Bild wird angezeigt"
    except ImportError:
        return "Display-Modul nicht verfügbar"
    except Exception as e:
        return f"Display-Fehler: {e}"


def _show_music_info(artist: str, title: str) -> str:
    """Musik-Info auf dem PIDisplay anzeigen."""
    try:
        from app.display import show_music_info
        show_music_info(artist, title)
        return f"Musik-Info: '{artist}' – '{title}'"
    except ImportError:
        return "Display-Modul nicht verfügbar"
    except Exception as e:
        return f"Display-Fehler: {e}"


def _set_alarm(minutes: int = 5, label: str = "") -> str:
    """Einen Wecker setzen."""
    def alarm_beep():
        time.sleep(minutes * 60)
        try:
            from app.tones import play as play_tone_impl
            play_tone_impl("alarm")
            try:
                from app.display import show_text
                show_text("Wecker", label or "Zeit!")
            except Exception:
                pass
        except Exception:
            pass

    t = threading.Thread(target=alarm_beep, daemon=True)
    t.start()
    return f"Wecker in {minutes} Min. gesetzt"


def _get_tones() -> list[dict]:
    """Alle verfügbaren Töne auflisten."""
    return [{"name": n, "description": d} for n, d in SOUND_NAMES]


# ── Tool-Routing ────────────────────────────────────────────────────
TOOL_HANDLERS = {
    "play_tone": _play_tone,
    "show_text": _show_text,
    "show_weather": _show_weather,
    "show_music_info": _show_music_info,
    "set_alarm": _set_alarm,
    "get_tones": _get_tones,
}

TOOL_SCHEMA = {
    "name": "voice-satellite",
    "description": "Remote-Steuerung des voice-satellite PIDisplay",
    "tools": [
        {
            "name": "play_tone",
            "description": "Einen vordefinierten Klingelton abspielen (boot/wake/end/error/alarm/blip)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name des Tons"}
                },
                "required": ["name"]
            }
        },
        {
            "name": "show_text",
            "description": "Text auf dem PIDisplay anzeigen",
            "input_schema": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string", "description": "Überschrift (max. ~20 Zeichen)"},
                    "body": {"type": "string", "description": "Textkörper, optional"}
                },
                "required": ["heading"]
            }
        },
        {
            "name": "show_weather",
            "description": "Wetter-Bild auf dem PIDisplay anzeigen",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "show_music_info",
            "description": "Musik-Info als Overlay auf dem Display",
            "input_schema": {
                "type": "object",
                "properties": {
                    "artist": {"type": "string", "description": "Künstler"},
                    "title": {"type": "string", "description": "Titel"}
                },
                "required": ["artist", "title"]
            }
        },
        {
            "name": "set_alarm",
            "description": "Wecker nach Minuten setzen",
            "input_schema": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "integer", "description": "Minuten bis Alarm"},
                    "label": {"type": "string", "description": "Optionaler Display-Text"}
                },
                "required": []
            }
        },
        {
            "name": "get_tones",
            "description": "Alle verfügbaren Töne auflisten",
            "input_schema": {"type": "object", "properties": {}}
        }
    ]
}


# ── JSON-RPC HTTP-Handler ───────────────────────────────────────────
class _MCPHandler(BaseHTTPRequestHandler):
    """Behandelt MCP-over-JSON-RPC über HTTP POST."""

    def log_message(self, format, *args):
        log.debug(format, *args)

    def _respond(self, code: int, body: Any):
        """Sende JSON-Antwort."""
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        """Tool-Discovery: GET /mcp/tools → Liste aller Tools."""
        if self.path == "/mcp/tools":
            self._respond(200, {"schema": TOOL_SCHEMA})
            return
        self._respond(404, {"error": "Not found"})

    def do_POST(self):
        """MCP JSON-RPC: POST /mcp → MCP-over-HTTP/JSON-RPC."""
        if self.path != "/mcp":
            self._respond(404, {"error": "Not found"})
            return

        # Request lesen
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            req = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._respond(400, {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Invalid JSON"}})
            return

        # Tool call
        if "method" in req:
            method = req["method"]
            params = req.get("params", {})
            request_id = req.get("id")

            handler = TOOL_HANDLERS.get(method)
            if not handler:
                self._respond(200, {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Tool '{method}' nicht gefunden"},
                    "id": request_id
                })
                return

            # Tool ausführen
            try:
                result = handler(**params) if isinstance(params, dict) else handler()
                self._respond(200, {
                    "jsonrpc": "2.0",
                    "result": result,
                    "id": request_id
                })
            except Exception as e:
                self._respond(200, {
                    "jsonrpc": "2.0",
                    "error": {"code": -32000, "message": str(e)},
                    "id": request_id
                })
                return

        else:
            self._respond(400, {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request"}})


# ── HTTP-Server starten ──────────────────────────────────────────
def start_mcp(host: str = "0.0.0.0", port: int = 8780):
    """MCP-Server als HTTP-Service starten.

    MCP-over-JSON-RPC über HTTP POST:
        POST http://host:port/mcp  →  {"jsonrpc":"2.0","method":"play_tone","params":{"name":"boot"}}
        GET  http://host:port/mcp/tools  →  Tool-Discovery
    """
    def run():
        server = HTTPServer((host, port), _MCPHandler)
        log.info("MCP-Server gestartet (HTTP-POST, %s:%d)", host, port)
        server.serve_forever()

    t = threading.Thread(target=run, daemon=True, name="mcp-server")
    t.start()
