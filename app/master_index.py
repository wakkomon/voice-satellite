"""Master-Index-Client (2.0.0alpha).

Der Satellite holt den fertigen Medien-Index vom Master-Container per
HTTP. Das Master-Payload hat dieselbe Struktur wie der Rückgabewert von
``intent._build_media_index()`` — dadurch überleben alle Matching- und
Scoring-Logik ohne Anpassung.

Fallback-Kette:
  1. Master-Index (Primär, via HTTP, sofort + periodisch)
  2. Master dauerhaft unerreichbar (3× Fehlschlag) → lokaler CIFS-Scan
     wird als Fallback gestartet (1.x-Verhalten)
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

_CONSECUTIVE_FAIL_THRESHOLD = 3


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

    Payload hat dieselbe Struktur wie ``intent._build_media_index()``:
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

    Bei _CONSECUTIVE_FAIL_THRESHOLD aufeinanderfolgenden Fehlschlägen
    wird der lokale CIFS-Scan als Fallback gestartet (idempotent —
    intent.start_m3u_indexer prüft _indexer_started).
    """
    payload = get_master_index_data()
    if payload is not None:
        if failures[0] > 0:
            log.info("Master wieder erreichbar — lokaler Fallback-Index "
                     "wird durch Master-Index ersetzt")
        failures[0] = 0
        _apply_payload(payload)
        return True
    failures[0] += 1
    log.warning("Master %s antwortet nicht (Fehlschlag %d/%d)",
                config.MASTER_URL, failures[0], _CONSECUTIVE_FAIL_THRESHOLD)
    if failures[0] == _CONSECUTIVE_FAIL_THRESHOLD:
        log.warning("Master dauerhaft nicht erreichbar — starte lokalen "
                    "CIFS-Scan als Fallback")
        try:
            intent.start_m3u_indexer(config.MUSIC_ROOT)
        except Exception:
            log.exception("Lokaler Fallback-Scan fehlgeschlagen")
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
