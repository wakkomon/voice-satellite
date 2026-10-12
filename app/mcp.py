"""MCP (Model Context Protocol) – Remote-Steuerungs-Schnittstelle für voice-satellite.

Bietet Tools zum Abspielen von Tönen und Anzeigen von Bildern auf dem
PIDisplay, die von einem AI-Agenten über MCP aufgerufen werden können.

Verwendet FastMCP (mcp.server) – das gleiche Framework, das im parent
Process von main.py verwendet wird. Wenn MCP noch nicht installiert ist,
wird die App ohne MCP-Server gestartet (kein Crash, nur Warnung).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("voice-satellite.mcp")

# ── Path-Konfiguration ──────────────────────────────────────────────
DATA_DIR = Path("/data")

# ── Ton-Konfiguration ───────────────────────────────────────────────
SOUND_NAMES = [
    ("boot", "Startton: Satellit ist online"),
    ("wake", "Codewort erkannt, Aufnahme beginnt"),
    ("end", "Sprachende erkannt, Verarbeitung läuft"),
    ("error", "Fehler: keine Sprache oder LLM-Ausfall"),
    ("alarm", "Alarm-Sirene (hoch/tief Wechsel)"),
    ("blip", "Kurzes Bestätigungssignal"),
]


# ── MCP-Server initialisieren ──────────────────────────────────────
def create_mcp_server():
    """MCP-Server-Instanz erstellen (spät, nach Import-Check)."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        log.warning("mcp-Paket nicht installiert – MCP-Server übersprungen")
        return None

    mcp = FastMCP("voice-satellite", json_schema_preset="default")

    @mcp.tool()
    def play_tone(name: str) -> str:
        """Einen vordefinierten Klingelton abspielen.

        Verfügbare Töne: boot, wake, end, error, alarm, blip.
        """
        valid = {n for n, _ in SOUND_NAMES}
        if name not in valid:
            return f"Ungültiger Ton '{name}' – verfügbare: {', '.join(sorted(valid))}"

        try:
            from app.tones import play as play_tone_impl
            play_tone_impl(name)
            return f"Ton '{name}' abgespielt"
        except Exception as e:
            return f"Ton-Abspiel-Fehler: {e}"

    @mcp.tool()
    def show_text(heading: str, body: str = "") -> str:
        """Text auf dem PIDisplay anzeigen.

        heading: Überschrift (max. ~20 Zeichen)
        body: Textkörper, optional. Leer lassen für schwarzen Bildschirm.
        """
        try:
            from app.display import show_text
            show_text(heading, body)
            return f"Display: '{heading}' {body or '(leer)'}"
        except ImportError:
            return "Display-Modul nicht verfügbar"
        except Exception as e:
            return f"Display-Fehler: {e}"

    @mcp.tool()
    def show_weather() -> str:
        """Wetter-Bild auf dem PIDisplay anzeigen."""
        try:
            from app.display import show_weather as show_w
            show_w()
            return "Wetter-Bild wird angezeigt"
        except ImportError:
            return "Display-Modul nicht verfügbar"
        except Exception as e:
            return f"Display-Fehler: {e}"

    @mcp.tool()
    def show_music_info(artist: str, title: str) -> str:
        """Musik-Info auf dem PIDisplay anzeigen.

        Wird als Overlay über den Wetter-Hintergrund gemalt.
        """
        try:
            from app.display import show_music_info
            show_music_info(artist, title)
            return f"Musik-Info: '{artist}' – '{title}'"
        except ImportError:
            return "Display-Modul nicht verfügbar"
        except Exception as e:
            return f"Display-Fehler: {e}"

    @mcp.tool()
    def set_alarm(minutes: int = 5, label: str = "") -> str:
        """Einen Wecker setzen, der nach Minuten abstimmt.

        label: optionaler Text, der auf dem Display während des
               Wartens angezeigt wird.
        """
        try:
            import threading

            def alarm_beep():
                time.sleep(minutes * 60)
                from app.tones import play as play_tone_impl
                play_tone_impl("alarm")
                try:
                    from app.display import show_text
                    show_text("Wecker", label or "Zeit!")
                except Exception:
                    pass

            t = threading.Thread(target=alarm_beep, daemon=True)
            t.start()
            return f"Wecker in {minutes} Min. gesetzt"
        except Exception as e:
            return f"Wecker-Fehler: {e}"

    @mcp.tool()
    def get_tones() -> list[dict]:
        """Alle verfügbaren Töne mit Beschreibung auflisten."""
        return [{"name": n, "description": d} for n, d in SOUND_NAMES]

    return mcp


# ── Socket-Server starten ──────────────────────────────────────────
def start_mcp():
    """MCP-Server starten (wird von main.py aus aufgerufen)."""
    mcp = create_mcp_server()
    if mcp is None:
        return

    def run():
        try:
            mcp.run("stdio")
        except Exception as e:
            log.warning("MCP-Server-Fehler: %s", e)

    t = threading.Thread(target=run, daemon=True, name="mcp-server")
    t.start()
    log.info("MCP-Server gestartet (stdio)")
