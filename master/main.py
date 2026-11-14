"""Master-Entry-Point

Startet den Starlette-Server mit Indexer-HTTP-API, Raspi-Manager und
Hintergrund-Index-Scan.
"""
from __future__ import annotations

import logging
import sys
import uvicorn

from . import certgen, config as cfg
from .api import app, manager, start_indexer_background, _append_log_line
import logging

# ── Logging sink: leitet Logs in den Live-Log-Puffer ───────────────


class _MasterLogSink(logging.Handler):
    """Handler, der Log-Einträge auch in den Live-Log-Puffer schreibt."""

    def __init__(self) -> None:
        super().__init__()
        self.setLevel(logging.DEBUG)
        # Einheitliches Format fuer den Live-Log (UI parsed Zeit + Level + Logger).
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            _append_log_line(line)
        except Exception:
            self.handleError(record)


log = logging.getLogger("master")


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("Master-Server startet (API auf %s:%d, TLS)", cfg.API_HOST, cfg.API_PORT)
    # TLS: selfsigned-Zertifikat sicherstellen (neu, wenn fehlt/ablaufend) und
    # den Server als HTTPS betreiben — der gesamte LAN-Verkehr (Satelliten,
    # WebUI, MCP, Proxy) laeuft verschluesselt.
    certfile, keyfile = certgen.ensure_cert()
    log.info("HTTPS aktiv (Zertifikat: %s)", certfile)

    # Raspi-Manager starten (Satelliten-Anmeldung + Heartbeat) — Singleton aus api.py
    manager.start()

    # Zentrales Scheduling (Master-Modus-Wecker/Timer/Erinnerungen):
    # faellige Instanzen aus api.schedule_store an die Geraete liefern.
    from . import schedule as sched_mod
    from .api import schedule_store
    schedule_store.start()

    # Logging-Handler: alle Logs der 'master' Kategorie auch in den Live-Log-Puffer schreiben
    log.addHandler(_MasterLogSink())

    # Initialer Index-Scan im Hintergrund-Thread
    start_indexer_background()

    # API-Server starten
    uvicorn.run(
        app,
        host=cfg.API_HOST,
        port=cfg.API_PORT,
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
        log_level=cfg.LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
