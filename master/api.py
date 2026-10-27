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
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from . import config as master_cfg
from . import proxy
from . import mcp_gateway
from .indexer import _build_media_index, load_index_cache, save_index
from .manager import SatManager
from .satconfig import store as config_store, EDITABLE_KEYS, GLOBAL_ONLY_KEYS, SECRET_KEYS

log = logging.getLogger("master.api")

# Master-Startzeit für Uptime-Anzeige
_START_TIME = time.time()

# Live-Log-Puffer: Ringpuffer der letzten N Log-Zeilen
_LOG_LINES: list[str] = []
_LOG_MAX = 500
_log_lock = threading.Lock()


def _append_log_line(msg: str) -> None:
    """Einen Text-Zeilen-Eintrag zum Live-Log-Puffer hinzufügen."""
    with _log_lock:
        _LOG_LINES.append(msg)
        if len(_LOG_LINES) > _LOG_MAX:
            del _LOG_LINES[:_LOG_MAX]

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

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


async def _sat_unpause(req: Request) -> JSONResponse:
    mac = req.path_params["mac"]
    result = await manager.action_unpause_container(mac)
    return JSONResponse(result)


# ── Satellite-Heartbeat (Push von den Satelliten) ──────────────

async def _sat_heartbeat(req: Request) -> JSONResponse:
    """POST /api/satellite/{mac}/heartbeat – Satellite pusht seinen Status.

    Payload: {hostname, ip, docker_running, pipeline, system, components}
    Der Master aktualisiert last_seen, Hostname, IP, Pipeline-/Komponenten-Info.
    """
    mac = req.path_params["mac"].lower()
    try:
        payload = await req.json()
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    # Die echte Host-IP (von NAT) statt der Container-IP aus dem Payload
    if req.client:
        payload["ip"] = req.client.host
    manager.record_heartbeat(mac, payload)
    return JSONResponse({"ok": True})


# ── Zentrale Konfigurationsverwaltung (b) ──────────────────────
# Satelliten ziehen ihre gültige Konfiguration per GET /api/config/apply
# (im Heartbeat-Loop). Bei Änderungen pusht der Master aktiv via
# POST /apply-config an die Satelliten (nur die tatsächlich geänderten).

def _normalize_override(val) -> str | int | float | bool | None:
    """Werte für die Satellite-Apply normalisieren (nur Standard-Types)."""
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    return str(val)


async def _config_get(req: Request) -> JSONResponse:
    """GET /api/config → Snapshot der zentralen Konfiguration (UI)."""
    return JSONResponse(config_store.snapshot())


async def _config_put(req: Request) -> JSONResponse:
    """PUT /api/config → globale und/oder per-Satellite-Werte setzen.

    Body: {"global": {key: val}, "satellites": {mac: {key: val}}}
    Nur EDITABLE_KEYS greifen; Secrets werden NICHT hier gespeichert.
    Nach Erfolg: aktiv an alle (betroffenen) Satelliten pushen.
    """
    try:
        data = await req.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"error": "invalid body"}, status_code=400)

    # Nur erlaubte Schlüssel setzen (global: EDITABLE + GLOBAL_ONLY +
    # Secrets; per Satellite: nur EDITABLE_KEYS).
    g_allowed = set(EDITABLE_KEYS) | set(GLOBAL_ONLY_KEYS) | set(SECRET_KEYS)
    g = {k: v for k, v in (data.get("global") or {}).items() if k in g_allowed}
    sats: dict[str, dict] = {}
    for mac, ov in (data.get("satellites") or {}).items():
        if isinstance(ov, dict):
            sats[str(mac).lower()] = {k: v for k, v in ov.items() if k in EDITABLE_KEYS}

    if g:
        config_store.set_global(g)
    for mac, ov in sats.items():
        if ov:
            config_store.set_satellite(mac, ov)
    version = config_store.version()
    log.info("Zentrale Config aktualisiert (version=%d): global=%s sats=%s",
             version, sorted(g), sorted(sats))
    return JSONResponse({"ok": True, "version": version})


async def _config_export(req: Request) -> Response:
    """GET /api/config/export → Konfig-Datei als JSON-Download.

    Query: secrets=0 → Secrets weglassen (weitergabefähiges Backup).
    Default: alle Werte inkl. Secrets (echtes Backup/Transfer-Format).
    """
    include_secrets = req.query_params.get("secrets", "1") not in ("0", "false", "no")
    payload = config_store.export_payload(include_secrets=include_secrets)
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    fname = time.strftime("voice-satellite-config-%Y%m%d-%H%M%S.json")
    headers = {"Content-Disposition": f'attachment; filename="{fname}"'}
    return Response(body, media_type="application/json; charset=utf-8", headers=headers)


async def _config_import(req: Request) -> JSONResponse:
    """POST /api/config/import → Konfig-Datei (JSON) in den Store mergen.

    Body: Master-/Satellite-Export oder flaches {KEY: Wert}-Dict.
    """
    try:
        data = await req.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "invalid json"}, status_code=400)
    try:
        stats = config_store.import_payload(data)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    version = config_store.version()
    log.info("Config-Import: global=%d sats=%s unknown=%s (version=%d)",
             stats["global_applied"], stats["satellites_applied"],
             stats["unknown_keys"], version)
    return JSONResponse({"ok": True, "version": version, **stats})


async def _config_sat_get(req: Request) -> JSONResponse:
    """GET /api/config/satellite/{mac} → gültige (merged) Konfiguration."""
    mac = req.path_params["mac"].lower()
    return JSONResponse(config_store.get_effective(mac))


async def _config_apply_get(req: Request) -> JSONResponse:
    """GET /api/config/apply?mac=...&version=... → Satellite-Pull."""
    mac = req.query_params.get("mac", "").lower()
    try:
        client_ver = int(req.query_params.get("version", "0"))
    except ValueError:
        client_ver = 0
    master_ver = config_store.version()
    if client_ver >= master_ver:
        return JSONResponse({"changed": False, "version": master_ver,
                             "overrides": {}})
    eff = config_store.get_effective(mac)
    overrides = {k: _normalize_override(v) for k, v in eff.items() if v is not None}
    return JSONResponse({"changed": True, "version": master_ver,
                         "overrides": overrides})


# ── SSE Events ──────────────────────────────────────────────────

async def _events(req: Request) -> StreamingResponse:
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

    return StreamingResponse(generator(), media_type="text/event-stream")


# ── Satellite List Endpoint ─────────────────────────────────────

async def _satellite_list(req: Request) -> JSONResponse:
    """Alle bekannten Satelliten auflisten."""
    satellites = await manager.list_satellites()
    return JSONResponse(satellites)


async def _satellites_status(req: Request) -> JSONResponse:
    """Detaillierter Status pro Satellite (pipeline, components, system)."""
    with manager._lock:
        result = []
        for s in manager.satellites.values():
            result.append({
                "mac": s.mac,
                "ip": s.ip,
                "hostname": s.hostname,
                "status": s.status,
                "last_seen": s.last_seen,
                "docker_running": s.docker_running,
                "known": s.is_known,
                "pipeline": s.info.get("pipeline"),
                "system": s.info.get("system"),
                "components": s.info.get("components"),
            })
    return JSONResponse(result)


async def _ui(req: Request) -> Response:
    """Web-UI (Dashboard) – single-file HTML."""
    path = os.path.join(_STATIC_DIR, "index.html")
    with open(path, "rb") as f:
        return Response(f.read(), media_type="text/html; charset=utf-8")


async def _status(req: Request) -> JSONResponse:
    """GET /api/status → Master-Uptime + Startzeit."""
    uptime_s = time.time() - _START_TIME
    return JSONResponse({
        "start_time": _START_TIME,
        "uptime_s": int(uptime_s),
        "uptime": _fmt_uptime(uptime_s),
    })


def _fmt_uptime(seconds: float) -> str:
    """Seconds → menschenlesbare Zeichenkette (Tage, Stunden, Minuten, Sekunden)."""
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}T")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


async def _live_log(req: Request) -> StreamingResponse:
    """GET /logs → SSE-Stream der letzten Log-Zeilen + neue Einträge."""
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=128)

    async def generator():
        # Erst die vorhandenen Zeilen senden
        with _log_lock:
            snapshot = list(_LOG_LINES)
        for line in snapshot:
            yield f"data: {line}\n\n"
        # Dann neue Zeilen live
        try:
            while True:
                if await req.is_disconnected():
                    break
                msg = await q.get()
                yield f"data: {msg}\n\n"
        finally:
            # Cleanup: Queue aus _log_clients entfernen (hier: keine Liste nötig,
            # da pro-Request eigener Queue + Generator endet bei Disconnect)
            pass

    return StreamingResponse(generator(), media_type="text/event-stream")


# ── App Setup ───────────────────────────────────────────────────

manager = SatManager()


def _on_sat_status(mac: str, sat) -> None:
    """Manager-Callback: Status-Wechsel per SSE an die Web-UI senden."""
    payload = {
        "mac": mac, "ip": sat.ip, "hostname": sat.hostname,
        "status": sat.status, "docker_running": sat.docker_running,
        "pipeline": sat.info.get("pipeline"),
        "components": sat.info.get("components"),
        "system": sat.info.get("system"),
    }
    try:
        asyncio.get_running_loop().call_soon_threadsafe(
            _broadcast, "sat_status", payload)
    except RuntimeError:
        _broadcast("sat_status", payload)  # Fallback (selbe Loop)


manager.set_status_callback(_on_sat_status)

routes = [
    Route("/", _ui),
    Route("/healthz", lambda req: JSONResponse({"ok": True})),
    Route("/api/status", _status),
    Route("/logs", _live_log),
    Route("/api/heartbeat", _heartbeat),
    Route("/api/music", _api_music),
    Route("/api/music/search", _api_music),  # Alias für Satelliten
    Route("/index/rebuild", _index_rebuild, methods=["POST"]),
    Route("/events", _events),
    Route("/api/satellite/{mac}/restart", _sat_restart, methods=["POST"]),
    Route("/api/satellite/{mac}/pause", _sat_pause, methods=["POST"]),
    Route("/api/satellite/{mac}/unpause", _sat_unpause, methods=["POST"]),
    Route("/api/satellite/{mac}/heartbeat", _sat_heartbeat, methods=["POST"]),
    Route("/api/satellites", _satellite_list),
    Route("/api/satellites/status", _satellites_status),
    # (b) Zentrale Konfigurationsverwaltung
    Route("/api/config", _config_get),
    Route("/api/config", _config_put, methods=["PUT"]),
    Route("/api/config/apply", _config_apply_get, methods=["GET"]),
    Route("/api/config/export", _config_export),
    Route("/api/config/import", _config_import, methods=["POST"]),
    Route("/api/config/satellite/{mac}", _config_sat_get),
    # (c) Zentrale API-Proxy-Endpunkte (TTS/STT/LLM)
    Route("/proxy/tts", proxy.proxy_tts, methods=["GET", "HEAD", "POST"]),
    Route("/proxy/stt", proxy.proxy_stt, methods=["GET", "HEAD", "POST"]),
    Route("/proxy/llm", proxy.proxy_llm, methods=["GET", "HEAD", "POST"]),
    Route("/proxy/openhab/{subpath:path}", proxy.proxy_openhab, methods=["GET", "POST", "PUT", "DELETE"]),
    Route("/proxy/music/{subpath:path}", proxy.proxy_music,
          methods=["GET", "HEAD"]),
    # (d) Zentrales MCP-Gateway (Tools aller Satelliten)
    Route("/mcp/tools", mcp_gateway.mcp_tools_all),
    Route("/mcp/tools/{mac}", mcp_gateway.mcp_tools_one),
    Route("/mcp", mcp_gateway.mcp_call, methods=["POST"]),
    Route("/mcp/{mac}", mcp_gateway.mcp_call, methods=["POST"]),
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
