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
from .satconfig import store as config_store, EDITABLE_KEYS

log = logging.getLogger("master.api")

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

    # Nur erlaubte Schlüssel setzen.
    g = {k: v for k, v in (data.get("global") or {}).items() if k in EDITABLE_KEYS}
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
    # Push an alle bekannten Satelliten (aktiv, ohne Warten auf nächsten Pull).
    asyncio.get_running_loop().create_task(_push_config_to_all())
    return JSONResponse({"ok": True, "version": version})


async def _config_sat_get(req: Request) -> JSONResponse:
    """GET /api/config/satellite/{mac} → gültige (merged) Konfiguration."""
    mac = req.path_params["mac"].lower()
    return JSONResponse(config_store.get_effective(mac))


async def _config_push(req: Request) -> JSONResponse:
    """POST /api/config/push → Config an alle (online) Satelliten pushen."""
    await _push_config_to_all()
    return JSONResponse({"ok": True})


async def _config_apply(req: Request) -> JSONResponse:
    """GET /api/config/apply?mac=...&version=... → Satellite-Pull.

    POST (ohne mac) → aktiv an alle Satelliten pushen (WebUI-Button).
    """
    if req.method == "POST":
        await _push_config_to_all()
        return JSONResponse({"ok": True})
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


async def _push_config_to_all() -> None:
    """Aktive Config-Push an alle (online) Satelliten (außerhalb SSE-Loop)."""
    import httpx
    with manager._lock:
        targets = [(s.mac, s.ip) for s in manager.satellites.values()
                   if s.status == "online" and s.ip]
    if not targets:
        return
    port = os.environ.get("SATELLITE_WEB_PORT", "8793")
    ver = config_store.version()
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        for mac, ip in targets:
            eff = config_store.get_effective(mac)
            overrides = {k: _normalize_override(v)
                         for k, v in eff.items() if v is not None}
            try:
                r = await client.post(
                    f"http://{ip}:{port}/apply-config",
                    json={"mac": mac, "version": ver, "overrides": overrides})
                log.debug("Config-Push %s → %d", ip, r.status_code)
            except Exception as ex:
                log.debug("Config-Push %s fehlgeschlagen: %s", ip, ex)


async def _push_config_sat(req: Request) -> JSONResponse:
    """POST /api/config/push/{mac} → gezielt einen Satellite pushen."""
    mac = req.path_params["mac"].lower()
    sat = manager.satellites.get(mac)
    if not sat or not sat.ip:
        return JSONResponse({"error": "satellite not found"}, status_code=404)
    import httpx
    port = os.environ.get("SATELLITE_WEB_PORT", "8793")
    eff = config_store.get_effective(mac)
    overrides = {k: _normalize_override(v) for k, v in eff.items() if v is not None}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            r = await client.post(
                f"http://{sat.ip}:{port}/apply-config",
                json={"mac": mac, "version": config_store.version(),
                      "overrides": overrides})
        return JSONResponse({"ok": r.status_code == 200, "status": r.status_code})
    except Exception as ex:
        return JSONResponse({"error": f"push failed: {ex}"}, status_code=502)


def _on_config_change() -> None:
    """Config-Store-Callback (außerhalb des Locks): UI über Änderung informieren."""
    payload = {"version": config_store.version()}
    try:
        asyncio.get_running_loop().call_soon_threadsafe(
            _broadcast, "config_change", payload)
    except RuntimeError:
        _broadcast("config_change", payload)


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
config_store.set_change_callback(_on_config_change)

routes = [
    Route("/", _ui),
    Route("/healthz", lambda req: JSONResponse({"ok": True})),
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
    Route("/api/config/apply", _config_apply, methods=["GET", "POST"]),
    Route("/api/config/push", _config_push, methods=["POST"]),
    Route("/api/config/satellite/{mac}", _config_sat_get),
    Route("/api/config/push/{mac}", _push_config_sat, methods=["POST"]),
    # (c) Zentrale API-Proxy-Endpunkte (TTS/STT/LLM)
    Route("/proxy/tts", proxy.proxy_tts, methods=["POST"]),
    Route("/proxy/stt", proxy.proxy_stt, methods=["POST"]),
    Route("/proxy/llm", proxy.proxy_llm, methods=["POST"]),
    Route("/proxy/openhab", proxy.proxy_openhab, methods=["GET", "POST", "PUT", "DELETE"]), 
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
