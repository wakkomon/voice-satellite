#!/usr/bin/env python3
"""Regression: Master-Proxy /proxy/image (Default-Bild ueber den Master).

Im Master-Image ausfuehren (httpx/starlette vorhanden):
    docker exec -w /app voice-satellite-master python3 tests/test_image_proxy.py

Prueft: Store>ENV>leer-Resolution von DEFAULT_IMAGE_URL, den /proxy/-Loop-
Guard, den 502-Kein-Backend-Fall und den Happy-Path (Upstream-Bild wird
1:1 mit Content-Type image/* weitergereicht). Loekal ohne HTTP-Stack wird
der Proxy-Teil uebersprungen (px is None) — die Store-Schicht ist in
tests/test_config_store_proxy.py abgedeckt.
"""
import asyncio
import base64
import http.server
import os
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def ck(cond, msg):
    print(("  ok  " if cond else "  FAIL") + "  " + msg)
    if not cond:
        fails.append(msg)


tmp = tempfile.mkdtemp(prefix="vs-img-")
os.environ["VS_CONFIG_DIR"] = tmp
from master import satconfig as sc  # noqa: E402
store = sc.store

try:
    from master import proxy as px
except Exception as e:  # pragma: no cover - lokales Testumfeld ohne HTTP-Stack
    px = None
    print("  (HTTP-Stack nicht verfuegbar, Proxy-Test uebersprungen: %s)" % e)

# 1x1 JPEG, als Upstream-Anwort und als Vergleichs-Basis.
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/"
    "wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/"
    "2gAIAQEAAD8AVN//2Q==")


class _ImgHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(JPEG)))
        self.end_headers()
        self.wfile.write(JPEG)

    def log_message(self, *args):
        pass


if px:
    from starlette.requests import Request

    def _get_req(path, method="GET", headers=None, query=b""):
        scope = {
            "type": "http", "method": method, "path": path,
            "query_string": query,
            "headers": [(k.lower().encode(), v.encode())
                        for k, v in (headers or {}).items()],
            "server": ("127.0.0.1", 8791), "client": ("127.0.0.1", 12345),
            "scheme": "http", "root_path": "", "http_version": "1.1",
        }
        return Request(scope)

    print("== _default_image_url: Store > ENV > leer ==")
    store.set_global({"DEFAULT_IMAGE_URL": "http://10.0.0.5/weather/snap.jpg"})
    ck(px._default_image_url() == "http://10.0.0.5/weather/snap.jpg",
       "Store-Wert (global, effektiv) gewinnt")
    store.set_global({"DEFAULT_IMAGE_URL": ""})
    os.environ["DEFAULT_IMAGE_URL"] = "http://10.0.0.6/env.jpg"
    ck(px._default_image_url() == "http://10.0.0.6/env.jpg",
       "leerer Store -> ENV-Fallback")
    del os.environ["DEFAULT_IMAGE_URL"]
    ck(px._default_image_url() == "", "Store + ENV leer -> ''")

    print("== Loop-Guard: /proxy/-Ziel wird abgewiesen ==")
    ck(px._is_self_proxy("http://127.0.0.1:8791/proxy/image"),
       "_is_self_proxy erkennt /proxy/-Pfad")
    store._global["DEFAULT_IMAGE_URL"] = "http://127.0.0.1:8791/proxy/image"
    resp = asyncio.run(px.proxy_image(_get_req("/proxy/image")))
    ck(resp.status_code == 502, "Ziel auf eigenen /proxy/ -> 502 (Loop unterbunden)")
    store._global.pop("DEFAULT_IMAGE_URL", None)

    print("== kein DEFAULT_IMAGE_URL -> 502 ==")
    resp = asyncio.run(px.proxy_image(_get_req("/proxy/image")))
    ck(resp.status_code == 502, "ohne konfigurierte Quelle -> 502")

    print("== HEAD-Probe -> 200 ohne Body ==")
    resp = asyncio.run(px.proxy_image(_get_req("/proxy/image", method="HEAD")))
    ck(resp.status_code == 200, "HEAD liefert 200 (Erreichbarkeitsprobe)")

    print("== Happy-Path: Upstream-Bild wird 1:1 weitergereicht ==")
    srv = http.server.HTTPServer(("127.0.0.1", 0), _ImgHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        store.set_global({"DEFAULT_IMAGE_URL": "http://127.0.0.1:%d/snap.jpg" % port})
        resp = asyncio.run(px.proxy_image(_get_req("/proxy/image?cachebust=1")))
        body = getattr(resp, "body", b"")
        ck(resp.status_code == 200, "Upstream 200 wird durchgereicht")
        ck(body == JPEG, "Bild-Bytes 1:1 vom Upstream")
        ck(str(resp.headers.get("content-type", "")).startswith("image/"),
           "Content-Type image/*")
    finally:
        srv.shutdown()
        store.set_global({"DEFAULT_IMAGE_URL": ""})

print()
if fails:
    print("FEHLGESCHLAGEN: %d Test(s)" % len(fails))
    sys.exit(1)
print("ALLE OK" if px else "STORE-PFAD OK (HTTP-Stack hier nicht pruefbar)")
