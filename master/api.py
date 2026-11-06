"""Master-API – httpx/uvicorn.

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
import re
import threading
import time

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import (FileResponse, JSONResponse,
                                 PlainTextResponse, Response,
                                 StreamingResponse)
from starlette.routing import Route

from . import chatstore
from . import config as master_cfg
from . import __version__ as MASTER_VERSION
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

# Hintergrund-Tasks (Zielgerät-Broadcast) referenziert halten gegen GC
_bg_tasks: set = set()
# aktive Live-Log-SSE-Queues (GET /logs) — werden thread-sicher befüllt
_log_clients: list[asyncio.Queue] = []
_log_clients_lock = threading.Lock()
# Main-Event-Loop (wird bei erster SSE-Verbindung gemerkt)
_main_loop: asyncio.AbstractEventLoop | None = None


def _push_log_to_clients(msg: str) -> None:
    """Neue Live-Log-Zeile in alle SSE-Queues stellen (aus jedem Thread sicher).

    Der Log-Handler läuft in Fremd-Threads (Manager-Monitor-Thread), daher
    immer via call_soon_threadsafe auf die im Startup gemerkte Main-Loop.
    Volle Queue = langsamer Client: Zeile verwerfen, Stream nicht blockieren.
    """
    with _log_clients_lock:
        clients = list(_log_clients)
    if not clients or _main_loop is None or _main_loop.is_closed():
        return
    for q in clients:
        try:
            if q.full():
                continue
            _main_loop.call_soon_threadsafe(q.put_nowait, msg)
        except RuntimeError:
            pass


def _append_log_line(msg: str) -> None:
    """Einen Text-Zeilen-Eintrag zum Live-Log-Puffer hinzufügen."""
    with _log_lock:
        _LOG_LINES.append(msg)
        if len(_LOG_LINES) > _LOG_MAX:
            del _LOG_LINES[:_LOG_MAX]
    _push_log_to_clients(msg)

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


# ── Satelliten-Zugriff (MASTER_TOKEN) ──────────────────────────

_reject_logged: dict = {}


def _sat_token() -> str:
    """Token, mit dem Satelliten den Master ansprechen (Heartbeat, Config-Pull).

    Quelle: zentrales Secret MASTER_TOKEN (UI-Bereich Secrets) mit ENV-Fallback
    MASTER_TOKEN. Leer = offen (abwaertskompatibel zu aelteren Satelliten)."""
    tok = config_store.get_secret("MASTER_TOKEN")
    if tok:
        return str(tok).strip()
    import os
    return os.environ.get("MASTER_TOKEN", "").strip()


def _sat_auth_ok(req: Request, who: str) -> bool:
    """Satelliten-Zugriff pruefen (Bearer MASTER_TOKEN).

    Ohne gesetztes MASTER_TOKEN bleibt der Zugang offen (wie bisher). Pro
    Satellit und 5 Minuten eine Warnung ins Live-Log, damit ein falsch
    konfigurierter Satellit das Log nicht zuspamt."""
    tok = _sat_token()
    if not tok:
        return True
    got = req.headers.get("authorization", "") or req.headers.get("x-master-token", "")
    if got.startswith("Bearer "):
        got = got[7:]
    import hmac
    if got and hmac.compare_digest(str(got), tok):
        return True
    now = time.time()
    if now - _reject_logged.get(who, 0.0) > 300:
        _reject_logged[who] = now
        log.warning("Satelliten-Zugriff abgelehnt (%s): MASTER_TOKEN fehlt oder falsch",
                    who)
    return False


# ── Satellite-Heartbeat (Push von den Satelliten) ──────────────

async def _sat_heartbeat(req: Request) -> JSONResponse:
    """POST /api/satellite/{mac}/heartbeat – Satellite pusht seinen Status.

    Payload: {hostname, ip, docker_running, pipeline, system, components}
    Der Master aktualisiert last_seen, Hostname, IP, Pipeline-/Komponenten-Info.
    """
    mac = req.path_params["mac"].lower()
    if not _sat_auth_ok(req, mac):
        return JSONResponse({"error": "unauthorized",
                             "detail": "MASTER_TOKEN fehlt oder falsch"},
                            status_code=401)
    try:
        payload = await req.json()
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    # Die echte Host-IP (von NAT) statt der Container-IP aus dem Payload
    if req.client:
        payload["ip"] = req.client.host
    # Live-Log-Events des Satelliten: vorher herausloesen, damit sie nicht im
    # Status-Objekt (UI/Config-Export) landen. Der Master loggt sie erst nach
    # dem Heartbeat, damit sein Satellitenname schon bekannt ist.
    events = payload.pop("events", None)
    events_reset = bool(payload.pop("events_reset", False))
    # Chat-Eintraege (Sprech-Turne + Musik) des Satelliten – Chat-WebUI.
    # record_heartbeat ZUERST (legt den Satelliten im Registry an), dann den
    # kanonischen Registry-Key (stabile IP statt Docker-Bridge-MAC) aufloesen
    # und die Eintraege unter GLEICHDEM Key speichern, den die Chat-WebUI bei
    # Geratefilterung nutzt – sonst taucht bei ausgewaehltem Gerat kein
    # Textinhalt auf.
    chat = payload.pop("chat", None)
    manager.record_heartbeat(mac, payload)
    canon = manager.canonical_key(mac, str(payload.get("ip") or "")) or mac
    chat_stored = 0
    if isinstance(chat, list):
        try:
            chat_stored = chatstore.add_sats_chat(canon, chat)
        except Exception as e:
            log.warning("Chat-Eintraege (%s): %s", canon, e)
    n = 0
    if isinstance(events, list):
        n = manager.record_events(mac, events, events_reset,
                                  ip=str(payload.get("ip") or ""))
    return JSONResponse({"ok": True, "events_stored": n,
                         "chat_stored": chat_stored})


# ── Chat-WebUI ────────────────────────────────────────────────
# Chatfenster im WebUI: Eintraege der Satelliten (Sprech-Turne + Musik,
# gepusht per Push/Heartbeat), TTS-Aufnahmen (Master zeichnet ueber den
# TTS-Proxy mit) und ein Web-Chat mit eigener LLM/TTS-Frage-Antwort.

def _chat_ui_file(name: str) -> Response:
    path = os.path.join(_STATIC_DIR, name)
    with open(path, "rb") as f:
        return Response(f.read(), media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


async def _chat_ui(req: Request) -> Response:
    """Web-Chat (chat.html) – eigenes Tab in der Master-WebUI."""
    return _chat_ui_file("chat.html")


async def _chat_push(req: Request) -> JSONResponse:
    """POST /api/satellite/{mac}/chat – Satellite pusht Chat-Eintraege.
    Body: {entries: [{eid, ts, role, text, path}]}. Antwort: {chat_ack}."""
    mac = req.path_params["mac"].lower()
    if not _sat_auth_ok(req, mac):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = await req.json()
    except (json.JSONDecodeError, ValueError):
        payload = {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    # Die direkt gepushten Eintraege haelten selbst keine IP; die Quell-IP
    # (req.client.host) ist aber dieselbe wie beim Heartbeat und damit der
    # Match fuer s.ip.
    ip = str(payload.get("ip") or "") if isinstance(payload, dict) else ""
    ip = ip or (req.client.host if req.client else "") or ""
    # Kanonischer Registry-Key (stabile IP statt Bridge-MAC), wie beim
    # Heartbeat – sonst findet die WebUI die Eintraege bei Geratefilter nicht.
    canon = manager.canonical_key(mac, ip) or mac
    try:
        ack = chatstore.add_sats_chat(canon, entries if isinstance(entries, list) else [])
    except Exception as e:
        log.warning("Chat-Push (%s): %s", canon, e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "chat_ack": ack})


async def _chat_messages(req: Request) -> JSONResponse:
    """GET /api/chat/messages?mac=&limit= → Eintraege, neueste zuerst."""
    mac = (req.query_params.get("mac") or "").strip() or None
    try:
        limit = int(req.query_params.get("limit", "200"))
    except ValueError:
        limit = 200
    entries = chatstore.chat_entries(mac, min(max(limit, 1), 500))
    return JSONResponse({"entries": entries})


async def _chat_recs(req: Request) -> JSONResponse:
    """GET /api/chat/recs → alle TTS-Aufnahmen (key, size, mtime)."""
    return JSONResponse({"recs": chatstore.list_recordings()})


async def _chat_rec(req: Request) -> Response:
    """GET /api/chat/rec/{key}.wav → eine TTS-Aufnahme (Abitur-Playback)."""
    key = req.path_params["key"]
    if not re.fullmatch(r"[a-f0-9]{12,32}", key):
        return PlainTextResponse("ungueltiger key", status_code=400)
    fn = os.path.join(chatstore.RECS_DIR, key + ".wav")
    if not os.path.isfile(fn):
        return PlainTextResponse("nicht gefunden", status_code=404)
    return FileResponse(fn, media_type="audio/wav")


async def _chat_send(req: Request) -> JSONResponse:
    """POST /api/chat/send {text, mac, speak} → LLM-Antwort (+TTS-Aufnahme).

    mac (optional): Konfig des gewaehlten Satelliten verwenden (LLM/TTS-
    Modelle), sonst globale. TTS-Antwort wird aufgezeichnet und mit
    'rec' zurueckgegeben – die Chat-UI spielt sie ab.
    """
    try:
        body = await req.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "ungueltiges JSON"}, status_code=400)
    text = (body.get("text") or "").strip()
    mac = (body.get("mac") or "").lower().strip()
    speak = bool(body.get("speak", True))
    # Zielgerät der Ausgabe: 'browser' | '' (=browser) | 'all' (=alle
    # Online-Satelliten) | MAC/IP/Hostname eines Satelliten. Bestimmt, WO
    # der Ausgabetext (Antwort) gesprochen wird.
    out = (body.get("out") or "").strip().lower()
    if out in ("", "web"):
        out = "browser"
    if not text:
        return JSONResponse({"error": "leere Nachricht"}, status_code=400)
    chatstore.add_chat(mac or "web", "user", text, who="web")

    cfg = config_store.get_effective(mac or None)
    import httpx
    url = master_cfg.LLM_BACKEND_URL
    tok = proxy._upstream_token("LLM_TOKEN", proxy.ENV_LLM_TOKEN)
    if not url:
        answer, rec = "LLM ist nicht konfiguriert.", None
    else:
        payload = {"model": cfg.get("LLM_MODEL") or "",
                   "messages": [{"role": "user", "content": text}],
                   "stream": False}
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                r = await client.post(url, json=payload,
                                      headers=proxy._headers(tok, req))
                data = r.json()
                answer = (((data.get("choices") or [{}])[0]
                           .get("message") or {}).get("content") or "").strip()
        except Exception as e:
            log.warning("Chat-LLM: %s: %s", type(e).__name__, e)
            answer = f"LLM-Fehler ({type(e).__name__})"
    if not answer:
        answer = "(leere Antwort)"
    rec = None
    want_tts = speak and answer and not answer.startswith("LLM")
    # Browser-Ausgabe: Master synthetisiert die Antwort als Aufnahme zum
    # Abspielen. Bei Satellit-Ausgabe spricht das Zielgerät selbst (/speak) —
    # dann keine Doppel-TTS auf der Master-Seite.
    if want_tts and out == "browser":
        rec = chatstore.key_for(answer)
        try:
            twav = await _synth_tts(cfg, answer)
            if twav:
                chatstore.save_recording(rec, twav)
            else:
                rec = None
        except Exception as e:
            log.warning("Chat-TTS: %s", e)
            rec = None
    entry = chatstore.add_chat(mac or "web", "assistant", answer,
                               rec=rec, out=out)
    # Zielgerät-Broadcast: Ausgewählter Satellit spricht die Antwort laut
    # (/speak, wait=false). Feuer-und-vergiss, damit die Chat-Antwort nicht
    # auf dem Gerät blockiert (wie /api/broadcast).
    broadcast = None
    if want_tts and out != "browser":
        task = asyncio.create_task(_sat_control(
            out, "/speak", {"text": answer, "wait": False}, timeout=60))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        broadcast = "started"
    return JSONResponse({"answer": answer, "rec": rec, "ts": entry["ts"],
                         "out": out, "broadcast": broadcast})


async def _synth_tts(cfg: dict, text: str) -> bytes | None:
    """Antwort ueber den TTS-Backend sprachlich erzeugen (Voll-WAV)."""
    import httpx
    url = master_cfg.TTS_BACKEND_URL
    if not url:
        return None
    tok = proxy._upstream_token("TTS_TOKEN", proxy.ENV_TTS_TOKEN)
    model = cfg.get("TTS_MODEL") or ""
    voice = cfg.get("TTS_VOICE") or ""
    payload = {"model": model, "voice": voice, "input": text,
               "response_format": "wav"}
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.post(url, json=payload, headers=headers)
        if r.status_code != 200 or not r.content:
            log.warning("Chat-TTS: HTTP %d", r.status_code)
            return None
        return r.content


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
    if not _sat_auth_ok(req, mac):
        return JSONResponse({"error": "unauthorized",
                             "detail": "MASTER_TOKEN fehlt oder falsch"},
                            status_code=401)
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
        return Response(f.read(), media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


async def _status(req: Request) -> JSONResponse:
    """GET /api/status → Master-Uptime + Startzeit."""
    uptime_s = time.time() - _START_TIME
    return JSONResponse({
        "start_time": _START_TIME,
        "uptime_s": int(uptime_s),
        "uptime": _fmt_uptime(uptime_s),
        "version": MASTER_VERSION,
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
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=128)
    with _log_clients_lock:
        _log_clients.append(q)

    async def generator():
        try:
            # Erst die vorhandenen Zeilen senden
            with _log_lock:
                snapshot = list(_LOG_LINES)
            for line in snapshot:
                yield f"data: {line}\n\n"
            # Dann neue Zeilen live (Queue wird von _append_log_line befüllt)
            while True:
                if await req.is_disconnected():
                    break
                msg = await q.get()
                yield f"data: {msg}\n\n"
        finally:
            with _log_clients_lock:
                if q in _log_clients:
                    _log_clients.remove(q)

    return StreamingResponse(generator(), media_type="text/event-stream")


# ── App Setup ───────────────────────────────────────────────────

manager = SatManager()


# Zuletzt an die UI gemeldeter Status pro Satellit (Live-Log-Dedupe)
_last_status_sent: dict[str, tuple] = {}


def _on_sat_status(mac: str, sat) -> None:
    """Manager-Callback: Status-Wechsel per SSE an die Web-UI senden.

    Der Manager ruft das Callback bei JEDEM Heartbeat (und jeden
    Monitor-Tick) – nicht nur bei echten Wechseln. Ohne Dedupe würde die
    UI-Zeile „Satellit X: online“ alle Heartbeats (60 s) erneut im
    Live-Log erscheinen; nur Status-Änderungen (online/offline/unknown,
    Container-Start/Stopp) breiten wir aus. Die Satellitentabelle holt
    sich ihre Daten ohnehin per 30-s-Poll und bleibt aktuell."""
    # Dedupe-Signatur = (Host-Status, Activity-Phase). Damit erwischt die
    # Web-UI sowohl echte Statuswechsel (online/offline) ALS AUCH jede
    # Aenderung der Pipeline-Phase – die Spalte 'Aktivitaet' bleibt live
    # aktuell, statt erst beim naechsten 30-s-Poll aufzufrischen.
    act = manager.activity(sat)
    prev = _last_status_sent.get(mac)
    sig = (sat.status, act)
    if prev is not None and prev == sig:
        return
    # Live-Log-Zeile nur bei echtem Statuswechsel (online/offline) – nicht
    # bei jeder Phase-Wechselung, sonst wuerde das Log vollgespamt.
    log_status = prev is None or prev[0] != sat.status
    _last_status_sent[mac] = sig
    payload = {
        "mac": mac, "ip": sat.ip, "hostname": sat.hostname,
        "status": sat.status, "docker_running": sat.docker_running,
        "activity": act,
        "pipeline": sat.info.get("pipeline"),
        "components": sat.info.get("components"),
        "system": sat.info.get("system"),
    }
    payload["log"] = log_status  # UI: Live-Log-Zeile nur bei echtem Statuswechsel
    try:
        asyncio.get_running_loop().call_soon_threadsafe(
            _broadcast, "sat_status", payload)
    except RuntimeError:
        _broadcast("sat_status", payload)  # Fallback (selbe Loop)


manager.set_status_callback(_on_sat_status)


# ── Steuerung via openHAB / curl (Broadcast, Lautstärke, Audio-Ausgabe) ──
#
# Der Master spricht die Web-Dienste der Satelliten direkt an (Base-URL
# aus mac/ip + web_port, gemeldet im Heartbeat). Damit ist jede Raum-
# Durchsage, Lautstärkeänderung und Ausgabe-Umschaltung mit einem
# curl-Befehl möglich – z.B. aus einer openHAB-Rule:
#
#   # Durchsage an einen Raum / alle Räume:
#   curl -s -X POST $MASTER/api/broadcast \
#        -H "Authorization: Bearer $TOKEN" \
#        -d '{"sat":"wohnzimmer","text":"Essen ist fertig"}'
#
#   # Lautstärke setzen (0-100) / abfragen:
#   curl -s -X POST $MASTER/api/satellite/wohnzimmer/volume \
#        -d '{"volume":40}'          # oder {"volume":0,"muted":true}
#   curl -s $MASTER/api/satellite/wohnzimmer/volume
#
#   # Audio-Ausgabe umschalten / abrufen:
#   curl -s -X POST $MASTER/api/satellite/wohnzimmer/output -d '{"out":"plughw:1,0"}'
#   curl -s $MASTER/api/satellite/wohnzimmer/output
#
# 'sat' ist MAC, IP oder (Teil-)Hostnamen; leere/fehlende sat = alle
# bekannten, online Satelliten.
#
import httpx as _httpx


async def _sat_control(key: str, endpoint: str, payload: dict | None = None,
                       timeout: float = 60.0) -> JSONResponse:
    """Einen Steuer-Endpunkt AUF dem Satelliten aufrufen.

    key leert / 'all' / '*' → an ALLE bekannten, online Satelliten, sonst
    an den einen (MAC/IP/Hostnamen). Liefert je eine JSONResponse: bei
    Einzelziel die Antwort des Satelliten, bei Broadcast eine Zusammenfassung
    pro Ziel."""
    key = (key or "").strip().lower()
    if key in ("", "all", "*"):
        with manager._lock:
            targets = [
                (k, f"http://{s.ip}:{SatManager._web_port(s)}")
                for k, s in manager.satellites.items()
                if s.status == "online" and s.ip
            ]
    else:
        hit = manager.sat_target(key)
        targets = [hit] if hit else []
    if not targets:
        return JSONResponse(
            {"ok": False,
             "error": ("kein Satellit gefunden" if key not in ("", "all", "*")
                       else "keine bekannten, online Satelliten")},
            status_code=404)

    import json as _json
    async def _call(t):
        url = t[1] + endpoint
        try:
            async with _httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(url, json=(payload or {}))
                try:
                    body = r.json()
                except Exception:
                    body = {"raw": r.text[:400]}
                return t[0], r.status_code, body
        except Exception as e:
            return t[0], 502, {"error": str(e)}

    if len(targets) == 1:
        mac, code, body = await _call(targets[0])
        out = dict(body)
        out.setdefault("ok", code == 200)
        return JSONResponse(out, status_code=code)
    results = await asyncio.gather(*[_call(t) for t in targets])
    summary = [
        {"sat": mac, "ok": code == 200, "code": code, "detail": body}
        for mac, code, body in results
    ]
    return JSONResponse({"ok": all(s["ok"] for s in summary),
                         "targets": summary})


async def _ctl_body(req: Request) -> dict:
    """JSON-Body einer POST-Steueranfrage (leer, wenn nicht vorhanden)."""
    import json as _json
    try:
        raw = await req.body()
        return _json.loads(raw or b"{}") or {}
    except Exception:
        return {}


async def _openhab_broadcast(req: Request) -> JSONResponse:
    """POST /api/broadcast {sat?, text, vol?} – Durchsage ohne Wake/LLM."""
    body = await _ctl_body(req)
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "text fehlt"})
    payload = {"text": text, "wait": bool(body.get("wait", True))}
    if body.get("vol") is not None:
        payload["vol"] = int(body["vol"])
    return await _sat_control(body.get("sat") or "", "/speak", payload,
                              timeout=max(5.0, float(body.get("wait_ms", 60.0)) / 1000.0 + 30.0))


async def _openhab_volume(req: Request) -> JSONResponse:
    """POST /api/satellite/{mac}/volume {volume?, muted?}"""
    mac = req.path_params["mac"]
    body = await _ctl_body(req)
    if body.get("volume") is None and body.get("muted") is None:
        return JSONResponse({"ok": False, "error": "volume oder muted fehlt"})
    return await _sat_control(mac, "/volume", body)


async def _openhab_volume_get(req: Request) -> JSONResponse:
    """GET /api/satellite/{mac}/volume – aktueller Zustand."""
    mac = req.path_params["mac"]
    hit = manager.sat_target(mac)
    if not hit:
        return JSONResponse({"ok": False, "error": "Satellit nicht bekannt"},
                            status_code=404)
    key, base = hit
    try:
        async with _httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(base + "/volume")
            return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


async def _openhab_output(req: Request) -> JSONResponse:
    """POST /api/satellite/{mac}/output {out|device} – Ausgabe umschalten."""
    mac = req.path_params["mac"]
    body = await _ctl_body(req)
    dev = body.get("out") or body.get("device")
    if not dev:
        return JSONResponse({"ok": False, "error": "out (Gerätename) fehlt"})
    return await _sat_control(mac, "/audio/out", {"out": dev})


async def _openhab_output_get(req: Request) -> JSONResponse:
    """GET /api/satellite/{mac}/output – verfügbare + aktuelle Geräte."""
    mac = req.path_params["mac"]
    hit = manager.sat_target(mac)
    if not hit:
        return JSONResponse({"ok": False, "error": "Satellit nicht bekannt"},
                            status_code=404)
    key, base = hit
    try:
        async with _httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(base + "/audio/out")
            return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


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
    # Chat-WebUI
    Route("/chat.html", _chat_ui),
    Route("/api/satellite/{mac}/chat", _chat_push, methods=["POST"]),
    Route("/api/chat/messages", _chat_messages),
    Route("/api/chat/recs", _chat_recs),
    Route("/api/chat/rec/{key}", _chat_rec),
    Route("/api/chat/send", _chat_send, methods=["POST"]),
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
    Route("/proxy/openhab", proxy.proxy_openhab, methods=["GET", "POST", "PUT", "DELETE"]),
    Route("/proxy/openhab/{subpath:path}", proxy.proxy_openhab, methods=["GET", "POST", "PUT", "DELETE"]),
    Route("/proxy/music", proxy.proxy_music, methods=["GET", "HEAD"]),
    Route("/proxy/music/{subpath:path}", proxy.proxy_music,
          methods=["GET", "HEAD"]),
    # (d) Zentrales MCP-Gateway (Tools aller Satelliten)
    # openHAB / curl-Steuerung: Broadcast, Lautstärke, Audio-Ausgabe.
    # 'sat' ist MAC/IP/Hostnamen; leer = alle online Satelliten (Broadcast).
    Route("/api/broadcast", _openhab_broadcast, methods=["POST"]),
    Route("/api/satellite/{mac}/volume", _openhab_volume, methods=["POST"]),
    Route("/api/satellite/{mac}/volume", _openhab_volume_get, methods=["GET"]),
    Route("/api/satellite/{mac}/output", _openhab_output, methods=["POST"]),
    Route("/api/satellite/{mac}/output", _openhab_output_get, methods=["GET"]),
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
