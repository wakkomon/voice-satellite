"""Web-Interface (stdlib http.server, kein FastAPI/armv7-Risiko).

GET  /              -> HTML-Echtzeitansicht (app/ui.html)
GET  /health        -> {"ok": true}
GET  /status        -> Zustand, Statistik, letzte Events
GET  /events        -> SSE-Stream: alle Pipeline-Events in Echtzeit
POST /speak         -> {"text": "..."}  (antwortet direkt per TTS)
POST /intent        -> {"text": "..."}  (Router testen, ohne Mikrofon)
"""
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import audio, config, router, tts

log = logging.getLogger("web")
_pipeline = None
_UI_PATH = os.path.join(os.path.dirname(__file__), "ui.html")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, {"ok": True})
        elif path == "/":
            self._send_html()
        elif path == "/events":
            self._sse()
        elif path == "/status":
            p = _pipeline
            if not p:
                return self._send(200, {"state": "booting"})
            self._send(200, {
                "state": p.state,
                "stats": p.stats,
                "uptime_s": round(time.time() - p.stats["started"]),
                "events": p.events[-20:],
            })
        else:
            self._send(404, {"error": "unknown path"})

    def do_POST(self):
        data = self._read_json()
        text = (data.get("text") or "").strip()
        if self.path == "/speak":
            if not text:
                return self._send(400, {"error": "text fehlt"})
            self._ev(f"Web: \"{text[:60]}\" (TTS direkt)")
            wav = tts.synthesize(text)
            if not wav:
                return self._send(502, {"error": "TTS leer"})
            threading.Thread(target=self._play, args=(wav,), daemon=True).start()
            return self._send(200, {"ok": True, "bytes": len(wav)})
        if self.path == "/intent":
            if not text:
                return self._send(400, {"error": "text fehlt"})
            t0 = time.time()
            answer, kind, _ok = router.handle(text)
            dt = time.time() - t0
            self._ev(f"Web-Intent {dt:.1f}s ({kind}): {answer[:80]}")
            return self._send(200, {"answer": answer, "route": kind})
        self._send(404, {"error": "unknown path"})

    def _ev(self, msg):
        """Aktivitaeten aus dem Webthread in den Live-Feed melden."""
        p = _pipeline
        if p:
            p.ev(msg)

    # ── UI & SSE ──────────────────────────────────────────────
    def _send_html(self):
        try:
            body = open(_UI_PATH, "rb").read()
        except OSError as e:
            self._send(500, {"error": f"ui.html fehlt: {e}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        """Server-Sent Events: komplettes Snapshot alle 2 s (Zustand + Statistik
        + Events-Ring) – Client dedupliziert per seq. Reconnect: neu laden,
        der Ring bringt die letzten 60 Events von selbst."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                p = _pipeline
                if p:
                    snap = {
                        "state": p.state,
                        "stats": p.stats,
                        "uptime_s": round(time.time() - p.stats["started"]),
                        "events": p.events[-20:],
                    }
                    self.wfile.write(f"id: {p.seq}\n".encode("utf-8"))
                    self.wfile.write(("data: "
                                       + json.dumps(snap, ensure_ascii=False)
                                       + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    @staticmethod
    def _play(wav):
        try:
            audio.play_wav(wav)
        except Exception as e:
            log.error("Speak-Fehler: %s", e)

    def log_message(self, fmt, *args):
        log.debug("http: " + fmt, *args)


def start(pipeline):
    global _pipeline
    _pipeline = pipeline

    def serve():
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", config.WEB_PORT), Handler)
            log.info("Web-Interface auf :%d", config.WEB_PORT)
            srv.serve_forever()
        except Exception as e:
            log.error("Web-Interface nicht gestartet: %s", e)

    threading.Thread(target=serve, daemon=True).start()
