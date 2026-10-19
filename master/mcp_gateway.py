"""Zentrales MCP-Gateway am Master (2.0.0alpha).

Der Master bündelt die MCP-Tools aller Satelliten und stellt sie als einen
zentralen MCP-Endpunkt bereit. Ein einzelner MCP-Client (z.B. Hermes) kann
so jedes PIDisplay im Haus ansteuern, ohne pro Satellite die IP zu kennen.

Protokoll (identisch zu app/mcp.py – MCP-over-JSON-RPC):
    GET  /mcp/tools              →  Tools aller Satelliten (per-Mac-namespaced)
    GET  /mcp/tools/{mac}        →  Tools eines Satelliten (durchgereicht)
    POST /mcp                    →  JSON-RPC → Satellite
                                    {jsonrpc, id, target:"<mac>", method, params}
    POST /mcp/{mac}              →  JSON-RPC → konkreter Satellite (target aus Pfad)

Der Client ruft denselben JSON-RPC-Standard wie am Satellite auf; nur das
Feld "target" (bzw. die Pfad-Komponente) bestimmt, welcher Satellite
bedient wird. Ohne "target" wird der (einzige) bekannte Satellite genutzt.
"""
from __future__ import annotations

import logging
import os

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import proxy as proxy_mod  # für _authorized (optionale Proxy-Auth)

log = logging.getLogger("master.mcp")

MCP_TIMEOUT = 15.0  # Sekunden (Display-Steuerung darf nicht ewig hängen)


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
            if s.status == "online" and s.ip:
                return m, s.ip
    return None, None


def _mcp_url(ip: str) -> str:
    # MCP läuft auf dem Pi auf MCP_PORT (Default 8780), NICHT im Container.
    port = os.environ.get("MCP_PORT", "8780")
    return f"http://{ip}:{port}"


def _auth_error(req: Request):
    if not proxy_mod.AUTH_TOKEN:
        return None
    if req.headers.get("authorization", "") == f"Bearer {proxy_mod.AUTH_TOKEN}":
        return None
    return JSONResponse({"error": "unauthorized"}, status_code=401)


async def mcp_tools_all(req: Request) -> JSONResponse:
    """Tools aller bekannten Satelliten, per-Mac-namespaced."""
    if e := _auth_error(req):
        return e  # type: ignore[return-value]
    manager = _manager()
    tools: list[dict] = []
    with manager._lock:
        sats = [(s.mac, s.ip, s.hostname) for s in manager.satellites.values()
                if s.ip]
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


async def mcp_call(req: Request) -> JSONResponse:
    """JSON-RPC an einen (oder den) Satellite weiterleiten."""
    if e := _auth_error(req):
        return e  # type: ignore[return-value]
    manager = _manager()
    try:
        rpc = await req.json()
    except (ValueError, Exception):
        return JSONResponse({"jsonrpc": "2.0",
                             "error": {"code": -32700, "message": "Invalid JSON"},
                             "id": None})
    if not isinstance(rpc, dict) or "method" not in rpc:
        return JSONResponse({"jsonrpc": "2.0",
                             "error": {"code": -32600, "message": "Invalid request"},
                             "id": None})

    mac = req.path_params.get("mac") or rpc.get("target") or rpc.get("mac")
    rpc_id = rpc.get("id")
    mac_l, ip = _resolve_single((mac or "").lower() if mac else None, manager)
    if not ip:
        return JSONResponse({
            "jsonrpc": "2.0",
            "error": {"code": -32002,
                      "message": "kein erreichbarer Satellite (target fehlt?)"},
            "id": rpc_id,
        })
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
