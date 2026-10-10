"""Web-Interface (stdlib http.server, kein FastAPI/armv7-Risiko).

GET  /              -> HTML-Echtzeitansicht (app/ui.html)
GET  /health        -> {"ok": true}
GET  /frame         -> PNG: aktuelles Display-Bild (Framebuffer)
GET  /status        -> Zustand, Statistik, letzte Events
GET  /volume        -> aktuelle Lautstärke {"volume": 80, "muted": false}
GET  /events        -> SSE-Stream: alle Pipeline-Events in Echtzeit
POST /speak         -> {"text": "..."}  (antwortet direkt per TTS)
POST /intent        -> {"text": "..."}  (Router testen, ohne Mikrofon)
POST /volume        -> {"volume": 0..100} und/oder {"muted": true|false}
POST /tone          -> {"tone": "alarm"}  (Alarm-/Klingelton abspielen)
"""
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, audio, config, display, playback, router, tones, tts, volume

log = logging.getLogger("web")
_pipeline = None
_UI_PATH = os.path.join(os.path.dirname(__file__), "ui.html")
_blip_lock = threading.Lock()
_last_blip = 0.0


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
        elif path == "/frame":
            self._send_frame()
        elif path == "/":
            self._send_html()
        elif path == "/events":
            self._sse()
        elif path == "/volume":
            st = volume.get()
            if st is None:
                return self._send(500, {"error": "Lautstärke nicht ermittelbar"})
            vol, muted = st
            return self._send(200, {"volume": vol, "muted": muted})
        elif path == "/status":
            p = _pipeline
            if not p:
                return self._send(200, {"state": "booting"})
            self._send(200, {
                "state": p.state,
                "stats": p.stats,
                "uptime_s": round(time.time() - p.stats["started"]),
                "version": __version__,
                "events": p.events[-20:],
                "music": playback.player.status(),
                "volume": _vol_snap(),
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
            # channel='web': Kurzhistorie getrennt von der Sprachpipeline –
            # ein getippter Webui-Text darf nicht beim naechsten Wake als
            # Kontext (oder Echo) in der Sprachantwort auftauchen.
            answer, kind, _ok = router.handle(text, channel="web")
            dt = time.time() - t0
            self._ev(f"Web-Intent {dt:.1f}s ({kind}): {answer}")
            return self._send(200, {"answer": answer, "route": kind})
        if self.path == "/tone":
            # Alarm-/Klingelton: {"tone": "alarm|wake|end|boot|error",
            #                     "repeat": 3?}  – repeat nur bei alarm.
            tone = (data.get("tone") or data.get("kind") or "alarm").strip().lower()
            if tone not in ("alarm", "wake", "end", "boot", "error"):
                return self._send(400, {"error": f"unknown tone {tone!r} "
                                                 "(alarm|wake|end|boot|error)"})
            try:
                repeat = max(1, min(20, int(data.get("repeat", 1))))
            except (TypeError, ValueError):
                return self._send(400, {"error": "repeat muss eine Zahl sein"})
            self._ev(f"Web: Ton {tone!r} ({repeat}x)")
            threading.Thread(target=self._play_tone, args=(tone, repeat),
                             daemon=True).start()
            return self._send(200, {"ok": True, "tone": tone, "repeat": repeat})
        if self.path == "/volume":
            # {"volume": 0..100?} und/oder {"muted": true|false} –
            # mindestens eines muss vorhanden sein.
            vol = data.get("volume")
            muted = data.get("muted")
            if vol is None and muted is None:
                return self._send(400, {"error": "volume oder muted fehlt"})
            try:
                if vol is not None:
                    vol = int(vol)
                if not isinstance(muted, bool):
                    if muted is not None:
                        raise ValueError("muted muss true/false sein")
                volume.set_volume(vol, muted=muted)
            except Exception as e:
                return self._send(400, {"error": str(e)})
            st = volume.get()
            if st is not None:
                cur = st
            elif vol is None:
                cur = (-1, bool(muted))
            else:
                cur = (vol, bool(muted))
            self._ev(f"Web: Lautstärke auf {cur[0]}%")
            _blip()                                  # Kontrollton
            return self._send(200, {"ok": True, "volume": cur[0],
                                    "muted": cur[1]})
        if self.path == "/play":
            # Musik: {"path": "mp3/..."} (relativ zu MUSIC_ROOT, Datei oder
            # .m3u8-Playliste) ODER {"url": "http(s)://..."} (Radio-Stream).
            rel = (data.get("path") or "").strip()
            url = (data.get("url") or "").strip()
            if not rel and not url:
                return self._send(400, {"error": "path oder url fehlt"})
            if rel:
                err = playback.player.play_path(rel)
            else:
                err = playback.player.play_url(url, data.get("title") or "")
            if err:
                self._ev(f"Web-Play fehlgeschlagen: {err}")
                return self._send(404, {"error": err})
            return self._send(200, {"ok": True, **playback.player.status()})
        if self.path == "/stop":
            playback.player.stop()
            return self._send(200, {"ok": True})
        if self.path == "/stop_agent":
            # STOP für die Pipeline (LLM/TTS unterbrechen, zurück zu wake)
            if not _pipeline:
                return self._send(503, {"error": "Pipeline nicht aktiv"})
            _pipeline.stop_pipeline()
            return self._send(200, {"ok": True, "state": "wake"})
        if self.path == "/listen":
            # Manuelle Aufnahme vom Web-UI (Start/STOP-Button)
            if not _pipeline:
                return self._send(503, {"error": "Pipeline nicht aktiv"})
            ok, msg = _pipeline.manual_listen()
            return self._send(200, {"ok": ok, "message": msg, "state": _pipeline.state})
        if self.path == "/pause":
            # Web-UI-Pause: eigener Counter, haengt die Wiedergabe an der
            # Position an (ohne die Sprachpipeline zu betreffen).
            if not (playback.player.is_playing() or playback.player.has_pending()):
                return self._send(200, {"ok": True, "paused": False})
            playback.player.pause_web()
            return self._send(200, {"ok": True, "paused": True})
        if self.path == "/resume":
            playback.player.resume_web()
            return self._send(200, {"ok": True, **playback.player.status()})
        self._send(404, {"error": "unknown path"})

    def _ev(self, msg):
        """Aktivitaeten aus dem Webthread in den Live-Feed melden."""
        p = _pipeline
        if p:
            p.ev(msg)

    # ── UI & SSE ──────────────────────────────────────────────
    def _send_frame(self):
        """Aktuelles Display-Bild als PNG (Framebuffer-Snapshot).
        Cache: 1 s – das Bild ändert sich nur bei neuen Inhalten,
        und das Encoding auf armv7l soll nicht unnötig die CPU belegen."""
        body = display.snapshot_png()
        if body is None:
            return self._send(404, {"error": "Display nicht aktiv"})
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "max-age=1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
                        "version": __version__,
                        "events": p.events[-20:],
                        "music": playback.player.status(),
                        "volume": _vol_snap(),
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
        """Web-TTS abspielen; Wake-Loop bleibt waehrenddessen taub und der
        Mic-Stream wird hinterher geleert (kein Echo ins naechste Wake).
        Musik macht ueber den Player-Hold Platz, sonst schreibt aplay auf
        ein bereits belegtes Geraet."""
        p = _pipeline
        if p:
            p.web_audio_busy = True
        playback.player.pause()
        try:
            audio.play_wav(wav)
        except Exception as e:
            log.error("Speak-Fehler: %s", e)
        finally:
            playback.player.resume()
            if p:
                p.drain_mic()          # Echo-Reste verwerfen
                p.web_audio_busy = False

    @staticmethod
    def _play_tone(tone, repeat):
        """Ton wiederholt abspielen; audio.play_wav blockiert bis aplay fertig.
        Wie beim Web-TTS: Wake-Loop taub + Mic-Stream leer nach dem Ton +
        Player-Hold, damit die Musik nicht mit schreibt."""
        p = _pipeline
        if p:
            p.web_audio_busy = True
        playback.player.pause()
        try:
            for _ in range(repeat):
                tones.play(tone)
        except Exception as e:
            log.error("Ton-Fehler: %s", e)
        finally:
            playback.player.resume()
            if p:
                p.drain_mic()
                p.web_audio_busy = False

    def log_message(self, fmt, *args):
        log.debug("http: " + fmt, *args)


def _blip():
    """Lautstärke-Kontrollton (Blip): kurzes Klick-Signal, wenn die
    Lautstärke in der Web-UI geändert wird. Gedrosselt auf 1/10 s –
    der Slider feuert bei ~400 ms Debounce je Ziehen einen POST,
    schnelleres Tippen darf nicht zur Tonsalve werden."""
    global _last_blip
    with _blip_lock:
        now = time.time()
        if now - _last_blip < 1.0:
            return
        _last_blip = now
    threading.Thread(target=Handler._play_tone, args=("blip", 1),
                     daemon=True).start()


def _vol_snap():
    """Lautstaere-Zustand fuer Status/SSE (ohne Crash bei amixer-Fehler)."""
    try:
        st = volume.get()
    except Exception:
        return None
    if st is None:
        return None
    vol, muted = st
    return {"volume": vol, "muted": muted}


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
