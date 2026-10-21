"""Master-Index-Client (2.0.0alpha).

Der Satellite holt den fertigen Medien-Index vom Master-Container per
HTTP. Der Master (dev) ist die EINZIGE Index-Quelle: er scannt CIFS
selbst und liefert den fertigen Payload. Der Satellite indiziert
selbst nicht — kein lokaler CIFS-Scan, kein Fallback.

Das Master-Payload hat dieselbe Struktur wie der Rückgabewert von
``master.indexer._build_media_index()`` — dadurch überleben alle
Matching- und Scoring-Logik ohne Anpassung.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import config, intent

log = logging.getLogger("master_index")


# ── Verbindungs-Status (für WebUI / Status-Endpoint) ──────────
# Zentrales, thread-übergreifend abfragbares Bild der Master-Verbindung.
# Der Index-Sync-Thread (unten) schreibt; Web-/Heartbeat-Threads lesen.
_master_lock = threading.Lock()
_master_state = {
    "connected": False,   # letzter HTTP-Versuch war erfolgreich
    "failures": 0,        # aufeinanderfolgende Fehlschläge (seit letztem OK)
    "last_ok": None,      # ISO-Zeit des letzten erfolgreichen Syncs
    "last_try": None,     # ISO-Zeit des letzten Versuchs
    "source": "none",     # "master", sobald der erste Sync geklappt hat
}


def _now_iso() -> str:
    """Lokale Zeit (Europe/Berlin) als ISO-Zeitstempel."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def master_status() -> dict:
    """Aktuellen Master-Verbindungsstatus als JSON-fähiges Dict.

    Für die Satelliten-WebUI (web.build_status). „configured"=false, wenn
    kein MASTER_URL gesetzt ist (reiner Standalone-Betrieb, lokaler Index).
    """
    configured = bool(config.MASTER_URL)
    with _master_lock:
        return {
            "configured": configured,
            "connected": bool(_master_state["connected"]) and configured,
            "failures": _master_state["failures"],
            "last_ok": _master_state["last_ok"],
            "last_try": _master_state["last_try"],
            "source": _master_state["source"],
        }


def _http_get_json(url: str, timeout: int = 30) -> dict | None:
    """Simpler HTTP-GET, gibt JSON zurück oder None.

    Der Index kann mehrere MB groß sein (Tausende Einträge) — daher
    30 s Timeout und kein Gzip-Handshake über stdlib.
    """
    try:
        req = Request(url, headers={"Accept-Encoding": "identity"})
        with urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            body = r.read().decode("utf-8")
        return json.loads(body)
    except (URLError, OSError, ValueError):
        return None


def get_master_index_data() -> dict | None:
    """Fertigen Index vom Master holen.

    Payload hat dieselbe Struktur wie ``master.indexer._build_media_index()``:
      { "m3u_idx": {...}, "m3u_all": [...],
        "mp3_idx": {...}, "mp3_all": [...], "dir_idx": {...} }

    Gibt Payload zurück oder None, wenn Master nicht erreichbar.
    """
    url = config.MASTER_URL.rstrip("/") + "/api/music"
    return _http_get_json(url)


def _apply_payload(payload: dict) -> None:
    """Master-Payload in die In-Memory-Index-Dicts von intent übernehmen."""
    with intent._lock:
        intent._m3u_index = payload.get("m3u_idx") or {}
        intent._m3u_all = payload.get("m3u_all") or []
        intent._mp3_index = payload.get("mp3_idx") or {}
        intent._dir_index = payload.get("dir_idx") or {}
    log.info("Master-Index übernommen: %d Playlists, %d Audiotracks",
             len(intent._m3u_all),
             len((payload.get("mp3_all") or [])))


def _sync_once(failures: list[int]) -> bool:
    """Einmaligen Sync versuchen. True wenn Erfolg.

    Der Master ist die einzige Index-Quelle: bei Fehlschlag wird kein
    lokaler CIFS-Scan gestartet — der letzte erfolgreiche Master-Index
    bleibt bis der Master wieder antwortet.
    """
    payload = get_master_index_data()
    if payload is not None:
        if failures[0] > 0:
            log.info("Master wieder erreichbar — Index wird aktualisiert")
        failures[0] = 0
        _apply_payload(payload)
        with _master_lock:
            _master_state["connected"] = True
            _master_state["failures"] = 0
            _master_state["last_ok"] = _now_iso()
            _master_state["last_try"] = _master_state["last_ok"]
            _master_state["source"] = "master"
        return True
    failures[0] += 1
    with _master_lock:
        _master_state["connected"] = False
        _master_state["failures"] = failures[0]
        _master_state["last_try"] = _now_iso()
    log.warning("Master %s antwortet nicht (Fehlschlag %d)",
                config.MASTER_URL, failures[0])
    return False


def start_sync(interval: int | None = None) -> None:
    """Master-Index-Sync im Hintergrund starten.

    Sofortiger erster Fetch, dann alle MASTER_POLL_SEC Sekunden neu.
    """
    interval = interval or config.MASTER_POLL_SEC
    failures = [0]

    def _worker() -> None:
        while True:
            if not _sync_once(failures):
                # Bei Fehlschlag kürzer versuchen, bis Master zurückkommt
                time.sleep(min(interval, 60))
            else:
                time.sleep(interval)

    threading.Thread(target=_worker, name="master-index-sync",
                     daemon=True).start()
    log.info("Master-Index-Sync gestartet (%s, Poll: %ds)",
             config.MASTER_URL, interval)
