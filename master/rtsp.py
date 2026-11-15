"""RTSP-Proxy des Masters: wandelt RTSP-/Stream-Quellen in HTTP um.

Ersatz fuer die entfallene Pi-Bridge (BRIDGE_HOST/BRIDGE_PORT): Satelliten
kennen nur MASTER_URL + MASTER_TOKEN und spielen Radio/TV ueber den
Master-Strom sowie Kamerabilder ueber den Frame-Endpunkt.

Routen (alle mit Bearer MASTER_TOKEN, siehe proxy._auth_error; /proxy/*
passt das WebGate als Maschinen-Route durch):
  POST /proxy/rtsp/relay                {source, title} -> {relay_id}
  GET  /proxy/rtsp/{relay_id}/stream    MP3-Strom (curl|mpg123|aplay)
  POST /proxy/rtsp/stop                 {relay_id}
  GET  /proxy/rtsp/status               aktive Relays
  GET  /proxy/rtsp/cameras              Kameraliste (Name + M3U-Relativpfad)
  GET  /proxy/rtsp/frame/{relpfad}      Live-Frame als rohes RGB565
                                        (480x320 = 307200 B, display-Vertrag)

Sicherheitsregeln:
  * Der Satellit schickt nur den M3U-RELATIVPFAD (z.B. 'TV/radio.m3u',
    'Kamera/CAM_Tor.m3u') plus Sendertitel — die Strom-URL mit
    eingebetteten Credentials loest der Master aus seiner /m-Kopie der
    Datei auf.
  * rtsp://-URLs werden NIE geloggt; auch ffmpeg-STDERR (dort stehen sie)
    wird vor jeder Uebernahme ins Puffer-Log zensiert.
"""
from __future__ import annotations

import asyncio
import codecs
import logging
import os
import queue
import re
import select
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque

from starlette.responses import JSONResponse, Response, StreamingResponse

from master import config as master_cfg
from master.proxy import _auth_error

log = logging.getLogger("vs.rtsp")

FFMPEG      = "ffmpeg"
MAX_RELAYS  = int(os.environ.get("RTSP_MAX_RELAYS", "3"))
RELAY_TTL_S = int(os.environ.get("RTSP_RELAY_TTL_S", str(2 * 3600)))
CAM_DIR     = os.environ.get("RTSP_CAMERA_DIR", "Kamera")
FRAME_W     = int(os.environ.get("RTSP_FRAME_W", "480"))
FRAME_H     = int(os.environ.get("RTSP_FRAME_H", "320"))
FRAME_BYTES = FRAME_W * FRAME_H * 2          # RGB565 (display.show_camera)

_URL_RE = re.compile(r"^(rtsp|rtmps|rtmp|http|https)://\S+")


def _sanitize(text: str) -> str:
    """Zensiert rtsp/http-URLs (Credentials!) aus Text — fuer Logs/Status."""
    return re.sub(r"(rtsp|rtmps|rtmp|https?)://[^\s'\"]+", r"\1://***", text)


# ── M3U-Auflösung (nur der Master sieht die echten URLs) ──────────────
def _read_m3u_text(path: str) -> str:
    """M3U lesen; UTF-16-kodierte (Kamera-Dateien) wie UTF-8 tolerant."""
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return codecs.decode(raw, "utf-16")


def _m3u_pairs(text: str) -> list[tuple[str, str]]:
    """(titel, url)-Paare: EXTINF-Zeile + darauffolgende URL-Zeile."""
    out: list[tuple[str, str]] = []
    pending = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = re.match(r"^#EXTINF[^,]*,(.*)$", line)
            if m:
                pending = m.group(1).strip()
            continue
        m = _URL_RE.match(line)
        if m:
            out.append((pending, m.group(0)))
            pending = ""
    return out


def _inject_credentials(url: str) -> str:
    """RTSP_USER/RTSP_PASS injizieren, falls die URL kein '@' traegt
    (Verhalten der ehemaligen Pi-Bridge; die ENV-Werte sind optional)."""
    if url.startswith("rtsp://") and "@" not in url:
        user = os.environ.get("RTSP_USER", "")
        pw = os.environ.get("RTSP_PASS", "")
        if user:
            auth = f"{user}:{pw}@" if pw else f"{user}@"
            url = "rtsp://" + auth + url[len("rtsp://"):]
    return url


def resolve_source(source: str, title: str = "") -> tuple[str | None, str | None]:
    """M3U-Relativpfad (unter MUSIC_ROOT) -> (strom-url, None) oder
    (None, fehlermeldung). Titel gewinnt bei EXTINF-Uebereinstimmung,
    sonst die erste URL. Pfad-Traversing ist gesperrt; Fehlermeldungen
    nennen nie eine URL."""
    rel = (source or "").strip().lstrip("/")
    if not rel:
        return None, "leere Quelle"
    if not rel.lower().endswith((".m3u", ".m3u8")):
        return None, "nur M3U-Dateipfade erlaubt"
    base = os.path.realpath(master_cfg.MUSIC_ROOT)
    path = os.path.realpath(os.path.join(base, rel))
    if path != base and not path.startswith(base + os.sep):
        return None, "ungueltiger Pfad"
    if not os.path.isfile(path):
        return None, f"Datei nicht gefunden: {rel}"
    try:
        pairs = _m3u_pairs(_read_m3u_text(path))
    except Exception:
        return None, "M3U nicht lesbar"
    if not pairs:
        return None, "keine Stream-URL in M3U"
    chosen = None
    if title:
        t = title.strip().lower()
        for name, url in pairs:
            n = name.lower()
            if n and (n == t or t in n or n in t):
                chosen = url
                break
    if chosen is None:
        chosen = pairs[0][1]
    return _inject_credentials(chosen), None


def _build_ffmpeg_cmd(url: str, transport: str | None) -> list[str]:
    """ffmpeg-Grundbefehl zum MP3-Relay. rtsp-Transport-Optionen NUR bei
    rtsp://-Quellen (http/https/HLS lehnt die Option ab — 'Option not found').
    Wichtig: -vn (kein Video), aber KEIN -an (das wuerde das Audio killen)."""
    cmd = [FFMPEG, "-loglevel", "error"]
    if url.startswith("rtsp://") and transport:
        cmd += ["-rtsp_transport", transport]
    cmd += ["-i", url,
            "-vn",
            "-c:a", "libmp3lame", "-b:a", "128k", "-ar", "44100",
            "-f", "mp3", "pipe:1"]
    return cmd


def _rtsp_transports(url: str = "") -> list[str]:
    """Transport-Probereihenfolge. Default: TCP zuerst (stabil, NAT),
    dann UDP-Fallback — die Fritz!box-AVM-RTSP lehnt TCP mit '461 Unknown'
    ab; RTSP_TRANSPORT erzwingt einen einzelnen Transport.
    (Ohne -stimeout: von Debian-ffmpeg 5.1 und dem alten Pi-ffmpeg nicht
    unterstuetzt, wie es schon in der Bridge-Doku stand.)

    SAT>IP-/Fritz!Box-Quellen (URL-Flag 'avm=') werden NIE mit TCP
    angefahren: der sofort scheiternde TCP-Attempt hinterlaesst eine
    halb offene Server-Session, und der anschliessende UDP-Versuch
    bekommt zwar RTP-Pakete auf den Socket, aber Relay/Session passen
    nicht zusammen — der Stream bleibt dauerhaft bei 0 Bytes stecken
    (live reproduziert am 15.11.). Fuer avm=-URLs daher direkt udp."""
    forced = os.environ.get("RTSP_TRANSPORT", "").strip().lower()
    if forced in ("tcp", "udp"):
        return [forced]
    if "avm=" in url:
        return ["udp"]
    return ["tcp", "udp"]


# ── Relay-Verwaltung ──────────────────────────────────────────────────
class _Relay:
    __slots__ = ("relay_id", "url", "title", "source", "started",
                 "last_request", "q")

    def __init__(self, url: str, title: str, source: str):
        self.relay_id = uuid.uuid4().hex[:12]
        self.url = url
        self.title = title or os.path.basename(source)
        self.source = source
        self.started = time.time()
        self.last_request = time.time()
        self.q: queue.Queue = queue.Queue(maxsize=2)


class RelayManager:
    """Aktive ffmpeg-RTSP-Relays. Ein ffmpeg-Prozess pro Relay, ein
    Pump-Thread pumpt die MP3-Ausgabe in einen kleinen Queue (volle queue
    -> Backpressure auf ffmpeg: live statt Vorab-Puffer). Client-Ausstieg
    laesst das Relay laufen (Radio-Resume verbindet neu an denselben
    Strom); der Reaper raeumt Totes und TTL-Orphanes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._relays: dict[str, _Relay] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._errbuf: dict[str, deque] = {}

    # -- Start / Stop -------------------------------------------------
    def start(self, url: str, title: str, source: str) -> tuple[_Relay, list[str]]:
        """Relay starten; ggf. aelteste verdraengen (max. MAX_RELAYS).
        Gibt (relay, verdraengte_ids) zurueck."""
        with self._lock:
            self._reap_locked()
            evicted: list[str] = []
            while len(self._relays) >= MAX_RELAYS:
                victim = min(self._relays.values(), key=lambda r: r.last_request)
                self._kill_locked(victim.relay_id)
                evicted.append(victim.relay_id)
            relay = _Relay(url, title, source)
            self._relays[relay.relay_id] = relay
            self._errbuf[relay.relay_id] = deque(maxlen=20)
            if shutil.which(FFMPEG) is None:
                self._relays.pop(relay.relay_id, None)
                self._errbuf.pop(relay.relay_id, None)
                raise RuntimeError("ffmpeg fehlt im Master-Image")
            # Transport-Probelauf: ein Transport kann sofort scheitern
            # (Fritz!box lehnt RTSP/TCP mit '461 Unknown' ab; http-Quellen
            # kennen die rtsp-Option nicht). Pro Transport max. 4 s warten;
            # lebt ffmpeg danach, ist der Relay gestartet.
            transports = (_rtsp_transports(url) if url.startswith("rtsp://")
                          else [None])
            # SAT/IP-Quellen (Fritz!Box) vergeben Streams erst nach einer
            # PLAY und koennen eine Session still verhungern lassen: ffmpeg
            # bleibtalive, es kommen aber NIEMALS RTP-Bytes. Einmal tot
            # blockiert die Quelle spaetere Sessions. Deshalb nicht nur
            # "lebt ffmpeg?" pruefen, sondern echte erste Ausgabbytes
            # abwarten und die Session bis zu 2x frisch starten.
            attempts = [(tr, 3 if url.startswith("rtsp://") else 1)
                        for tr in transports]
            proc: subprocess.Popen | None = None
            last_err = ""
            for tr, tries in attempts:
                for attempt in range(1, tries + 1):
                    proc = subprocess.Popen(
                        _build_ffmpeg_cmd(url, tr),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        stdin=subprocess.DEVNULL)
                    if self._first_bytes_ok(proc, max_s=4 + 8 * attempt):
                        break
                    #进程 tot oder Funkstille: verwerfen und neu versuchen
                    try:
                        err_out = proc.stderr.read(4000).decode("utf-8",
                                                                "replace") \
                            if proc.poll() is not None else ""
                    except Exception:
                        err_out = ""
                    last_err = _sanitize(err_out.strip())[-160:] \
                        or "RTSP-Session ohne RTP-Daten"
                    log.info("RTSP-Transport %s Versuch %d/%d an %s: %s",
                             tr or "plain", attempt, tries,
                             relay.title or relay.source, last_err[:120])
                    try:
                        proc.send_signal(signal.SIGINT)
                        proc.wait(timeout=3)
                    except Exception:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                    proc = None
                    time.sleep(1.0)   # Quelle mag halboffene Sessions
                if proc is not None:
                    break
            if proc is None:
                self._relays.pop(relay.relay_id, None)
                self._errbuf.pop(relay.relay_id, None)
                raise RuntimeError("Quelle nicht erreichbar: " + last_err)
            self._procs[relay.relay_id] = proc
            threading.Thread(target=self._pump, args=(relay, proc),
                             name=f"rtsp-pump-{relay.relay_id}",
                             daemon=True).start()
            threading.Thread(target=self._drain_err, args=(relay.relay_id, proc),
                             name=f"rtsp-err-{relay.relay_id}",
                             daemon=True).start()
        # Titel/Quelle sind sicher — die URL selbst wird nie geloggt.
        log.info("RTSP-Relay %s gestartet (%s)", relay.relay_id,
                 relay.title or relay.source)
        return relay, evicted

    def _first_bytes_ok(self, proc: subprocess.Popen, max_s: float) -> bool:
        """Wartet bis max_s: Erfolg, sobald ffmpeg die ersten Bytes schreibt
        (sie bleiben im Pipe-Puffer, der Pump liest sie ganz normal).
        Fail fast, wenn ffmpeg sofort exitet; Funkstille bis zum Deadline
        gilt als Misserfolg (Session verhungert)."""
        deadline = time.time() + max_s
        while time.time() < deadline:
            r, _, _ = select.select([proc.stdout], [], [], 0.5)
            if r:
                return True
            if proc.poll() is not None:
                return False
        return False
    def _pump(self, relay: _Relay, proc: subprocess.Popen) -> None:
        q = relay.q
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                q.put(chunk)               # voll -> ffmpeg schreibt langsamer
        except Exception:
            pass
        q.put(None)                        # EOF-Sentinel

    def _drain_err(self, rid: str, proc: subprocess.Popen) -> None:
        """ffmpeg-STDERR zensiert mitlesen (rtsp-URLs koennen dort stehen)."""
        try:
            for raw in proc.stderr:
                with self._lock:
                    buf = self._errbuf.get(rid)
                if buf is not None:
                    buf.append(_sanitize(raw.decode("utf-8", "replace").strip()))
        except Exception:
            pass

    def _kill_locked(self, rid: str, reason: str = "") -> None:
        """(nur mit Lock aufrufen) ffmpeg stoppen und Eintrag entfernen."""
        proc = self._procs.pop(rid, None)
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass
        relay = self._relays.pop(rid, None)
        self._errbuf.pop(rid, None)
        if relay is not None and reason:
            log.info("RTSP-Relay %s %s (%s)", rid, reason,
                     relay.title or relay.source)

    def stop(self, rid: str) -> bool:
        with self._lock:
            if rid not in self._relays:
                return False
            self._kill_locked(rid, reason="gestoppt")
            return True

    def touch(self, rid: str) -> _Relay | None:
        """Relay fuer Stream-Request holen; None wenn nicht (mehr) aktiv."""
        with self._lock:
            relay = self._relays.get(rid)
            proc = self._procs.get(rid)
            if relay is None or proc is None or proc.poll() is not None:
                return None
            relay.last_request = time.time()
            return relay

    def _dead_locked(self, rid: str) -> bool:
        p = self._procs.get(rid)
        return p is not None and p.poll() is not None

    def dead(self, rid: str) -> bool:
        with self._lock:
            return self._dead_locked(rid)

    def info(self) -> list[dict]:
        with self._lock:
            now = time.time()
            items = []
            for r in self._relays.values():
                items.append({"relay_id": r.relay_id, "title": r.title,
                              "source": r.source,
                              "seconds": int(now - r.started),
                              "running": not self._dead_locked(r.relay_id)})
            return items

    def errors(self, rid: str) -> list[str]:
        with self._lock:
            return list(self._errbuf.get(rid, ()))

    def _reap_locked(self) -> None:
        """Tote Relays und TTL-Orphanes entfernen (mit Lock)."""
        now = time.time()
        for rid in list(self._relays):
            relay = self._relays[rid]
            dead = self._dead_locked(rid)
            old = (now - relay.last_request) > RELAY_TTL_S
            if dead:
                errs = (" | ".join(list(self._errbuf.get(rid, ()))))
                errs = errs[-160:]
                self._kill_locked(
                    rid,
                    reason=f"beendet (ffmpeg EOF/Fehler)"
                           f"{': ' + errs if errs else ''}")
            elif old:
                self._kill_locked(rid, reason="TTL abgelaufen (Orphan)")

    def reaper_loop(self) -> None:
        while True:
            time.sleep(60)
            try:
                with self._lock:
                    self._reap_locked()
            except Exception:
                log.exception("RTSP-Reaper-Fehler")


manager = RelayManager()
threading.Thread(target=manager.reaper_loop, name="rtsp-reaper",
                 daemon=True).start()


# ── HTTP-Endpunkte ────────────────────────────────────────────────────
async def relay_start(req):
    """POST /proxy/rtsp/relay {source, title} -> {relay_id, stream_path}."""
    err = _auth_error(req)
    if err:
        return err
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    source = str(body.get("source") or "")
    title = str(body.get("title") or "")
    url, problem = resolve_source(source, title)
    if problem:
        return JSONResponse({"error": problem}, status_code=400)
    try:
        relay, evicted = manager.start(url, title, source)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({
        "relay_id": relay.relay_id,
        "stream_path": f"/proxy/rtsp/{relay.relay_id}/stream",
        "evicted": evicted,
    })


async def relay_stop(req):
    """POST /proxy/rtsp/stop {relay_id}."""
    err = _auth_error(req)
    if err:
        return err
    try:
        body = await req.json()
    except Exception:
        body = {}
    rid = str((body or {}).get("relay_id") or "")
    if not rid or not manager.stop(rid):
        return JSONResponse({"error": "Relay nicht gefunden"}, status_code=404)
    return JSONResponse({"stopped": rid})


def relay_status(req):
    """GET /proxy/rtsp/status — aktive Relays (ohne URLs)."""
    err = _auth_error(req)
    if err:
        return err
    return JSONResponse({"relays": manager.info(),
                         "max_relays": MAX_RELAYS})


def camera_list(req):
    """GET /proxy/rtsp/cameras — Kamera-Namen + M3U-Relativpfade.

    Dateinamen-Kameras unter <MUSIC_ROOT>/Kamera/CAM_<Name>.m3u; der
    Satellit braucht die Datei-Inhalte (rtsp-URL) nicht — nur den Pfad."""
    err = _auth_error(req)
    if err:
        return err
    root = os.path.join(master_cfg.MUSIC_ROOT, CAM_DIR)
    cams = []
    try:
        for fname in sorted(os.listdir(root)):
            if not fname.lower().endswith(".m3u"):
                continue
            stem = os.path.splitext(fname)[0]
            if stem.upper().startswith("CAM_"):
                stem = stem[4:]
            cams.append({"name": stem.replace("_", " "),
                         "source": f"{CAM_DIR}/{fname}"})
    except OSError:
        return JSONResponse({"cameras": [],
                             "error": "Kamera-Ordner nicht erreichbar"},
                            status_code=503)
    return JSONResponse({"cameras": cams})


def camera_frame(req):
    """GET /proxy/rtsp/frame/{relpfad} — Live-Frame als rohes RGB565
    (exakt FRAME_BYTES); display.show_camera blittet ohne Konvertierung."""
    err = _auth_error(req)
    if err:
        return err
    rel = req.path_params.get("relpath") or ""
    url, problem = resolve_source(rel, "")
    if problem:
        return JSONResponse({"error": problem}, status_code=400)
    frame_suffix = ["-frames:v", "1", "-an",
                    "-vf", f"scale={FRAME_W}:{FRAME_H}",
                    "-f", "rawvideo", "-pix_fmt", "rgb565le",
                    "pipe:1"]
    p = None
    last_err = ""
    # tcp zuerst; ein Transport kann an der Quelle scheitern (wie beim
    # Relay: Fritz!box lehnt TCP ab) — dann udp-Probelauf.
    for tr in (_rtsp_transports(url) if url.startswith("rtsp://") else [None]):
        cmd = [FFMPEG, "-loglevel", "error"]
        if url.startswith("rtsp://"):
            cmd += ["-rtsp_transport", tr]
        cmd += ["-i", url]
        cmd += frame_suffix
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=20)
        except subprocess.TimeoutExpired:
            log.warning("RTSP-Frame %s: Timeout (%s, Quelle zensiert)", rel, tr)
            return JSONResponse({"error": "Kamera nicht erreichbar (Timeout)"},
                                status_code=504)
        except FileNotFoundError:
            return JSONResponse({"error": "ffmpeg fehlt im Master-Image"},
                                status_code=500)
        if p.returncode == 0:
            break
        last_err = _sanitize((p.stderr or b"").decode("utf-8", "replace"))[:200]
        p = None
    if p is None:
        log.warning("RTSP-Frame %s: kein Transport erfolgreich: %s",
                    rel, last_err)
        return JSONResponse({"error": "Kamera nicht erreichbar"}, status_code=502)
    data = p.stdout
    if len(data) != FRAME_BYTES:
        log.warning("RTSP-Frame %s: %d Bytes (erwartet %d): %s", rel,
                    len(data), FRAME_BYTES,
                    _sanitize((p.stderr or b"").decode("utf-8", "replace"))[:200])
        return JSONResponse(
            {"error": f"Frame-Fehler: {len(data)} Bytes (erwartet {FRAME_BYTES})"},
            status_code=502)
    return Response(data, media_type="application/octet-stream",
                    headers={"Cache-Control": "no-store"})


async def relay_stream(req):
    """GET /proxy/rtsp/{relay_id}/stream — MP3-Strom (Relay lebt bis Stop).

    Ein Consumer pro Relay (der Satellit spielt). Client-Ausstieg laesst
    das Relay weiterlaufen; Wiederaufbau verbindet neu an denselben Strom
    und verwirft vorher Gepuffertes (live statt nachtraeglich)."""
    err = _auth_error(req)
    if err:
        return err
    rid = req.path_params.get("relay_id") or ""
    relay = manager.touch(rid)
    if relay is None:
        msg = "Relay nicht aktiv"
        errs = manager.errors(rid)
        if errs:
            msg += f" ({errs[-1][:160]})"
        return JSONResponse({"error": msg}, status_code=404)

    q = relay.q
    # Altpuffer verwerfen (Client war evtl. laenger weg -> live ab jetzt)
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass

    loop = asyncio.get_running_loop()

    async def gen():
        while True:
            try:
                chunk = await loop.run_in_executor(
                    None, lambda: q.get(timeout=10))
            except queue.Empty:
                if manager.dead(rid):
                    break
                continue                       # Funkstille: weiter warten
            if chunk is None:                  # ffmpeg EOF/Beendet
                break
            yield chunk

    return StreamingResponse(gen(), media_type="audio/mpeg")
