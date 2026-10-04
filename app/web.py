"""Winziges Debug-Interface (stdlib http.server, kein FastAPI/armv7-Risiko).

GET  /health          -> {"ok": true}
GET  /status          -> Zustand, Statistik, letzte Events
POST /speak           -> {"text": "..."}  (antwortet direkt per TTS)
POST /intent          -> {"text": "..."}  (Router testen, ohne Mikrofon)
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import audio, config, router, tts

log = logging.getLogger("web")
_pipeline = None


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
        if self.path == "/health":
            self._send(200, {"ok": True})
        elif self.path == "/status":
            p = _pipeline
            if not p:
                return self._send(200, {"state": "booting"})
            self._send(200, {
                "state": p.state,
                "stats": p.stats,
                "uptime_s": round(__import__("time").time() - p.stats["started"]),
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
            wav = tts.synthesize(text)
            if not wav:
                return self._send(502, {"error": "TTS leer"})
            threading.Thread(target=self._play, args=(wav,), daemon=True).start()
            return self._send(200, {"ok": True, "bytes": len(wav)})
        if self.path == "/intent":
            if not text:
                return self._send(400, {"error": "text fehlt"})
            answer, kind = router.handle(text)
            return self._send(200, {"answer": answer, "route": kind})
        self._send(404, {"error": "unknown path"})

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
            log.info("Debug-Interface auf :%d", config.WEB_PORT)
            srv.serve_forever()
        except Exception as e:
            log.error("Web-Interface nicht gestartet: %s", e)

    threading.Thread(target=serve, daemon=True).start()
