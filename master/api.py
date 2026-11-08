"""Master-API – httpx/uvicorn.

Endpoints:
  GET    /api/heartbeat        → Status + Sat-Liste
  POST   /api/satellite/{mac}/restart
  POST   /api/satellite/{mac}/pause
  POST   /api/satellite/{mac}/disable|enable → Satellit an-/abschalten
  POST   /api/image            → Bild an ein Gerät / alle Geräte pushen
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

from . import appupdate
from . import chatstore
from . import chatstream, llmmemory
from . import config as master_cfg
from . import __version__ as MASTER_VERSION
from . import proxy
from . import mcp_gateway
from . import webauth
from .indexer import _build_media_index, load_index_cache, save_index
from .manager import SatManager
from .satconfig import (store as config_store, EDITABLE_KEYS,
                          GLOBAL_ONLY_KEYS, SECRET_KEYS, BACKEND_KEYS,
                          URL_KEYS)

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


def _chat_progress(state: str, **extra) -> None:
    """Chat-Verarbeitungsphase an die WebUI melden (SSE-Event chat_progress)."""
    _broadcast("chat_progress", {"state": state, **extra})


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
    # Deaktivierter Satellit (Pi oder App) darf keine Meldungen mehr an den
    # Master senden — Heartbeat wird abgelehnt, das Gerät fällt im Monitor
    # auf offline und bekommt keine Warteschlange mehr ab.
    if manager.is_disabled_for(mac, str(payload.get("ip") or "")):
        return JSONResponse(
            {"error": "deaktiviert",
             "detail": "Satellit ist vom Master deaktiviert"},
            status_code=403)
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
    # Mini-Config-Pull: die Android-App (kein Pi-Config-Client) holt ihre
    # Display-Bilder ueber den Heartbeat. Nur anzeigerelevante, nicht-geheime
    # Werte aus der effektiven Config (Global + Sat-Override).
    try:
        # Geraete-Override falls mac als Satelit konfiguriert ist, sonst global.
        eff = config_store.get_effective(mac)
        cfg = {"default_image_url": str(eff.get("DEFAULT_IMAGE_URL") or ""),
               # Zentrale Intent-Liste (Ausloeser|Item|Wert, eine Zeile pro
               # Intent): die Android-App schaltet damit lokal ohne LLM.
               "intents": str(eff.get("INTENTS") or "")}
    except Exception as e:
        log.warning("Heartbeat-Config-Pull (%s): %s", mac, e)
        cfg = {}
    # Vorab registrierter Key (kanonisch) fuer Durchsage-Warteschlange:
    # Android-Apps holen offene Nachrichten mit dem Heartbeat ab.
    msgs = manager.drain_pending(canon)
    if canon != mac:
        msgs = manager.drain_pending(mac) + msgs
    # App-Update-Hinweis: liegt in der apps-Ablage ein strikt hoeheres APK
    # als die gemeldete versionName, erfaehrt die Android-App im Ack die
    # Download-Info (version/file/url/size); sie zeigt sie an und holt die
    # Datei selbst. Pi-Satelliten bekommen nie einen Hinweis.
    upd = appupdate.update_hint(str(payload.get("version") or ""),
                                str(payload.get("kind") or ""))
    ack = {"ok": True, "events_stored": n, "chat_stored": chat_stored,
           "config": cfg, "messages": msgs}
    if upd:
        ack["update"] = upd
    return JSONResponse(ack)


# ── App-Update-Verteilung (APKs an die Android-Apps) ──────────

async def _app_version(req: Request) -> JSONResponse:
    """GET /api/app/version → Info ueber das neueste verteilbare APK."""
    top = appupdate.latest()
    return JSONResponse({"ok": True, "latest": top,
                         "count": len(appupdate.list_apks())})


async def _app_download(req: Request) -> Response:
    """GET /api/app/download?file=<name> → APK-Datei zum Herunterladen.

    Ohne file-Parameter wird die neueste Version ausgeliefert. Die
    Aufloesung laeuft ueber appupdate.resolve_apk (nur kanonische Namen,
    kein Traversal)."""
    fn = str(req.query_params.get("file") or "").strip()
    if fn:
        path = appupdate.resolve_apk(fn)
    else:
        top = appupdate.latest()
        path = appupdate.resolve_apk(top["file"]) if top else None
    if not path:
        return JSONResponse({"error": "not found",
                             "detail": "APK nicht in der apps-Ablage"},
                            status_code=404)
    return FileResponse(path,
                        media_type="application/vnd.android.package-archive",
                        filename=os.path.basename(path))


# ── Benutzerverwaltung: Login, Session, Rollen ───────────────
# Menschlicher WebUI-Zugriff (Browser) wird hier gefuehrt; die
# Maschinen-Seite (Satelliten, Apps, openHAB-curl) bleibt bei
# MASTER_TOKEN und wird unten im Gate ausgenommen.

async def _login(req: Request) -> JSONResponse:
    """POST /login {username, password} → Session-Cookie 'vs_sid'."""
    try:
        body = await req.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "ungueltiges JSON"}, status_code=400)
    username = str(body.get("username") or body.get("user") or "")
    password = str(body.get("password") or "")
    role = webauth.verify_login(username, password)
    if role is None:
        return JSONResponse({"error": "Anmeldeangabe falsch"},
                            status_code=401)
    name = username.strip().lower()
    tok = webauth.start_session(name)
    resp = JSONResponse({"ok": True, "username": name, "role": role})
    resp.set_cookie("vs_sid", tok, httponly=True, samesite="lax",
                    path="/", max_age=webauth.SESSION_TTL_SEC)
    return resp


async def _logout(req: Request) -> JSONResponse:
    tok = req.cookies.get("vs_sid", "")
    if tok:
        webauth.end_session(tok)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("vs_sid", path="/")
    return resp


async def _me(req: Request) -> JSONResponse:
    """GET /api/me → eingeloggter Benutzer oder 401 (+needs_setup)."""
    user = _req_user(req)
    if user:
        return JSONResponse(user)
    return JSONResponse({"error": "unauthorized",
                         "needs_setup": webauth.needs_setup()},
                        status_code=401)


async def _auth_status(req: Request) -> JSONResponse:
    """GET /api/auth/status → ob ein Konto existiert (Ersteinrichtung-UI)."""
    return JSONResponse({"has_users": not webauth.needs_setup()})


async def _setup(req: Request) -> JSONResponse:
    """POST /api/setup {username, password} → erstes Admin-Konto.

    Nur waehrend der Store leer ist (Ersteinrichtung) — danach 403."""
    if not webauth.needs_setup():
        return JSONResponse(
            {"error": "Ersteinrichtung bereits abgeschlossen"},
            status_code=403)
    try:
        body = await req.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "ungueltiges JSON"}, status_code=400)
    err = webauth.create_user(str(body.get("username") or ""),
                              str(body.get("password") or ""), "admin")
    if err:
        return JSONResponse({"error": err}, status_code=400)
    name = str(body.get("username") or "").strip().lower()
    tok = webauth.start_session(name)
    resp = JSONResponse({"ok": True, "username": name, "role": "admin"})
    resp.set_cookie("vs_sid", tok, httponly=True, samesite="lax",
                    path="/", max_age=webauth.SESSION_TTL_SEC)
    return resp


async def _users_list(req: Request) -> JSONResponse:
    """GET /api/users → Konten (Admin, keine Hashes)."""
    return JSONResponse({"users": webauth.list_users()})


async def _users_create(req: Request) -> JSONResponse:
    """POST /api/users {username, password, role, display?} (Admin)."""
    try:
        body = await req.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "ungueltiges JSON"}, status_code=400)
    err = webauth.create_user(
        str(body.get("username") or ""), str(body.get("password") or ""),
        str(body.get("role") or "user"),
        display=str(body.get("display") or ""))
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse({"ok": True, "users": webauth.list_users()})


async def _users_update(req: Request) -> JSONResponse:
    """PUT /api/users/{name} {password?, role?, display?} (Admin).

    Felder fehlen = unveraendert (wie Config-PUTs: leer lassen)."""
    name = req.path_params["name"]
    try:
        body = await req.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "ungueltiges JSON"}, status_code=400)
    err = webauth.update_user(
        name,
        password=str(body.get("password") or ""),
        role=str(body.get("role") or ""),
        display="" if body.get("display") is None
        else str(body.get("display") or ""))
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse({"ok": True, "users": webauth.list_users()})


async def _users_delete(req: Request) -> JSONResponse:
    """DELETE /api/users/{name} (Admin)."""
    err = webauth.delete_user(req.path_params["name"])
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse({"ok": True, "users": webauth.list_users()})


async def _sat_name_put(req: Request) -> JSONResponse:
    """PUT /api/satellite/{key}/name {name} (Admin) → Anzeigename setzen.

    Leerer Name loescht den Eintrag. key = MAC/IP/Hostnamen/custom-Name."""
    key = req.path_params["mac"]
    try:
        body = await req.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    err = manager.set_name(key, str(body.get("name") or ""))
    if err:
        return JSONResponse({"error": err}, status_code=404)
    return JSONResponse({"ok": True, "name": manager.display_name(key)})


async def _sat_disable(req: Request) -> JSONResponse:
    """POST /api/satellite/{key}/disable (Admin) — Satellit abschalten.

    Deaktivierter Satellit (Pi oder App): keine Pushes mehr (Broadcast, DM,
    Chat-out, Bild-Push) und keine Meldungen an den Master (Heartbeat/Chat/
    DM → 403 'deaktiviert'). Persistenz: sat_disabled.json im Config-Volume."""
    key = req.path_params["mac"]
    err = manager.set_disabled(key, True)
    if err:
        return JSONResponse({"error": err}, status_code=404)
    return JSONResponse({"ok": True, "mac": key, "disabled": True})


async def _sat_enable(req: Request) -> JSONResponse:
    """POST /api/satellite/{key}/enable (Admin) — Satellit wieder aktivieren."""
    key = req.path_params["mac"]
    err = manager.set_disabled(key, False)
    if err:
        return JSONResponse({"error": err}, status_code=404)
    return JSONResponse({"ok": True, "mac": key, "disabled": False})


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
    # Deaktiviertes Gerät meldet keine Chats mehr.
    if manager.is_disabled_for(mac, ip):
        return JSONResponse(
            {"error": "deaktiviert",
             "detail": "Satellit ist vom Master deaktiviert"},
            status_code=403)
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


async def _chat_recq(req: Request) -> Response:
    """GET /api/chat/recq?text=… → TTS-Aufnahme zu einem Text.

    Der Master berechnet den Key selbst (chatstore.key_for). Die WebUI kann
    ihn nicht selbst bilden: crypto.subtle gibt es nur im Secure Context,
    nicht bei http://<LAN-IP> — sonst schlaegt der Browser-Playback still fehl.
    """
    text = req.query_params.get("text", "")
    if not text.strip():
        return PlainTextResponse("text fehlt", status_code=400)
    fn = os.path.join(chatstore.RECS_DIR, chatstore.key_for(text) + ".wav")
    if not os.path.isfile(fn):
        return PlainTextResponse("nicht gefunden", status_code=404)
    return FileResponse(fn, media_type="audio/wav")


async def _dm_core(from_key: str, from_name: str, to: str,
                   text: str) -> tuple[int, dict]:
    """Kern der Geräte-zu-Gerät-Direktnachricht (auch MCP-Tool 'send_dm').

    Deaktivierte Geräte werden übersprungen (kein 'all'-Ziel, kein
    Einzel-Ziel) — sie sind vom Admin abgeschaltet und empfangen nichts."""
    if not text or not to:
        return 400, {"ok": False, "error": "text und to erforderlich"}
    if to in ("all", "*"):
        with manager._lock:
            targets = [k for k, sa in manager.satellites.items()
                       if sa.status == "online" and k != from_key
                       and not sa.disabled]
        if not targets:
            return 404, {"ok": False, "error": "keine anderen online Geräte"}
        for k in targets:
            manager.queue_for(k, text, speak=False, src=from_name)
        return 200, {"ok": True, "queued": len(targets), "targets": targets}
    hit = manager.sat_target(to)
    if not hit:
        return 404, {"ok": False, "error": "Zielgerät nicht gefunden",
                     "to": to}
    target_key = hit[0]
    if target_key == from_key:
        return 200, {"ok": True, "queued": 0, "note": "eigenes Gerät"}
    if manager.is_disabled(target_key):
        return 403, {"ok": False, "error": "Zielgerät ist deaktiviert"}
    manager.queue_for(target_key, text, speak=False, src=from_name)
    return 200, {"ok": True, "queued": 1, "target": target_key}


async def _sat_dm(req: Request) -> JSONResponse:
    """POST /api/satellite/{mac}/dm {to, text} -> Geräte-zu-Gerät-Direktnachricht.

    Die sendende App meldet sich unter ihrem MAC/Device-ID an; 'to' ist das
    Zielgerät (MAC/IP/Hostname, 'all' = alle anderen Online-Geräte). Der Text
    geht OHNE LLM und OHNE TTS als reine Textnachricht in die Pull-Warteschlange
    des Ziels; das Ziel holt sie beim nächsten Heartbeat ab und zeigt sie mit der
    Quelle im Chat an. Android-Apps haben keinen Web-Dienst -> Pull statt Push."""
    mac = req.path_params["mac"].lower()
    if not _sat_auth_ok(req, mac):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await req.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    text = str((body.get("text") or "")).strip()[:1000]
    to = str((body.get("to") or "")).strip().lower()
    # Deaktiviertes Gerät sendet keine Nachrichten mehr (auch keine DMs).
    ip = (req.client.host if req.client else "") or ""
    if manager.is_disabled_for(mac, ip):
        return JSONResponse(
            {"error": "deaktiviert",
             "detail": "Gerät ist vom Master deaktiviert"},
            status_code=403)
    # Quellname: Anzeigename (custom > Hostname) des sendenden Geräts.
    from_name = (manager.display_name(mac) or mac)[:48]
    code, data = await _dm_core(mac, from_name, to, text)
    return JSONResponse(data, status_code=code)


async def _chat_intent(text: str, mac: str, out: str) -> str | None:
    """Intent-Bridge fuer den Master-Chat: vor dem LLM den Intent-Stack
    eines Satelliten fragen (POST /intent, intents_only) — openHAB-,
    Lautstaerke- und Musik-/Kamera-Befehle werden DORT ausgefuehrt und als
    Antworttext zurueckgegeben. Kein Match, kein Satellit, App ohne
    Web-Dienst oder Fehler -> None (Nachricht geht ans LLM wie bisher).
    """
    key = mac or (out if out not in ("", "browser", "web", "all") else "")
    if not key:
        # Browser-Chat ohne Ziel: erster online Pi-Satellit (Android-Apps
        # haben keinen Web-Dienst und koennen /intent nicht bedienen).
        with manager._lock:
            sats = [(k, sa) for k, sa in manager.satellites.items()
                    if sa.status == "online" and sa.ip and sa.kind != "android"
                    and not sa.disabled]
        if not sats:
            return None
        key = sats[0][0]
    payload = {"text": text, "intents_only": True}
    try:
        resp = await _sat_control(key, "/intent", payload, timeout=30.0)
        body = json.loads(resp.body)
    except Exception as e:
        log.debug("Chat-Intent-Bridge fehlgeschlagen: %s", e)
        return None
    if key in ("all", "*"):
        for t in (body.get("targets") or []):
            det = t.get("detail") or {}
            if det.get("handled"):
                return str(det.get("answer") or "")
        return None
    if body.get("handled"):
        return str(body.get("answer") or "")
    return None


async def _chat_send(req: Request) -> JSONResponse:
    """POST /api/chat/send {text, mac, speak, out} → LLM-Antwort (+TTS-Aufnahme).

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
    out = (body.get("out") or "").strip().lower()
    if out in ("", "web"):
        out = "browser"
    if not text:
        return JSONResponse({"error": "leere Nachricht"}, status_code=400)
    # Web-UI-Login-Name als Quelle (Benutzerverwaltung): erscheint in der
    # Chat-Anzeige und wandert als 'src' in den Zielgeräten mit.
    who = _req_user(req).get("display") or "web"
    # rid: Request-Id der WebUI (Live-Rendering gegen Gleichzeitigkeit)
    rid = str(body.get("rid") or "")[:32]
    result = await _chat_send_core(text, mac, speak, out, who, rid)
    return JSONResponse(result)


async def _chat_send_core(text: str, mac: str, speak: bool, out: str,
                          who: str = "web", rid: str = "") -> dict:
    """Kern des Chat-Sendens — wird auch vom MCP-Tool 'chat_send' genutzt.

    mac: Konfig des gewaehlten Satelliten (LLM/TTS), sonst globale.
    out: wo die Antwort gesprochen wird (browser | Satellit | all).
    rid: Request-Id (WebUI-Live-Rendering; leer = Master generiert neue).
    """
    mac = (mac or "").lower().strip()
    out = (out or "browser").strip().lower()
    if out in ("", "web"):
        out = "browser"
    if not text:
        raise ValueError("leere Nachricht")
    chatstore.add_chat(mac or "web", "user", text, who=who)
    _chat_progress("thinking")

    cfg = config_store.get_effective(mac or None)
    rid = (rid or "")[:32] or os.urandom(4).hex()
    import httpx
    url = proxy._upstream_url("LLM_URL", master_cfg.LLM_BACKEND_URL)
    tok = proxy._upstream_token("LLM_TOKEN", proxy.ENV_LLM_TOKEN)
    answer, think, rec = "", "", None
    # Intent-Bridge: "Licht an", "Musik stoppen", "lauter" … bedient der
    # Satellit; nur echter Freitext erreicht das LLM.
    intent_hit = await _chat_intent(text, mac, out)
    if intent_hit is not None:
        answer = intent_hit
        _chat_progress("intent", rid=rid, mac=mac or "web", out=out)
    elif not url:
        answer = "LLM ist nicht konfiguriert."
    else:
        # Streaming: Teilantwort und Thinking live an die UI (chat_progress
        # "stream"). Denk-Modelle melden reasoning_content VOR der Antwort;
        # der Hermes-Gateway (.33:8642) liefert beides als SSE-Deltas.
        # Gedächtnis (CHAT_MEMORY_ENABLED): letze N Chat-Eintraege dieses
        # Chats + Fakten aus dem Langzeitstore als Systemprompt — analog
        # zum Satellite (app/memory.py).
        if master_cfg.CHAT_MEMORY_ENABLED:
            msgs = llmmemory.build_messages(mac or "web", text)
        else:
            msgs = [{"role": "user", "content": text}]
        payload = {"model": cfg.get("LLM_MODEL") or "",
                   "messages": msgs,
                   "stream": True}
        try:
            mt = int(str(cfg.get("LLM_MAX_TOKENS") or "").strip() or 0)
            if mt > 0:
                payload["max_tokens"] = mt
        except ValueError:
            pass
        try:
            tmo = httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0)
            async with httpx.AsyncClient(timeout=tmo) as client:
                async with client.stream("POST", url, json=payload,
                                         headers=proxy._headers(tok, req)) as up:
                    if up.status_code != 200:
                        await up.aread()
                        raise RuntimeError(f"HTTP {up.status_code}")
                    last_push = 0.0
                    async for line in up.aiter_lines():
                        r_delta, c_delta = chatstream.split_sse_chunk(line)
                        think += r_delta
                        answer += c_delta
                        if (r_delta or c_delta) and time.time() - last_push >= 0.25:
                            last_push = time.time()
                            _chat_progress("stream", rid=rid, mac=mac or "web",
                                           out=out, think=think, answer=answer)
        except Exception as e:
            log.warning("Chat-LLM: %s: %s", type(e).__name__, e)
            if not answer:
                answer = f"LLM-Fehler ({type(e).__name__})"
    # Denktags: falls das Modell das Denken inline in content liefert
    answer, inline = chatstream.split_think_tags(answer.strip())
    if inline:
        think = (think + "\n" + inline).strip()
    if not answer:
        answer = "(leere Antwort)"
    # Antwort sofort melden: die UI zeigt sie, bevor die TTS fertig ist.
    _chat_progress("answer", answer=answer, think=think, mac=mac or "web",
                   out=out, ts=time.time(), rid=rid)
    rec = None
    want_tts = speak and answer and not answer.startswith("LLM")
    # Browser-Ausgabe: Master synthetisiert die Antwort als Aufnahme zum
    # Abspielen. Bei Satellit-Ausgabe spricht das Zielgerät selbst (/speak) —
    # dann keine Doppel-TTS auf der Master-Seite.
    if want_tts and out == "browser":
        rec = chatstore.key_for(answer)
        _chat_progress("tts")
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
                               rec=rec, out=out, think=think, who=who)
    # Langzeitgedächtnis: Fakten aus dem Turn extrahieren (Daemon-Thread,
    # blockiert die Antwort nicht). Nur bei echten Antworten, nicht bei
    # Fehler-Platzhaltern.
    if (master_cfg.CHAT_MEMORY_ENABLED and url and answer
            and intent_hit is None and not answer.startswith("LLM")):
        llmmemory.extract_facts_async(text, answer,
                                      cfg.get("LLM_MODEL") or "",
                                      key=mac or "web")
    # Zielgerät-Broadcast: Ausgewählter Satellit spricht die Antwort laut
    # (/speak, wait=false). Feuer-und-vergiss, damit die Chat-Antwort nicht
    # auf dem Gerät blockiert (wie /api/broadcast).
    broadcast = None
    if want_tts and out == "all":
        # "An alle": die Antwort wird GENAU EINMAL im Master synthetisiert
        # und das fertige Audio dann gleichzeitig an alle Online-Saetlinge
        # geschickt (statt jeder Saetling synthetisiert einzeln — das war
        # langsam und redundante Backend-Last). src = schreibender Benutzer.
        task = asyncio.create_task(_fanout_broadcast(cfg, answer, src=who))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        broadcast = "started"
    elif want_tts and out != "browser":
        task = asyncio.create_task(_sat_control(
            out, "/speak", {"text": answer, "wait": False, "src": who},
            timeout=60))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        broadcast = "started"
    _chat_progress("done")
    return {"answer": answer, "rec": rec, "ts": entry["ts"],
            "out": out, "broadcast": broadcast}



async def _synth_tts(cfg: dict, text: str) -> bytes | None:
    """Antwort ueber den TTS-Backend sprachlich erzeugen (Voll-WAV)."""
    import httpx
    url = proxy._upstream_url("TTS_URL", master_cfg.TTS_BACKEND_URL)
    if not url:
        return None
    tok = proxy._upstream_token("TTS_TOKEN", proxy.ENV_TTS_TOKEN)
    model = cfg.get("TTS_MODEL") or ""
    voice = cfg.get("TTS_VOICE") or ""
    # Payload wie beim Satelliten (app/tts.py): ohne language waehlt das
    # Qwen3-TTS-Backend ggf. eine andere Stimme als die gewohnte.
    payload = {"model": model, "voice": voice, "input": text,
               "response_format": "wav"}
    lang = str(cfg.get("TTS_LANGUAGE") or "").strip()
    if lang:
        payload["language"] = lang
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.post(url, json=payload, headers=headers)
        if r.status_code != 200 or not r.content:
            log.warning("Chat-TTS: HTTP %d", r.status_code)
            return None
        return r.content


async def _fanout_broadcast(cfg: dict, text: str, src: str = "") -> None:
    """Broadcast "an alle": 1x TTS, fertiges WAV parallel an alle Online-
    Satelliten.

    Neue Satelliten spielen audio_b64 direkt ab; alte Satelliten ignorieren
    das Feld und sprechen den Text selbst (text bleibt als Notbehelf im
    Payload — Abwaertskompatibilitaet ohne Versionspruefung). Android-Apps
    haben keinen Web-Dienst: wie bisher Text in die Pull-Warteschlange."""
    import base64
    try:
        wav = await _synth_tts(cfg, text)
    except Exception as e:
        log.warning("Broadcast-TTS: %s", e)
        wav = None
    if not wav:
        # Fallback: alte Route — jeder Satellit synthetisiert selbst
        await _sat_control("all", "/speak",
                           {"text": text, "wait": False, "src": src},
                           timeout=60)
        return
    payload = {"text": text,
               "audio_b64": base64.b64encode(wav).decode("ascii"),
               "wait": False}
    if src:
        # Quelle (schreibender Web-UI-Benutzer) — die Geräte zeigen sie
        # in ihrem Chat an.
        payload["src"] = src
    with manager._lock:
        targets = [
            # sat_kind (nicht s.kind): App-Recordes ohne kind-Feld im Heartbeat
            # (App < 2.4.1) bleiben sonst als 'pi' registriert und bekamen
            # statt des Audios einen POST auf einen Web-Dienst, den es nicht gibt.
            (k, f"http://{s.ip}:{SatManager._web_port(s)}",
             SatManager._kind_for(k, s))
            for k, s in manager.satellites.items()
            if s.status == "online" and s.ip and not s.disabled
        ]
    if not targets:
        return

    async def _one(t):
        k, base, kind = t
        if kind == "android":
            manager.queue_for(k, text, speak=True, src=src,
                              audio_b64=payload["audio_b64"])
            return
        try:
            async with _httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(base + "/speak", json=payload)
                if r.status_code != 200:
                    log.warning("Broadcast %s: HTTP %d", k, r.status_code)
        except Exception as e:
            log.warning("Broadcast %s: %s", k, e)

    await asyncio.gather(*(_one(t) for t in targets))


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
    """GET /api/config → Snapshot der zentralen Konfiguration (UI).

    Zeigt fuer die Master-Ziele (*_URL) den WIRKSAMEN Wert: fehlt ein
    Store-Eintrag, wird der ENV-Fallback (loop-guard-behandelt) eingesetzt,
    damit die UI anzeigt, was der Proxy tatsaechlich anpricht.
    """
    snap = config_store.snapshot()
    try:
        for k in URL_KEYS:
            if snap["global"].get(k):
                continue  # Store-Wert ist wirksam und wird roh angezeigt
            env_val = (os.environ.get(k.replace("_URL", "_BACKEND_URL"), "")
                       or os.environ.get(k, ""))
            eff = proxy._upstream_url(k, env_val)
            if eff:
                snap["global"][k] = eff
    except Exception:
        log.debug("Backend-Ziel-Anzeige fehlgeschlagen", exc_info=True)
    return JSONResponse(snap)


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
    g_allowed = (set(EDITABLE_KEYS) | set(GLOBAL_ONLY_KEYS)
                 | set(SECRET_KEYS) | set(BACKEND_KEYS) | set(URL_KEYS))
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


async def _config_export_split(req: Request, kind: str) -> Response:
    """GET /api/config/export-sat | export-master → getrennte Config-Datei.

    export-sat: Satelliten-Konfiguration (inkl. INTENTS/Intent-Regeln und
    aller Satelliten-Overrides) — ohne Master-eigene Backend-Ziele.
    export-master: nur Master-Konfiguration (Backend-Ziele, System, Secrets).
    Query secrets=0 wie beim Gesamt-Export."""
    include_secrets = req.query_params.get("secrets", "1") not in ("0", "false", "no")
    if kind == "satellite":
        payload = config_store.export_satellites(include_secrets=include_secrets)
        stem = "voice-satellite-satelliten-config"
    else:
        payload = config_store.export_master(include_secrets=include_secrets)
        stem = "voice-satellite-master-config"
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    fname = time.strftime(stem + "-%Y%m%d-%H%M%S.json")
    headers = {"Content-Disposition": f'attachment; filename="{fname}"'}
    return Response(body, media_type="application/json; charset=utf-8", headers=headers)


def _appqr_text(req: Request) -> str:
    """Aktuelle globale Master-Config als App-KEY=WERT-Text (für den QR-Code).

    Baut genau den Text, den die Android-App per Config-Import versteht
    (MASTER_URL + MASTER_TOKEN + Sprach-/Modellfelder). Geteilt von
    /api/config/appqr (Text) und /api/config/appqr.png (QR-Bild)."""
    import os
    try:
        g = config_store.get_effective(None) or {}
    except Exception as e:
        log.warning("appqr: %s", e)
        g = {}
    def val(k):
        v = g.get(k)
        return "" if v is None else str(v).strip()
    # MASTER_URL: Adresse, unter der der Browser das WebUI erreicht (LAN-IP:Port).
    # Das App-Gerät verbindet sich an dieselbe Adresse.
    host = (req.url.netloc if req.url else "") or ""
    scheme = (req.url.scheme if req.url else "http") or "http"
    master_url = (os.environ.get("MASTER_URL") or f"{scheme}://{host}").strip()
    token = str(config_store.get_secret("MASTER_TOKEN") or
                os.environ.get("MASTER_TOKEN", "") or "").strip()
    lines = ["# voice-satellite App-Konfiguration (aus Master-WebUI, als QR)",
             "MASTER_URL=" + master_url,
             "MASTER_TOKEN=" + token,
             "USE_MASTER=1",
             "STT_LANGUAGE=" + val("STT_LANGUAGE"),
             "LLM_MODEL=" + val("LLM_MODEL"),
             "TTS_LANGUAGE=" + val("TTS_LANGUAGE"),
             "DEFAULT_IMAGE_URL=" + val("DEFAULT_IMAGE_URL"),
             "INTENTS=" + val("INTENTS").replace("\n", "\\n"),
             "WAKE_WORD=" + val("WAKE_WORD")]
    return "\n".join(lines) + "\n"


async def _config_appqr_png(req: Request) -> Response:
    """GET /api/config/appqr.png -> QR-Code-PNG der App-Konfiguration.

    Enthält dasselbe wie /api/config/appqr (inkl. MASTER_TOKEN) -> nur im
    vertrauenswürdigen LAN betreiben. Ein App-Gerät scannt das Bild
    (Einstellungen -> QR-Code scannen) und importiert die Konfiguration."""
    try:
        import io
        import segno
    except Exception as e:
        log.warning("appqr.png: segno fehlt: %s", e)
        return JSONResponse({"error": "QR-Bibliothek (segno) nicht installiert"},
                            status_code=500)
    text = _appqr_text(req)
    try:
        buf = io.BytesIO()
        segno.make(text, error="M").save(buf, kind="png", scale=8, border=2)
    except Exception as e:
        log.warning("appqr.png: %s", e)
        return JSONResponse({"error": "QR-Erzeugung fehlgeschlagen"}, status_code=500)
    return Response(buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-cache"})


async def _config_appqr(req: Request) -> Response:
    """GET /api/config/appqr -> Konfig als App-KEY=WERT-Text (für den QR-Code).

    Baut die aktuelle globale Master-Config in dem KEY=WERT-Format, das die
    Android-App per Config-Import versteht. Ein neues App-Gerät scannt den QR
    (Config-Seite) und importiert die Werte - inkl. MASTER_URL + MASTER_TOKEN -
    und verbindet sich damit selbst am Master. TTS/STT/LLM-Modell + Stimme
    bleiben bewusst leer: der Master bestimmt sie wie bei den Pi-Satelliten
    selbst im Proxy-Flow. Enthält das Master-Token -> wie /api/config/export
    nur im vertrauenswürdigen LAN betreiben."""
    text = _appqr_text(req)
    return Response(text.encode("utf-8"), media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


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
# Erster Start: leeren User-Store aus ENV bevoelkern (WEB_ADMIN_USER/PASSWORD)
try:
    webauth.seed_admin_from_env()
except Exception:
    log.exception("Admin-Konto aus ENV nicht angelegt")


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
                (k, f"http://{s.ip}:{SatManager._web_port(s)}",
             SatManager._kind_for(k, s))
                for k, s in manager.satellites.items()
                if s.status == "online" and s.ip and not s.disabled
            ]
    else:
        hit = manager.sat_target(key)
        kind = manager.sat_kind(key) if hit else None
        targets = [(hit[0], hit[1], kind)] if hit else []
        if hit and manager.is_disabled(hit[0]):
            return JSONResponse({"ok": False, "error": "Satellit deaktiviert"},
                                status_code=403)
    if not targets:
        return JSONResponse(
            {"ok": False,
             "error": ("kein Satellit gefunden" if key not in ("", "all", "*")
                       else "keine bekannten, online Satelliten")},
            status_code=404)

    def _enqueue(k, endpoint, payload):
        """Steueranfrage an eine App (kein Web-Dienst): als Warteschlangen-
        Nachricht vormerken; die App holt sie mit ihrem Heartbeat ab."""
        if endpoint == "/speak":
            manager.queue_for(k, str((payload or {}).get("text") or ""),
                              speak=True,
                              src=str((payload or {}).get("src") or ""),
                              audio_b64=str((payload or {}).get("audio_b64") or ""))
            return {"ok": True, "queued": True,
                    "detail": "App ohne Web-Dienst — gewartet bis Heartbeat"}
        return {"ok": False,
                "error": f"{endpoint} von der App nicht unterstuetzt"}

    import json as _json
    async def _call(t):
        k, base, kind = t
        if kind == "android":
            res = _enqueue(k, endpoint, payload)
            return k, (200 if res.get("ok") else 400), res
        url = base + endpoint
        try:
            async with _httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(url, json=(payload or {}))
                try:
                    body = r.json()
                except Exception:
                    body = {"raw": r.text[:400]}
                return k, r.status_code, body
        except Exception as e:
            return k, 502, {"error": str(e)}

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
    user = _req_user(req)
    if user.get("display") and user.get("username") != "master-token":
        payload["src"] = user["display"]
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


async def image_push_core(sat_key: str, url: str, ttl: int = 60,
                          text: str = "", src: str = "") -> tuple[int, dict]:
    """Kern des Bild-Pushes — wird auch vom MCP-Tool 'push_image' genutzt.

    Apps (Android) bekommen Bild-URL + ttl in die Pull-Warteschlange; die
    App zeigt das Bild so lange an (Default 60 s). Pi-Satelliten haben
    (noch) keine Bildanzeige — sie werden pro Ziel gemeldet.
    text (optional): Begleittext, wird zusätzlich gesprochen/Durchsage."""
    url = str(url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return 400, {"ok": False, "error": "url (http/https) fehlt"}
    try:
        ttl = int(ttl or 0)
    except (TypeError, ValueError):
        ttl = 0
    ttl = min(max(ttl, 5), 600) if ttl else 60
    text = str(text or "").strip()[:500]
    sat_key = (sat_key or "").strip().lower()

    if sat_key in ("", "all", "*"):
        with manager._lock:
            targets = [(k, sa) for k, sa in manager.satellites.items()
                       if sa.status == "online" and sa.ip and not sa.disabled]
        if not targets:
            return 404, {"ok": False, "error": "keine bekannten, online Satelliten"}
    else:
        hit = manager.sat_target(sat_key)
        if not hit:
            return 404, {"ok": False, "error": "kein Satellit gefunden",
                         "sat": sat_key}
        key = hit[0]
        if manager.is_disabled(key):
            return 403, {"ok": False, "error": "Satellit deaktiviert"}
        targets = [(key, manager.satellites[key])]

    results = []
    for k, sa in targets:
        kind = SatManager._kind_for(k, sa)
        if kind == "android":
            manager.queue_for(k, text, speak=bool(text), src=src,
                              image=url, ttl=ttl)
            results.append({"sat": k, "ok": True,
                            "detail": "vorgemerkt, abholbar beim naechsten Heartbeat"})
        else:
            results.append({"sat": k, "ok": False,
                            "detail": "Bild-Anzeige auf Pi-Satelliten noch nicht verfuegbar"})
    ok = any(r["ok"] for r in results)
    return (200 if ok else 501), {"ok": ok, "targets": results}


async def _image_push(req: Request) -> JSONResponse:
    """POST /api/image {sat?, url, ttl?, text?, src?} — Bild an Gerät(e).

    Apps zeigen das Bild für ttl Sekunden (Default 60, Max 600) an; der
    optionale Begleittext wird zusätzlich gesprochen. Deaktivierte Geräte
    werden übersprungen; Pi-Satelliten werden pro Ziel gemeldet (noch ohne
    Bildanzeige)."""
    body = await _ctl_body(req)
    user = _req_user(req)
    src = str(body.get("src") or "").strip()[:48]
    if not src and user.get("display") and user.get("username") != "master-token":
        src = user["display"]
    code, data = await image_push_core(
        str(body.get("sat") or ""), str(body.get("url") or ""),
        body.get("ttl") or 0, str(body.get("text") or ""), src)
    return JSONResponse(data, status_code=code)



# ── Zentrales Gedaechtnis (Master-Modus: Web-Chat + alle Satelliten) ──
# Geraete-Meldungen (Maschinenrouten, Bearer MASTER_TOKEN) schreiben in
# dieselbe sqlite wie der Web-Chat; die WebUI verwaltet alles unter
# Konfiguration -> Gedaechtnis (Admin). Keys = kanonische Registry-Keys.

def _memory_dev_key(req: Request) -> str:
    """Registry-Key eines Geraets aus mac-Query + Quell-IP (wie Heartbeat)."""
    mac = (req.query_params.get("mac") or "").strip().lower()
    ip = (req.client.host if req.client else "") or ""
    return (manager.canonical_key(mac, ip) or mac)


def _memory_sat_denied(req: Request) -> JSONResponse | None:
    if _sat_auth_ok(req, "memory"):
        return None
    return JSONResponse({"error": "unauthorized",
                         "detail": "MASTER_TOKEN fehlt oder falsch"},
                        status_code=401)


async def _memory_sat_facts(req: Request) -> JSONResponse:
    """GET /api/memory/sat/facts?mac= — Fakten dieses Geraets (Satelliten)."""
    denied = _memory_sat_denied(req)
    if denied:
        return denied
    key = _memory_dev_key(req)
    if not key:
        return JSONResponse({"error": "mac fehlt"}, status_code=400)
    return JSONResponse({"facts": llmmemory.load_facts(key)})


async def _memory_sat_history(req: Request) -> JSONResponse:
    """GET /api/memory/sat/history?mac=&channel= — Kurzzeitgedaechtnis."""
    denied = _memory_sat_denied(req)
    if denied:
        return denied
    key = _memory_dev_key(req)
    if not key:
        return JSONResponse({"error": "mac fehlt"}, status_code=400)
    ch = (req.query_params.get("channel") or "").strip().lower()
    msgs = [{"role": m["role"], "content": m["content"]}
            for m in llmmemory.exchange_history(key, ch)]
    return JSONResponse({"messages": msgs})


async def _memory_sat_exchange(req: Request) -> JSONResponse:
    """POST /api/memory/sat/exchange {mac, channel, user, assistant}.

    Der Satellit meldt seinen Gedaechnis-Austausch — im Master-Modus
    liegt das Gedaechtnis ausschliesslich zentral hier."""
    denied = _memory_sat_denied(req)
    if denied:
        return denied
    try:
        body = await req.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "ungueltiges JSON"}, status_code=400)
    key = _memory_dev_key(req) or str(body.get("mac") or "").strip().lower()
    if not key:
        return JSONResponse({"error": "mac fehlt"}, status_code=400)
    channel = str(body.get("channel") or "voice").strip().lower()
    user_msg = str(body.get("user") or "")[:2000]
    assistant_msg = str(body.get("assistant") or "")[:4000]
    if not user_msg or not assistant_msg:
        return JSONResponse({"error": "user und assistant erforderlich"},
                            status_code=400)
    llmmemory.add_exchange(key, channel, user_msg, assistant_msg)
    return JSONResponse({"ok": True})


async def _memory_sat_extract(req: Request) -> JSONResponse:
    """POST /api/memory/sat/extract {mac, user, assistant, model} —
    die Fakten-Extraktion laeuft zentral am Master (entlastet den Pi)."""
    denied = _memory_sat_denied(req)
    if denied:
        return denied
    try:
        body = await req.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "ungueltiges JSON"}, status_code=400)
    key = _memory_dev_key(req) or str(body.get("mac") or "").strip().lower()
    if not key:
        return JSONResponse({"error": "mac fehlt"}, status_code=400)
    user_msg = str(body.get("user") or "")
    assistant_msg = str(body.get("assistant") or "")
    if user_msg and assistant_msg:
        llmmemory.extract_facts_async(user_msg, assistant_msg,
                                      str(body.get("model") or ""), key=key)
    return JSONResponse({"ok": True})


async def _memory_snapshot(req: Request) -> JSONResponse:
    """GET /api/memory — Gedaechtnis aller Geraete fuer die WebUI
    (Konfiguration -> Gedaechtnis, nur Admin)."""
    keys = {"web"} | set(llmmemory.fact_keys()) | set(llmmemory.history_keys())
    with manager._lock:
        keys |= set(manager.satellites.keys())
    devices = []
    for k in sorted(keys):
        if k == "web":
            hist = [{"role": m["role"], "content": m["content"],
                     "channel": "web"}
                    for m in llmmemory.history("web", limit=12)]
        else:
            hist = llmmemory.exchange_history(k, limit=12)
        devices.append({"key": k,
                        "name": manager.display_name(k) or k,
                        "facts": llmmemory.load_facts(k),
                        "history": hist})
    return JSONResponse({"devices": devices})


async def _memory_facts_put(req: Request) -> JSONResponse:
    """PUT /api/memory/facts {key, facts:[...]} — Faktenliste ersetzen."""
    try:
        body = await req.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "ungueltiges JSON"}, status_code=400)
    key = str(body.get("key") or "").strip().lower()
    facts = body.get("facts")
    if not key or not isinstance(facts, list):
        return JSONResponse({"error": "key und facts[] erforderlich"},
                            status_code=400)
    llmmemory.replace_facts(key, [str(f) for f in facts[:200]])
    return JSONResponse({"ok": True})


async def _memory_history_clear(req: Request) -> JSONResponse:
    """POST /api/memory/history/clear {key, channel?} — Verlauf loeschen
    (Fakten bleiben erhalten)."""
    try:
        body = await req.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "ungueltiges JSON"}, status_code=400)
    key = str(body.get("key") or "").strip().lower()
    if not key:
        return JSONResponse({"error": "key erforderlich"}, status_code=400)
    channel = str(body.get("channel") or "").strip().lower()
    llmmemory.clear_history(key, channel)
    return JSONResponse({"ok": True})


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
    # Benutzerverwaltung (Login/Session/Rollen + Verwaltung)
    Route("/login", _login, methods=["POST"]),
    Route("/logout", _logout, methods=["POST"]),
    Route("/api/me", _me),
    Route("/api/auth/status", _auth_status),
    Route("/api/setup", _setup, methods=["POST"]),
    Route("/api/users", _users_list),
    Route("/api/users", _users_create, methods=["POST"]),
    Route("/api/users/{name}", _users_update, methods=["PUT"]),
    Route("/api/users/{name}", _users_delete, methods=["DELETE"]),
    Route("/api/satellite/{mac}/name", _sat_name_put, methods=["PUT"]),
    Route("/api/satellite/{mac}/disable", _sat_disable, methods=["POST"]),
    Route("/api/satellite/{mac}/enable", _sat_enable, methods=["POST"]),
    Route("/api/image", _image_push, methods=["POST"]),
    Route("/api/app/version", _app_version),
    Route("/api/app/download", _app_download),
    Route("/api/satellite/{mac}/chat", _chat_push, methods=["POST"]),
    Route("/api/satellite/{mac}/dm", _sat_dm, methods=["POST"]),
    Route("/api/chat/messages", _chat_messages),
    Route("/api/chat/recs", _chat_recs),
    Route("/api/chat/rec/{key}", _chat_rec),
    Route("/api/chat/recq", _chat_recq),
    Route("/api/chat/send", _chat_send, methods=["POST"]),
    Route("/api/satellites", _satellite_list),
    Route("/api/satellites/status", _satellites_status),
    # (b) Zentrale Konfigurationsverwaltung
    Route("/api/config", _config_get),
    Route("/api/config", _config_put, methods=["PUT"]),
    Route("/api/config/apply", _config_apply_get, methods=["GET"]),
    Route("/api/config/export", _config_export),
    Route("/api/config/export-sat",
          lambda req: _config_export_split(req, "satellite")),
    Route("/api/config/export-master",
          lambda req: _config_export_split(req, "master")),
    Route("/api/config/appqr", _config_appqr),
    Route("/api/config/appqr.png", _config_appqr_png),
    Route("/api/config/import", _config_import, methods=["POST"]),
    Route("/api/config/satellite/{mac}", _config_sat_get),
    Route("/api/memory", _memory_snapshot),
    Route("/api/memory/facts", _memory_facts_put, methods=["PUT"]),
    Route("/api/memory/history/clear", _memory_history_clear,
          methods=["POST"]),
    Route("/api/memory/sat/facts", _memory_sat_facts),
    Route("/api/memory/sat/history", _memory_sat_history),
    Route("/api/memory/sat/exchange", _memory_sat_exchange, methods=["POST"]),
    Route("/api/memory/sat/extract", _memory_sat_extract, methods=["POST"]),
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


# ── WebUI-Zugriff: Benutzerverwaltung (Login/Session/Rollen) ───
# Das Gate deckt die menschlichen WebUI-Routen ab. Ausgenommen bleiben
# die Maschinen-Routen: Satelliten/Apps (eigene Bearer-Pruefung am
# Endpunkt), Proxy (Backend-Zugang), MCP (wie bisher), Musik-Index.
# Automatisierung (openHAB-curl) kommt an den Steuer-Endpunkten mit
# Bearer MASTER_TOKEN weiter durch.

_PUBLIC_PATHS = {
    "/", "/chat.html", "/healthz", "/login", "/logout",
    "/api/me", "/api/auth/status", "/api/setup",
}


def _gate_public(path: str) -> bool:
    """Routen ohne WebUI-Login (statisch, Maschinen, Ersteinrichtung)."""
    if path in _PUBLIC_PATHS:
        return True
    if path in ("/proxy", "/mcp") or \
            path.startswith(("/proxy/", "/mcp")):
        return True
    if path in ("/api/music", "/api/music/search"):
        return True
    if path.startswith("/api/satellite/") and \
            path.endswith(("/heartbeat", "/chat", "/dm")):
        return True
    if path == "/api/config/apply":
        return True
    if path.startswith("/api/memory/sat/"):  # Geraete-Meldungen (Token im Handler)
        return True
    return False


def _token_allowed(path: str) -> bool:
    """Steuer-/Lese-Endpunkte, fuer die Bearer MASTER_TOKEN reicht.

    OpenHAB-curl (Steuer-Endpunkte) UND die Android-App (Satellitenliste
    fuer die Direktnachrichten-Ziele, read-only)."""
    if path == "/api/broadcast":
        return True
    if path == "/api/image":
        return True
    if path in ("/api/app/version", "/api/app/download"):
        return True
    if path in ("/api/satellites", "/api/satellites/status"):
        return True
    if path.startswith("/api/satellite/"):
        return path.rsplit("/", 1)[-1] in ("volume", "output",
                                           "disable", "enable")
    if path.startswith("/api/memory"):
        return True
    return False


def _needs_admin(path: str, method: str) -> bool:
    """Nur Admin-Session (oder Token): Schreibaktionen der WebUI."""
    if path.startswith("/api/users"):
        return True
    if path in ("/api/config/export", "/api/config/import",
                "/api/config/export-sat", "/api/config/export-master",
                "/index/rebuild"):
        return True
    if path.startswith("/api/config/appqr"):      # enthaelt MASTER_TOKEN
        return True
    if path == "/api/config" and method in ("PUT", "POST"):
        return True
    if path.startswith("/api/satellite/"):
        tail = path.rsplit("/", 1)[-1]
        return tail in ("restart", "pause", "unpause", "name",
                        "volume", "output", "disable", "enable")
    if path.startswith("/api/memory") and not path.startswith("/api/memory/sat/"):
        return True
    return False


class _WebGateMiddleware:
    """Reines ASGI-Middleware (versionstabil, kein BaseHTTPMiddleware).

    Loest den WebUI-Benutzer aus Session-Cookie bzw. Bearer MASTER_TOKEN
    und legt ihn als scope['webauth_user'] ab (Handler: _req_user())."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        from starlette.requests import Request
        req = Request(scope, receive, send)
        # Session immer auflösen (auch auf publicen Routen wie /api/me),
        # damit sich die UI über den eigenen Status informieren kann.
        user = None
        tok = req.cookies.get("vs_sid", "")
        if tok:
            user = webauth.session_user(tok)
        if not _gate_public(path):
            if user is None and _token_allowed(path):
                user = _token_user(req)
            if user is None:
                resp = JSONResponse({"error": "unauthorized",
                                     "detail": "Login erforderlich"},
                                    status_code=401)
                await resp(scope, receive, send)
                return
            if _needs_admin(path, method) and user["role"] != "admin":
                resp = JSONResponse({"error": "forbidden",
                                     "detail": "Admin-Berechtigung erforderlich"},
                                    status_code=403)
                await resp(scope, receive, send)
                return
        if user is not None:
            scope["webauth_user"] = user
        await self.app(scope, receive, send)


def _token_user(req: Request) -> dict | None:
    """Bearer MASTER_TOKEN → Automatisierungs-Zugang (Rolle admin)."""
    tok = _sat_token()
    if not tok:
        return None
    got = req.headers.get("authorization", "")
    if got.startswith("Bearer "):
        got = got[7:]
    import hmac as _hmac
    if got and _hmac.compare_digest(got, tok):
        return {"username": "master-token", "role": "admin",
                "display": "Master-Token"}
    return None


def _req_user(req: Request) -> dict:
    """Eingeloggter WebUI-Benutzer eines Requests (leeres Dict, wenn keiner)."""
    return req.scope.get("webauth_user") or {}


app.add_middleware(_WebGateMiddleware)


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
