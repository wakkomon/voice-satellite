"""HTTP-Client (stdlib, mit Timeout + Retry) – wird von stt/router/tts genutzt."""
import json
import time
import urllib.request
import urllib.error


def post_json(url, payload, token=None, timeout=30, retries=1):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"POST {url} fehlgeschlagen: {last}")


def post_multipart(url, filename, content_type, body, fields=None, token=None, timeout=30):
    """multipart/form-data ohne externe Libs (Whisper erwartet file=@...)."""
    boundary = "----voicesatboundary78123"
    parts = []
    for key, val in (fields or {}).items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n{val}\r\n'
        )
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    )
    head = "".join(parts).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    data = head + body + tail
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url, token=None, timeout=30):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_text(url, token=None, timeout=30):
    """openHABs /state liefert text/plain – mit Accept: application/json kommt HTTP 400."""
    headers = {"Accept": "text/plain"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8").strip()


def post_raw(url, body, token=None, timeout=15, content_type="text/plain"):
    """openHAB erwartet den State-Rohwert (ON/OFF/50) als Body."""
    req = urllib.request.Request(
        url, data=str(body).encode("utf-8"), method="POST",
        headers={"Content-Type": content_type,
                 **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 201, 202, 204)
    except urllib.error.HTTPError as e:
        return e.code in (200, 202, 204)


def post_stream(url, payload, token=None, timeout=30, chunk_size=8192):
    """POST mit Streaming-Antwort; generator über Bytes-Chunks."""
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            yield chunk
