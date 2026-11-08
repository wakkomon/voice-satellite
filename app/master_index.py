"""Master-Index-Client.

Der Satellite holt den fertigen Medien-Index vom Master-Container per
HTTP. Der Master (dev) ist die EINZIGE Index-Quelle: er scannt CIFS
selbst und liefert den fertigen Payload. Der Satellite indiziert
selbst nicht — kein lokaler CIFS-Scan, kein Fallback.

Das Master-Payload hat dieselbe Struktur wie der Rückgabewert von
``master.indexer._build_media_index()`` — dadurch überleben alle
Matching- und Scoring-Logik ohne Anpassung.
"""
from __future__ import annotations

import gzip
import json
import logging
import threading
import time
from http.client import HTTPException
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


def _http_get_json(url: str, timeout: int = 30,
                   use_gzip: bool = False) -> dict | None:
    """Simpler HTTP-GET, gibt JSON zurück oder None.

    Der Index ist mehrere MB groß (~42 MB JSON bei 60k+ Tracks). Mit
    use_gzip bittet der Satellit um gzip; der Master liefert dann das ~5x
    kleinere komprimierte Payload — schneller UND unempfindlicher gegen
    abgebrochene Downloads (IncompleteRead). gzip.decompress ist Stdlib.
    """
    headers = {"Accept-Encoding": "gzip" if use_gzip else "identity"}
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            body = r.read()
            if r.headers.get("Content-Encoding", "") == "gzip":
                body = gzip.decompress(body)
            return json.loads(body.decode("utf-8"))
    except (URLError, OSError, ValueError, HTTPException):
        # HTTPException faengt http.client.IncompleteRead ab (Download vor
        # Transfer-Ende abgebrochen). Ohne diesen Catch warf ein einzelner
        # abgebrochener Versuch, der Master-Index-Sync-Thread starb daran und
        # das Web-UI-Badge blieb dauerhaft auf "getrennt" stehen, obwohl der
        # unabhaengige Heartbeat-Thread den Satelliten weiter "online" hielt.
        return None


def get_master_index_data() -> dict | None:
    """Fertigen Index vom Master holen.

    Payload hat dieselbe Struktur wie ``master.indexer._build_media_index()``:
      { "m3u_idx": {...}, "m3u_all": [...],
        "mp3_idx": {...}, "mp3_all": [...], "dir_idx": {...} }

    Gibt Payload zurück oder None, wenn Master nicht erreichbar.
    """
    url = config.MASTER_URL.rstrip("/") + "/api/music"
    return _http_get_json(url, use_gzip=True)


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
            try:
                ok = _sync_once(failures)
            except Exception as e:
                # Defensive: der Sync-Thread darf bei KEINEM Fehler absterben.
                # _http_get_json fängt die erwarteten Fehler bereits ab; dieser
                # Guard hält den Thread auch bei einer unerwarteten Exception am
                # Leben, damit das Master-Badge nicht auf "getrennt" einfriert.
                log.warning("Master-Index-Sync-Fehler (Thread läuft weiter): %s", e)
                ok = False
            if not ok:
                # Bei Fehlschlag kürzer versuchen, bis Master zurückkommt
                time.sleep(min(interval, 60))
            else:
                time.sleep(interval)

    threading.Thread(target=_worker, name="master-index-sync",
                     daemon=True).start()
    log.info("Master-Index-Sync gestartet (%s, Poll: %ds)",
             config.MASTER_URL, interval)
