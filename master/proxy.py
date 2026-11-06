"""Zentrale API-Proxy-Endpunkte am Master

Der Master liegt zwischen Satelliten und den Voice-/LLM-Backends:

    /proxy/tts     POST  →  TTS-Backend   (OpenAI /v1/audio/speech, Streaming-WAV)
    /proxy/stt     POST  →  STT-Backend   (multipart /v1/audio/transcriptions)
    /proxy/llm     POST  →  LLM-Backend   (OpenAI /v1/chat/completions)
    /proxy/openhab  *    →  OpenHAB-REST-API
    /proxy/music    GET  →  Musik-Dateien vom CIFS-Mount (Range-Requests)

Ziele + Secrets: zentrale Config (master/satconfig.py) bzw. ENV-Fallback
am Master. Satelliten senden OHNE Secret; der Proxy injiziert das
Backend-Token. Damit die Secrets nie auf den Satelliten/der Web-UI landen.

Zusätzlich: Optionales Per-Satellite-TOKEN (Authorization: *** gegen
unbefugte Fremdnutzung des Proxys, wenn AUTH_TOKEN gesetzt ist.
"""
from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlparse

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from . import chatstore
from . import config as master_cfg
from .satconfig import store as config_store

log = logging.getLogger("master.proxy")

# ENV-Fallbacks, falls (noch) keine zentrale Config-URL hinterlegt ist.
#
# WICHTIG (Loop-Guard): Die hier genannten Werte sind die *echten Upstream-
# Backends* (z.B. TTS-Container 192.168.1.31:7811), die am Master liegen.
# Sie sind NICHT dieselben Keys, die in der zentralen Satellite-Config
# (satconfig.EDITABLE_KEYS) gespeichert werden: Dort steht TTS_URL als
# *Proxy-Adresse* (…/proxy/tts), an die die Satelliten sich wenden. Der
# Proxy liest daher für sein Upstream-Ziel NIE TTS_URL aus dem Store, sondern
# ausschließlich TTS_BACKEND_URL (oder den ENV-Fallback), sonst würde er sich
# selbst anprechen (unendliche Schleife).
ENV_TTS_URL = os.environ.get("TTS_BACKEND_URL", os.environ.get("TTS_URL",
                                                              "http://192.168.1.31:7811/v1/audio/speech"))
ENV_STT_URL = os.environ.get("STT_BACKEND_URL", os.environ.get("STT_URL", ""))
ENV_LLM_URL = os.environ.get("LLM_BACKEND_URL", os.environ.get("LLM_URL", ""))
ENV_TTS_TOKEN = os.environ.get("TTS_BACKEND_TOKEN", os.environ.get("TTS_TOKEN", ""))
ENV_STT_TOKEN = os.environ.get("STT_BACKEND_TOKEN", os.environ.get("STT_TOKEN", ""))
ENV_LLM_TOKEN = os.environ.get("LLM_BACKEND_TOKEN", os.environ.get("LLM_TOKEN", ""))
ENV_OPENHAB_TOKEN = os.environ.get("OPENHAB_TOKEN", "")
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")  # leer = Auth aus


def _master_token() -> str:
    """Zentrales Satelliten-Access-Token (UI-Secret, ENV-Fallback)."""
    tok = config_store.get_secret("MASTER_TOKEN")
    if tok:
        return str(tok).strip()
    return os.environ.get("MASTER_TOKEN", "").strip()


def auth_tokens() -> list:
    """Gueltige Tokens fuer Satelliten->Master (AUTH_TOKEN + MASTER_TOKEN)."""
    out = []
    for t in (AUTH_TOKEN, _master_token()):
        t = str(t or "").strip()
        if t and t not in out:
            out.append(t)
    return out


def _is_self_proxy(url: str) -> bool:
    """True, wenn url auf einen /proxy/-Endpoint dieses Masters zeigt
    (Schutz gegen up-to-proxy-Schleifen bei falscher ENV-Konfiguration)."""
    if not url:
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    return p.path.split("?")[0].rstrip("/").startswith("/proxy/")


def _upstream_url(key_url: str, env_url: str) -> str:
    """Echtes Upstream-Backend (aus ENV, NIE aus dem Config-Store, wo die
    Proxy-Adresse liegt) – mit Loop-Guard gegen up-to-proxy-Ziele."""
    url = env_url or ""
    if not url:
        return ""
    if _is_self_proxy(url):
        log.warning("%s zeigt auf einen /proxy/-Endpoint – Loop unterbunden", key_url)
        return ""
    return url


def _upstream_token(key_token: str, env_token: str) -> str:
    """Backend-Token: ConfigStore (global) zuerst, dann ENV-Fallback."""
    val = config_store.get_secret(key_token)
    return val or env_token or ""


def _authorized(req: Request) -> bool:
    """Zugriff erlaubt, wenn kein Token gesetzt ist oder der Bearer-Token
    einem der gueltigen Tokens entspricht (AUTH_TOKEN oder MASTER_TOKEN)."""
    toks = auth_tokens()
    if not toks:
        return True
    auth = req.headers.get("authorization", "")
    import hmac
    for t in toks:
        if hmac.compare_digest(auth, f"Bearer {t}"):
            return True
    return False


def _auth_error(req: Request) -> Response | None:
    if not _authorized(req):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


def _headers(token: str, req: Request, extra: dict | None = None) -> dict:
    h: dict = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
    ct = req.headers.get("content-type")
    if ct:
        h["Content-Type"] = ct
    if extra:
        h.update(extra)
    return h


def _fill_tts_defaults(req: Request, body: bytes) -> bytes:
    """Modell/Stimme aus der zentralen Config auffuellen, wenn der Client
    nichts (oder Leer) schickt.

    Die Android-App im Master-Modus sendet beides bewusst leer (Master ist
    die alleinige Quelle); der Pi sendet seinen eigenen Wert. Es wird NUR
    geleerte Felder gefuellt: die App bekommt dadurch dieselbe TTS_VOICE /
    TTS_MODEL wie der Pi, der Pi bleibt unangetastet.
    """
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return body
    if not isinstance(payload, dict):
        return body
    eff = config_store.get_effective(None)
    changed = False
    for field, key in (("model", "TTS_MODEL"), ("voice", "TTS_VOICE")):
        if not str(payload.get(field) or "").strip():
            val = str(eff.get(key) or "").strip()
            if val:
                payload[field] = val
                changed = True
    if not changed:
        return body
    return json.dumps(payload).encode("utf-8")


async def proxy_tts(req: Request) -> Response:
    """TTS-Proxy: JSON rein, audio/wav (Streaming) raus."""
    # Health-Probe: Satelliten-Heartbeat checkt Komponenten mit GET/HEAD.
    if req.method in ("GET", "HEAD"):
        return JSONResponse({"ok": True, "endpoint": "tts"})
    if e := _auth_error(req):
        return e
    url = _upstream_url("TTS_URL", ENV_TTS_URL)
    token = _upstream_token("TTS_TOKEN", ENV_TTS_TOKEN)
    if not url:
        return JSONResponse({"error": "TTS_URL nicht konfiguriert"}, status_code=502)
    body = await req.body()
    # App schickt im Master-Modus leeres model/voice -> aus der zentralen
    # Config auffuellen (gleiche Stimme wie der Pi-Satellit).
    body = _fill_tts_defaults(req, body)
    # Chat-UI: TTS-Ausgabe als Aufnahme merken (Key = Hash des eingelesenen
    # Texts; identischer Text -> dieselbe Aufnahme, keine Duplikate).
    rec_key = None
    try:
        _t = (json.loads(body) or {}).get("input") or ""
        if _t:
            rec_key = chatstore.key_for(_t)
    except (ValueError, AttributeError):
        pass
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0))
    try:
        resp = await client.send(
            httpx.Request("POST", url, content=body,
                          headers=_headers(token, req,
                                           {"Content-Type": "application/json"})),
            stream=True)
    except httpx.HTTPError as e:
        log.warning("TTS-Proxy-Fehler: %s", e)
        await client.aclose()
        return JSONResponse({"error": f"tts upstream: {e}"}, status_code=502)

    async def gen():
        buf = b""
        try:
            async for chunk in resp.aiter_bytes(8192):
                buf += chunk
                yield chunk
            # Stream komplett -> komplette WAV als Chat-Aufnahme sichern
            if rec_key and len(buf) > 400:
                try:
                    chatstore.save_recording(rec_key, buf)
                except Exception as e:
                    log.warning("Chat-Aufnahme: %s", e)
        except httpx.StreamError as e:
            log.warning("TTS-streamer: %s, sende %d Bytes bisher: %s", e, len(buf), e)
            if buf:
                yield buf
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(gen(), media_type="audio/wav")


async def proxy_stt(req: Request) -> Response:
    """STT-Proxy: multipart/form-data rein, JSON-Transkript raus."""
    if req.method in ("GET", "HEAD"):
        return JSONResponse({"ok": True, "endpoint": "stt"})
    if e := _auth_error(req):
        return e
    url = _upstream_url("STT_URL", ENV_STT_URL)
    token = _upstream_token("STT_TOKEN", ENV_STT_TOKEN)
    if not url:
        return JSONResponse({"error": "STT_URL nicht konfiguriert"}, status_code=502)
    body = await req.body()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            r = await client.post(
                url, content=body,
                headers=_headers(token, req))
        # Upstream liefert ggf. keinen JSON-Body (z.B. 500 vom Whisper)
        try:
            return JSONResponse(r.json(), status_code=r.status_code)
        except Exception:
            log.warning("STT-Proxy: Upstream-Response kein JSON (RC %d)", r.status_code)
            return JSONResponse(
                {"error": "stt upstream: keine JSON-Antwort", "detail": r.text[:200]},
                status_code=502)
    except httpx.HTTPError as e:
        log.warning("STT-Proxy-Fehler: %s", e)
        return JSONResponse({"error": f"stt upstream: {e}"}, status_code=502)


async def proxy_llm(req: Request) -> Response:
    """LLM-Proxy: OpenAI chat/completions. stream=true -> SSE durchreichen (App-Chat live), sonst JSON-Vollantwort."""
    if req.method in ("GET", "HEAD"):
        return JSONResponse({"ok": True, "endpoint": "llm"})
    if e := _auth_error(req):
        return e
    url = _upstream_url("LLM_URL", ENV_LLM_URL)
    token = _upstream_token("LLM_TOKEN", ENV_LLM_TOKEN)
    if not url:
        return JSONResponse({"error": "LLM_URL nicht konfiguriert"}, status_code=502)
    body = await req.body()
    # stream=true -> SSE an den Client durchreichen (App-Chat zeigt die
    # Antwort LIVE, waehrend sie waechst). stream=false/fehlend (Pi, sonst)
    # bleibt der bewaehrte Vollantwort-Pfad unberuehrt.
    try:
        want_stream = bool(json.loads(body).get("stream"))
    except Exception:
        want_stream = False
    try:
        if want_stream:
            client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))
            try:
                up_req = client.build_request(
                    "POST", url, content=body,
                    headers=_headers(token, req,
                                     {"Content-Type": "application/json"}))
                up_resp = await client.send(up_req, stream=True)
            except Exception as e:
                await client.aclose()
                log.warning("LLM-Stream-Setup: %s", e)
                return JSONResponse({"error": f"llm upstream: {e}"},
                                     status_code=502)
            return StreamingResponse(
                _llm_stream(up_resp, client),
                status_code=up_resp.status_code,
                headers={"Content-Type": "text/event-stream",
                         "Cache-Control": "no-cache",
                         "X-Accel-Buffering": "no"})
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            r = await client.post(
                url, content=body,
                headers=_headers(token, req,
                                 {"Content-Type": "application/json"}))
        return Response(r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type",
                                                 "application/json"))
    except httpx.HTTPError as e:
        log.warning("LLM-Proxy-Fehler: %s", e)
        return JSONResponse({"error": f"llm upstream: {e}"}, status_code=502)


async def _llm_stream(up_resp, client):
    """Upstream-SSE an den Client durchreichen; Upstream+Client sauber raeumen.

    Der Async-Generator laeuft NACHDEM proxy_llm die Response zurueckgegeben
    hat, deshalb halten wir client + up_resp hier offen und schliessen sie in
    finally (nicht im aufrufenden try/except).
    """
    try:
        async for line in up_resp.aiter_lines():
            yield (line + "\n").encode()
    except Exception as e:
        log.warning("LLM-Stream unterbrochen: %s", e)
    finally:
        for closer in (up_resp.aclose, client.aclose):
            try:
                await closer()
            except Exception:
                pass

async def proxy_openhab(req: Request) -> Response:
    """OpenHAB-Proxy: REST-API rein, JSON raus (GET/POST/PUT/DELETE).

    Der Client sendet zu /proxy/openhab/{subpath} (z.B. /proxy/openhab/lampe_1/state).
    Wir extrahieren den Sub-Pfad und hängen ihn an die echte Upstream-URL.
    """
    if e := _auth_error(req):
        return e
    base_url = _upstream_url("OPENHAB_URL", os.environ.get("OPENHAB_BACKEND_URL", os.environ.get("OPENHAB_URL", "")))
    token = _upstream_token("OPENHAB_TOKEN", ENV_OPENHAB_TOKEN)
    if not base_url:
        return JSONResponse({"error": "OPENHAB_URL nicht konfiguriert"}, status_code=502)
    # Sub-Pfad aus der anfragenden URL extrahieren
    # Robust gegen /proxy/openhab und /proxy/openhab/ (mit oder ohne Slash)
    try:
        path = urlparse(req.url.path).path
        if path.startswith("/proxy/openhab"):
            sub_path = path[len("/proxy/openhab"):]
            # Entferne führenden Slash vom Sub-Pfad
            sub_path = sub_path.lstrip("/")
        else:
            sub_path = ""
    except Exception:
        sub_path = ""
    target = base_url.rstrip("/") + (("/" + sub_path) if sub_path else "")
    # Query-Parameter durchreichen (Item-Liste: ?recursive=false, &type=...).
    # Ohne sie liefert openHAB die Liste verschachtelt und die
    # Geräte-Zuordnung im Satellit funktioniert nicht.
    if req.url.query:
        target += "?" + req.url.query
    method = req.method
    body = await req.body()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            r = await client.request(
                method, target, content=body,
                headers=_headers(token, req, {"Content-Type": req.headers.get("content-type", "application/json")}))
            return Response(r.content, status_code=r.status_code,
                            media_type=r.headers.get("content-type", "application/json"))
    except httpx.HTTPError as e:
        log.warning("OpenHAB-Proxy-Fehler: %s", e)
        return JSONResponse({"error": f"openhab upstream: {e}"}, status_code=502)


async def proxy_music(req: Request) -> Response:
    """Musik-Streaming-Proxy: Liefert Dateien vom CIFS-Mount am Master.
    Unterstützt Range-Requests für flüssiges Streaming.

    Pfad ist relativ zu MUSIC_ROOT, z.B. /proxy/music/Radio/FluxFM.mp3.

    Kein Token: die Route akzeptiert nur GET/HEAD (reines Streaming aus dem
    oeffentlichen /api/music-Index) und ist unten gegen Traversal gesichert —
    damit der Play-Button im WebUI ohne Token funktioniert.
    """
    try:
        rel_path = "/".join(urlparse(req.url.path).path.split("/proxy/music/", 1))
        rel_path = rel_path.strip("/")
        if not rel_path:
            rel_path = (req.query_params.get("path") or "").strip("/")
        if not rel_path:
            return JSONResponse({"error": "kein Pfad angegeben"}, status_code=400)
        # Sichere Pfad-Auflösung (kein Directory-Traversal)
        root = master_cfg.MUSIC_ROOT.rstrip("/")
        abs_path = os.path.normpath(os.path.join(root, rel_path))
        if abs_path != root and not abs_path.startswith(root + os.sep):
            return JSONResponse({"error": "unauthorized path"}, status_code=403)
        if not os.path.isfile(abs_path):
            return JSONResponse({"error": "datei nicht gefunden"}, status_code=404)

        range_hdr = req.headers.get("range")
        file_size = os.path.getsize(abs_path)
        mime_type = _guess_mime(abs_path)

        if range_hdr:
            # Range-Request: Teilbereich senden
            start, end = _parse_range(range_hdr, file_size)
            with open(abs_path, "rb") as f:
                f.seek(start)
                data = f.read(end - start + 1)
            resp = Response(
                content=data,
                status_code=206,
                media_type=mime_type,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(len(data)),
                },
            )
        else:
            # Komplette Datei
            with open(abs_path, "rb") as f:
                data = f.read()
            resp = Response(
                content=data,
                status_code=200,
                media_type=mime_type,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(len(data)),
                },
            )
        return resp
    except Exception as e:
        log.warning("Music-Proxy-Fehler: %s", e)
        return JSONResponse({"error": f"music proxy: {e}"}, status_code=500)


def _guess_mime(path: str) -> str:
    """Mime-Type basierend auf Extension erraten."""
    ext = os.path.splitext(path)[1].lower()
    return {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".m3u": "audio/x-mpegurl",
        ".m3u8": "audio/x-mpegurl",
        ".aac": "audio/aac",
        ".wma": "audio/x-ms-wma",
    }.get(ext, "application/octet-stream")


def _parse_range(range_hdr: str, file_size: int) -> tuple[int, int]:
    """Range-Header parsen und (start, end) zurückgeben."""
    try:
        unit, range_spec = range_hdr.split("=", 1)
        if unit.strip() != "bytes":
            return 0, file_size - 1
        start_end = range_spec.split("-", 1)
        if not start_end[0]:
            # Suffix-Range (letzten N Bytes)
            return file_size - int(start_end[1]), file_size - 1
        start = int(start_end[0])
        if not start_end[1]:
            return start, file_size - 1
        end = int(start_end[1])
        return start, min(end, file_size - 1)
    except (ValueError, IndexError):
        return 0, file_size - 1
