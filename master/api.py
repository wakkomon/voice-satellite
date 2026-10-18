"""Master-API – httpx/uvicorn (2.0.0alpha).

Endpoints:
  GET    /api/heartbeat        → Status + Sat-Liste
  POST   /api/satellite/{mac}/restart
  POST   /api/satellite/{mac}/pause
  GET    /events               → SSE stream (index_done, sat_status)
  GET    /api/music            → Index-Payload (gzip optional)
  POST   /index/rebuild        → Trigger Rebuild

Der Satellite zieht von GET /api/music den fertigen Index.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import os
import threading
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import config as master_cfg
from .indexer import _build_media_index, load_index_cache, save_index
from .manager import SatManager

log = logging.getLogger("master.api")

# Globale Zustände
_index_data: dict | None = None
_index_ts: float = 0.0
_index_busy = False
_sse_clients: list[asyncio.Queue] = []


def _broadcast(event: str, data: dict) -> None:
    """Event an alle SSE-Clients senden."""
    payload = json.dumps({"event": event, "data": data}, ensure_ascii=False)
    for q in list(_sse_clients):
        try:
            q.put_nowait(f"event: {event}\ndata: {payload}\n\n")
        except asyncio.QueueFull:
            pass


# ── API Handler ─────────────────────────────────────────────────

async def _heartbeat(req: Request) -> JSONResponse:
    global _index_data, _index_ts
    if _index_data is None:
        # Während initialer Scan läuft (oder Cache noch leer) → 202
        return JSONResponse({"status": "indexing"}, status_code=202)
    return JSONResponse({
        "status": "running",
        "index_ts": _index_ts,
        "index_busy": _index_busy,
        "satellites": await manager.list_satellites(),
    })


async def _api_music(req: Request) -> Response:
    """Index-Payload – mit gzip auf Anfrage."""
    global _index_data
    if _index_data is None:
        _index_data = load_index_cache(master_cfg.CACHE_DIR)
    if _index_data is None:
        # Initialer Scan läuft noch → "im Aufbau" statt harter 503
        if _index_busy:
            return JSONResponse({"status": "indexing"}, status_code=202)
        return JSONResponse({"error": "no index"}, status_code=503)
    body = json.dumps(_index_data, ensure_ascii=False).encode("utf-8")
    accept = req.headers.get("accept-encoding", "")
    use_gzip = "gzip" in accept
    if use_gzip:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            gz.write(body)
        body = buf.getvalue()
    headers = {"Content-Encoding": "gzip"} if use_gzip else {}
    return Response(body, media_type="application/json", headers=headers)


async def _index_rebuild(req: Request) -> JSONResponse:
    """Rebuild im Hintergrund triggern (blockiert nicht die Event-Loop)."""
    global _index_busy
    if _index_busy:
        return JSONResponse({"status": "busy"}, status_code=409)

    def _rebuild() -> None:
        global _index_data, _index_ts, _index_busy
        t0 = time.time()
        try:
            log.info("Index-Rebuild gestartet")
            _index_data = _build_media_index(
                master_cfg.MUSIC_ROOT,
                max_depth=3,
                on_progress=lambda n: log.debug("Index: %d Einträge", n),
            )
            save_index(_index_data, master_cfg.CACHE_DIR)
            _index_ts = time.time()
            log.info("Index-Rebuild fertig in %.1f s", time.time() - t0)
            _broadcast("index_done", {"ts": _index_ts})
        except Exception:
            log.exception("Rebuild fehlgeschlagen")
            _broadcast("index_error", {})
        finally:
            _index_busy = False

    _index_busy = True
    threading.Thread(target=_rebuild, daemon=True, name="index-rebuild").start()
    return JSONResponse({"status": "started"}, status_code=202)


# ── Satellite-Manager Endpoints ─────────────────────────────────

async def _sat_restart(req: Request) -> JSONResponse:
    mac = req.path_params["mac"]
    result = await manager.action_restart_container(mac)
    return JSONResponse(result)


async def _sat_pause(req: Request) -> JSONResponse:
    mac = req.path_params["mac"]
    result = await manager.action_pause_container(mac)
    return JSONResponse(result)


# ── SSE Events ──────────────────────────────────────────────────

async def _events(req: Request) -> PlainTextResponse:
    """SSE Stream für Client-Reconnects."""
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
    _sse_clients.append(q)

    async def generator():
        try:
            while True:
                if await req.is_disconnected():
                    break
                msg = await q.get()
                yield msg
        finally:
            _sse_clients.remove(q)

    return PlainTextResponse(generator(), media_type="text/event-stream")


# ── Satellite List Endpoint ─────────────────────────────────────

async def _satellite_list(req: Request) -> JSONResponse:
    """Alle bekannten Satelliten auflisten."""
    satellites = await manager.list_satellites()
    return JSONResponse(satellites)


# ── App Setup ───────────────────────────────────────────────────

manager = SatManager()

routes = [
    Route("/healthz", lambda req: JSONResponse({"ok": True})),
    Route("/api/heartbeat", _heartbeat),
    Route("/api/music", _api_music),
    Route("/api/music/search", _api_music),  # Alias für Satelliten
    Route("/index/rebuild", _index_rebuild, methods=["POST"]),
    Route("/events", _events),
    Route("/api/satellite/{mac}/restart", _sat_restart, methods=["POST"]),
    Route("/api/satellite/{mac}/pause", _sat_pause, methods=["POST"]),
    Route("/api/satellites", _satellite_list),
]

app = Starlette(routes=routes)


def start_indexer_background() -> threading.Thread:
    """Hintergrund-Thread: Initialer Scan, dann periodische Updates."""
    def _scan_once() -> float:
        """Einen vollen Scan ausführen. Gibt Dauer in s zurück."""
        global _index_data, _index_ts
        t0 = time.time()
        log.info("Starte Index-Scan auf %s", master_cfg.MUSIC_ROOT)
        _index_data = _build_media_index(
            master_cfg.MUSIC_ROOT,
            max_depth=3,
            on_progress=lambda n: log.debug("Index: %d Einträge", n),
        )
        save_index(_index_data, master_cfg.CACHE_DIR)
        _index_ts = time.time()
        elapsed = time.time() - t0
        log.info("Index-Scan fertig in %.1f s (%d Playlists, %d Audios)",
                 elapsed,
                 len(_index_data.get("m3u_all", [])),
                 len(_index_data.get("mp3_all", [])))
        _broadcast("index_done", {"ts": _index_ts, "elapsed": elapsed})
        return elapsed

    def _run() -> None:
        global _index_busy
        _index_busy = True
        try:
            _scan_once()
        finally:
            _index_busy = False
        rescan_sec = int(os.environ.get("INDEX_RESCAN_SEC", "21600"))  # 6 h
        log.info("Periodischer Re-Scan alle %d s", rescan_sec)
        while True:
            time.sleep(rescan_sec)
            _index_busy = True
            try:
                _scan_once()
            finally:
                _index_busy = False

    t = threading.Thread(target=_run, daemon=True, name="indexer-init")
    t.start()
    return t
