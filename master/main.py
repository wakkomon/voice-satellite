"""Master-Entry-Point (2.0.0alpha).

Startet den Starlette-Server mit Indexer-HTTP-API, Raspi-Manager und
Hintergrund-Index-Scan.
"""
from __future__ import annotations

import logging
import sys
import uvicorn

from . import config as cfg
from .api import app, manager, start_indexer_background

log = logging.getLogger("master")


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("Master-Server startet (API auf %s:%d)", cfg.API_HOST, cfg.API_PORT)

    # Raspi-Manager starten (Discovery + Heartbeat) — Singleton aus api.py
    manager.start()

    # Initialer Index-Scan im Hintergrund-Thread
    start_indexer_background()

    # API-Server starten
    uvicorn.run(
        app,
        host=cfg.API_HOST,
        port=cfg.API_PORT,
        log_level=cfg.LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
