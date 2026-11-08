"""Zentrales MCP-Gateway am Master

Der Master bündelt die MCP-Tools aller Satelliten (Display, Töne, Wecker —
Durchreichung an die Satelliten) UND stellt eigene Master-Tools bereit
(Broadcast, Chat, Direktnachrichten, Bild-Push, Lautstärke, De-/Aktivierung,
Container-Aktionen). Ein einzelner MCP-Client (z.B. Hermes) kann so das
gesamte System steuern, ohne pro Satellite die IP zu kennen.

Protokoll (identisch zu app/mcp.py – MCP-over-JSON-RPC):
    GET  /mcp/tools              → Master-Tools + Tools aller Satelliten
                                   (per-Mac-namespaced, master-Tools: mac="master")
    GET  /mcp/tools/{mac}        → Tools eines Satelliten (durchgereicht)
    POST /mcp                    → JSON-RPC; Master-Tool NAMED = lokale
                                   Ausführung, sonst → Satellite
    POST /mcp/{mac}              → JSON-RPC → konkreter Satellite

Der Client ruft denselben JSON-RPC-Standard wie am Satellite auf; nur das
Feld "target" (bzw. die Pfad-Komponente) bestimmt, welcher Satellite
bedient wird. Ohne "target" wird der (einzige) bekannte Satellite genutzt.
"""
from __future__ import annotations

import json
import logging
import os

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import proxy as proxy_mod  # für _authorized (optionale Proxy-Auth)

log = logging.getLogger("master.mcp")

MCP_TIMEOUT = 15.0  # Sekunden (Display-Steuerung darf nicht ewig hängen)

# ── Master-Tools (eigene Funktionen des Masters, lokal ausgeführt) ────
#
# Diese Tools ergänzen die durchgereichten Satellite-Tools (play_tone,
# show_text, show_weather, set_alarm, …) um die Master-Funktionen, die in
# den letzten Versionen dazu gekommen sind: Haus-Durchsage, Chat mit
# Zielgerät, Geräte-zu-Gerät-Direktnachrichten, Bild-Push, Lautstärke/-
# Ausgabe, Container-Aktionen und Satelliten an-/abschalten.

MASTER_TOOLS: list[dict] = [
    {
        "name": "list_satellites",
        "description": "Alle bekannten Satelliten auflisten (Status, "
                       "IP/Hostname, Aktivität, App-Version, deaktiviert).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "broadcast",
        "description": "Text ohne Wake-Word/LLM durchsagen (TTS). sat leer "
                       "= ALLE online Geräte (Hausdurchsage), sonst "
                       "MAC/IP/Hostnamen/custom-Name. wait=true lässt auf "
                       "die fertige Wiedergabe warten (Default false).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Zu sprechender Text"},
                "sat": {"type": "string", "description": "Zielgerät (leer = alle)"},
                "wait": {"type": "boolean", "default": False},
                "vol": {"type": "integer", "description": "Lautstärke 0-100 (optional)"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "chat_send",
        "description": "Master-Chat: LLM-Antwort (mit Intent-Bridge für "
                       "openHAB-/Musik-/Lautstärke-Befehle), optional als "
                       "TTS am Zielgerät. out: browser (Default) | "
                       "Satellit (MAC/IP/Name) | 'all' (alle Geräte).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "out": {"type": "string", "description": "browser | Satellit | all"},
                "speak": {"type": "boolean", "default": True},
                "mac": {"type": "string", "description": "Konfig-Quelle (optional)"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "send_dm",
        "description": "Geräte-zu-Gerät-Direktnachricht (reiner Text, ohne "
                       "LLM/TTS) in die Pull-Warteschlange des Ziels. "
                       "to: MAC/IP/Name oder 'all' (alle anderen Geräte).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["to", "text"],
        },
    },
    {
        "name": "push_image",
        "description": "Bild an die Geräte pushen: die App zeigt die Bild-URL "
                       "für ttl Sekunden (Default 60) an. sat leer = alle "
                       "online App-Geräte. text (optional) wird zusätzlich "
                       "gesprochen. Pi-Satelliten melden 'noch nicht verfügbar'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http(s)-Bild-URL"},
                "sat": {"type": "string", "description": "Ziel (leer = alle Apps)"},
                "ttl": {"type": "integer", "default": 60,
                        "description": "Anzeigedauer in Sekunden (5-600)"},
                "text": {"type": "string", "description": "Begleittext (optional)"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "set_volume",
        "description": "Lautstärke eines Geräts setzen (0-100) und/oder "
                       "stummschalten. sat: MAC/IP/Hostnamen.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sat": {"type": "string"},
                "volume": {"type": "integer"},
                "muted": {"type": "boolean"},
            },
            "required": ["sat"],
        },
    },
    {
        "name": "get_volume",
        "description": "Lautstärke/Zustand eines Geräts abfragen.",
        "inputSchema": {
            "type": "object",
            "properties": {"sat": {"type": "string"}},
            "required": ["sat"],
        },
    },
    {
        "name": "set_output",
        "description": "Audio-Ausgabegerät eines Satelliten umschalten "
                       "(z.B. plughw:1,0).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sat": {"type": "string"},
                "out": {"type": "string", "description": "ALSA-Gerätename"},
            },
            "required": ["sat", "out"],
        },
    },
    {
        "name": "get_output",
        "description": "Verfügbare + aktuelle Audio-Ausgabe eines Satelliten.",
        "inputSchema": {
            "type": "object",
            "properties": {"sat": {"type": "string"}},
            "required": ["sat"],
        },
    },
    {
        "name": "disable_satellite",
        "description": "Satelliten (Pi oder App) deaktivieren: empfängt keine "
                       "Pushes mehr, Heartbeat/Chat/DM werden mit 403 "
                       "abgewiesen. Persistiert.",
        "inputSchema": {
            "type": "object",
            "properties": {"sat": {"type": "string"}},
            "required": ["sat"],
        },
    },
    {
        "name": "enable_satellite",
        "description": "Deaktivierten Satelliten wieder aktivieren.",
        "inputSchema": {
            "type": "object",
            "properties": {"sat": {"type": "string"}},
            "required": ["sat"],
        },
    },
    {
        "name": "set_satellite_name",
        "description": "Anzeigename eines Satelliten setzen (leer = löschen).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sat": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["sat"],
        },
    },
    {
        "name": "satellite_restart",
        "description": "Voice-Container des Satelliten neu starten (SSH+Docker).",
        "inputSchema": {
            "type": "object",
            "properties": {"sat": {"type": "string"}},
            "required": ["sat"],
        },
    },
    {
        "name": "satellite_pause",
        "description": "Voice-Container des Satelliten pausieren (docker pause).",
        "inputSchema": {
            "type": "object",
            "properties": {"sat": {"type": "string"}},
            "required": ["sat"],
        },
    },
    {
        "name": "satellite_unpause",
        "description": "Gepauschten Voice-Container fortsetzen (docker unpause).",
        "inputSchema": {
            "type": "object",
            "properties": {"sat": {"type": "string"}},
            "required": ["sat"],
        },
    },
]


def _manager():
    """Lazy Access auf den api-Manager (vermeidet zirkulären Import beim Load)."""
    from . import api
    return api.manager


def _sat_ip(mac: str, manager) -> str | None:
    """IP eines Satelliten aus dem Manager (Heartbeat/Discovery)."""
    sat = manager.satellites.get(mac)
    return sat.ip if sat and sat.ip else None


def _resolve_single(mac: str | None, manager) -> tuple[str | None, str | None]:
    """(mac, ip) auflösen; ohne mac → einziger bekannter Satellite."""
    if mac:
        mac = mac.lower()
        ip = _sat_ip(mac, manager)
        return mac, ip
    # Fallback: erster bekannter/online Satellite
    with manager._lock:
        for m, s in manager.satellites.items():
            if s.status == "online" and s.ip and not s.disabled:
                return m, s.ip
    return None, None


def _mcp_url(ip: str) -> str:
    # MCP läuft auf dem Pi auf MCP_PORT (Default 8780), NICHT im Container.
    port = os.environ.get("MCP_PORT", "8780")
    return f"http://{ip}:{port}"


def _auth_error(req: Request):
    # Wie der Proxy: Token (AUTH_TOKEN ODER MASTER_TOKEN) ist Pflicht, sobald
    # mindestens eines gesetzt ist. Damit ist das MCP-Gateway genauso
    # token-geschuetzt wie API/Proxy (vorher: nur AUTH_TOKEN, d.h. offen,
    # wenn nur MASTER_TOKEN gesetzt war).
    if proxy_mod._authorized(req):
        return None
    return JSONResponse({"error": "unauthorized"}, status_code=401)


def _resp_body(resp) -> dict:
    """JSONResponse → dict (für die JSON-RPC-Result-Weitergabe)."""
    try:
        return json.loads(resp.body)
    except Exception:
        return {"raw": resp.body.decode("utf-8", "replace")[:400]}


# ── Master-Tool-Implementierungen ─────────────────────────────────
# Jede Funktion: params (dict) → async dict (JSON-RPC result). Fehler als
# {"ok": False, "error": ...} (kein JSON-RPC-Fehler — es ist ein Result,
# das der Aufrufer auswertet; nur strukturelle Fehler sind JSON-RPC-Fehler).

async def _t_list_satellites(params: dict) -> dict:
    sats = await _manager().list_satellites()
    return {"ok": True, "satellites": sats}


async def _t_broadcast(params: dict) -> dict:
    from . import api
    text = str(params.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "text fehlt"}
    payload = {"text": text,
               "wait": bool(params.get("wait", False)), "src": "MCP"}
    if params.get("vol") is not None:
        try:
            payload["vol"] = int(params["vol"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "vol muss eine Zahl sein"}
    resp = await api._sat_control(str(params.get("sat") or ""), "/speak",
                                  payload, timeout=60.0)
    return _resp_body(resp)


async def _t_chat_send(params: dict) -> dict:
    from . import api
    text = str(params.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "text fehlt"}
    try:
        return await api._chat_send_core(
            text, str(params.get("mac") or ""),
            bool(params.get("speak", True)),
            str(params.get("out") or "browser"), who="MCP")
    except ValueError as e:
        return {"ok": False, "error": str(e)}


async def _t_send_dm(params: dict) -> dict:
    from . import api
    code, data = await api._dm_core("", "MCP",
                                    str(params.get("to") or ""),
                                    str(params.get("text") or ""))
    data["status_code"] = code
    return data


async def _t_push_image(params: dict) -> dict:
    from . import api
    try:
        ttl = int(params.get("ttl") or 0)
    except (TypeError, ValueError):
        ttl = 0
    code, data = await api.image_push_core(
        str(params.get("sat") or ""), str(params.get("url") or ""),
        ttl, str(params.get("text") or ""), src="MCP")
    data["status_code"] = code
    return data


async def _t_set_volume(params: dict) -> dict:
    from . import api
    sat = str(params.get("sat") or "")
    if not sat:
        return {"ok": False, "error": "sat fehlt"}
    payload = {}
    if params.get("volume") is not None:
        try:
            payload["volume"] = int(params["volume"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "volume muss eine Zahl sein"}
    if params.get("muted") is not None:
        payload["muted"] = bool(params["muted"])
    if not payload:
        return {"ok": False, "error": "volume oder muted fehlt"}
    return _resp_body(await api._sat_control(sat, "/volume", payload))


async def _t_get_volume(params: dict) -> dict:
    from . import api
    manager = _manager()
    sat = str(params.get("sat") or "")
    hit = manager.sat_target(sat)
    if not hit:
        return {"ok": False, "error": "Satellit nicht bekannt"}
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as c:
            r = await c.get(hit[1] + "/volume")
        return _resp_body_from_bytes(r.content, r.status_code)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _resp_body_from_bytes(content: bytes, status: int) -> dict:
    try:
        data = json.loads(content or b"{}")
    except Exception:
        data = {"raw": content.decode("utf-8", "replace")[:400]}
    data.setdefault("status_code", status)
    return data


async def _t_set_output(params: dict) -> dict:
    from . import api
    sat = str(params.get("sat") or "")
    out = str(params.get("out") or "").strip()
    if not sat or not out:
        return {"ok": False, "error": "sat und out erforderlich"}
    return _resp_body(await api._sat_control(sat, "/audio/out", {"out": out}))


async def _t_get_output(params: dict) -> dict:
    from . import api
    manager = _manager()
    sat = str(params.get("sat") or "")
    hit = manager.sat_target(sat)
    if not hit:
        return {"ok": False, "error": "Satellit nicht bekannt"}
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as c:
            r = await c.get(hit[1] + "/audio/out")
        return _resp_body_from_bytes(r.content, r.status_code)
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _t_set_disabled(params: dict, disabled: bool) -> dict:
    manager = _manager()
    sat = str(params.get("sat") or "")
    err = manager.set_disabled(sat, disabled)
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "sat": sat, "disabled": disabled}


async def _t_set_name(params: dict) -> dict:
    manager = _manager()
    sat = str(params.get("sat") or "")
    err = manager.set_name(sat, str(params.get("name") or ""))
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "name": manager.display_name(sat)}


async def _t_container_action(params: dict, action: str) -> dict:
    manager = _manager()
    sat = str(params.get("sat") or "")
    fn = getattr(manager, f"action_{action}_container", None)
    if fn is None:
        return {"ok": False, "error": f"unbekannte Aktion {action!r}"}
    return await fn(sat)


MASTER_TOOL_FUNCS: dict[str, object] = {
    "list_satellites": lambda p: _t_list_satellites(p),
    "broadcast": lambda p: _t_broadcast(p),
    "chat_send": lambda p: _t_chat_send(p),
    "send_dm": lambda p: _t_send_dm(p),
    "push_image": lambda p: _t_push_image(p),
    "set_volume": lambda p: _t_set_volume(p),
    "get_volume": lambda p: _t_get_volume(p),
    "set_output": lambda p: _t_set_output(p),
    "get_output": lambda p: _t_get_output(p),
    "disable_satellite": lambda p: _t_set_disabled(p, True),
    "enable_satellite": lambda p: _t_set_disabled(p, False),
    "set_satellite_name": lambda p: _t_set_name(p),
    "satellite_restart": lambda p: _t_container_action(p, "restart"),
    "satellite_pause": lambda p: _t_container_action(p, "pause"),
    "satellite_unpause": lambda p: _t_container_action(p, "unpause"),
}


async def mcp_tools_all(req: Request) -> JSONResponse:
    """Tools: Master (mac='master') + alle bekannten Satelliten."""
    if e := _auth_error(req):
        return e  # type: ignore[return-value]
    manager = _manager()
    tools: list[dict] = []
    for t in MASTER_TOOLS:
        tools.append({**t, "mac": "master", "hostname": "master"})
    with manager._lock:
        sats = [(s.mac, s.ip, s.hostname) for s in manager.satellites.values()
                if s.ip and not s.disabled]
    async with httpx.AsyncClient(timeout=httpx.Timeout(MCP_TIMEOUT)) as client:
        for mac, ip, hostname in sats:
            try:
                r = await client.get(f"{_mcp_url(ip)}/mcp/tools")
                schema = r.json().get("schema", {})
                for t in schema.get("tools", []):
                    tools.append({**t, "mac": mac, "hostname": hostname})
            except Exception as ex:
                log.warning("MCP-Tools von %s (%s) nicht erreichbar: %s",
                            hostname, ip, ex)
    return JSONResponse({"tools": tools, "count": len(tools)})


async def mcp_tools_one(req: Request) -> JSONResponse:
    """Tools eines konkreten Satelliten (durchgereicht)."""
    if e := _auth_error(req):
        return e  # type: ignore[return-value]
    manager = _manager()
    mac = req.path_params["mac"].lower()
    ip = _sat_ip(mac, manager)
    if not ip:
        return JSONResponse({"error": f"satellite '{mac}' not found"},
                            status_code=404)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(MCP_TIMEOUT)) as c:
            r = await c.get(f"{_mcp_url(ip)}/mcp/tools")
        return JSONResponse(r.json())
    except Exception as ex:
        return JSONResponse({"error": f"mcp unreachable: {ex}"}, status_code=502)


def _rpc_result(rpc_id, data) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "result": data, "id": rpc_id})


def _rpc_error(rpc_id, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0",
                         "error": {"code": code, "message": message},
                         "id": rpc_id})


async def mcp_call(req: Request) -> JSONResponse:
    """JSON-RPC an einen (oder den) Satellite oder an einen Master-Tool
    (method = Name aus MASTER_TOOLS) weiterleiten."""
    if e := _auth_error(req):
        return e  # type: ignore[return-value]
    manager = _manager()
    try:
        rpc = await req.json()
    except (ValueError, Exception):
        return _rpc_error(None, -32700, "Invalid JSON")
    if not isinstance(rpc, dict) or "method" not in rpc:
        return _rpc_error(None, -32600, "Invalid request")

    rpc_id = rpc.get("id")
    method = str(rpc.get("method") or "")
    params = rpc.get("params") if isinstance(rpc.get("params"), dict) else {}

    # Master-Tool? (lokal ausführen, kein Satellite-Target nötig)
    if method in MASTER_TOOL_FUNCS:
        try:
            result = await MASTER_TOOL_FUNCS[method](params)
        except Exception as ex:
            log.warning("MCP-Tool %s: %s: %s", method,
                        type(ex).__name__, ex)
            return _rpc_error(rpc_id, -32000, f"{type(ex).__name__}: {ex}")
        return _rpc_result(rpc_id, result)

    # Satellite-Passthrough (Display, Töne, Wecker …)
    mac = req.path_params.get("mac") or rpc.get("target") or rpc.get("mac")
    mac_l, ip = _resolve_single((mac or "").lower() if mac else None, manager)
    if not ip:
        return _rpc_error(rpc_id, -32002,
                          "kein erreichbarer Satellite (target fehlt?)")
    if mac_l and manager.is_disabled(mac_l):
        return _rpc_error(rpc_id, -32003,
                          f"Satellite '{mac_l}' ist deaktiviert")
    # "target"/"mac" sind Gateway-Felder – nicht an den Satellite durchreichen.
    forward = {k: v for k, v in rpc.items() if k not in ("target", "mac")}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(MCP_TIMEOUT)) as c:
            r = await c.post(f"{_mcp_url(ip)}/mcp", json=forward)
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as ex:
        return JSONResponse({
            "jsonrpc": "2.0",
            "error": {"code": -32001, "message": f"mcp unreachable: {ex}"},
            "id": rpc_id,
        }, status_code=502)
