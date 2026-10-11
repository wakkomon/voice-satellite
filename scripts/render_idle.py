#!/usr/bin/env python3
"""Wetter-JPG -> /data/idle (RGB565-Rohframe, 480x320, 153600 Bytes).

Laeuft aus der Root-Cron alle 60 s. Schreibt NIE auf /dev/fb1 – der
Container voice-satellite ist einziger Framebuffer-Schreiber und blittet
das Frame selbst, wenn im Idle-Modus.

Atomarer Write (tmp + rename): ein abgebrochenes Skript hinterlaesst
kein kaputtes Frame, das letzte gute Frame bleibt gueltig.
"""
import os
import sys
import urllib.request

# Quell-URL: aus der shared .env des voice-satellite-Projekts lesen
# (Einheitliche Quelle mit dem Container, Default = Wetter-Snapshot).
DEFAULT_IMAGE = "http://192.168.1.33/weather/snap.jpg"
ENV_FILE = "/home/pi/voice-satellite/.env"
try:
    with open(ENV_FILE, "r", encoding="utf-8", errors="replace") as _f:
        for _line in _f:
            _s = _line.strip()
            if _s.startswith("DEFAULT_IMAGE_URL="):
                DEFAULT_IMAGE = _s.split("=", 1)[1].strip() or DEFAULT_IMAGE
                break
except OSError:
    pass

SRC = DEFAULT_IMAGE
OUT_DIR = "/var/lib/docker/volumes/voice-satellite-data/_data/idle"
OUT = os.path.join(OUT_DIR, "weather.bin")
W, H = 480, 320
SIZE = W * H * 2  # 16-bit RGB565
TARGET = W / H


def main():
    req = urllib.request.Request(SRC, headers={"User-Agent": "render-idle/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = r.read()
    if len(data) < 1000:
        sys.exit("Wetter-JPG zu klein: %d bytes" % len(data))

    tmp_src = "/tmp/render_idle_src.jpg"
    with open(tmp_src, "wb") as f:
        f.write(data)

    from PIL import Image
    img = Image.open(tmp_src)
    img = img.convert("RGB")

    # zentraler Zuschnitt auf 480:320 (3:2), dann Resize
    sw, sh = img.size
    cur = sw / sh
    if cur > TARGET:
        nw = int(sh * TARGET)
        x0 = (sw - nw) // 2
        img = img.crop((x0, 0, x0 + nw, sh))
    else:
        nh = int(sw / TARGET)
        y0 = (sh - nh) // 2
        img = img.crop((0, y0, sw, y0 + nh))
    img = img.resize((W, H), Image.LANCZOS)

    # PIL-Konvertierung RGB565 nicht immer geloes (PIL-Version) –
    # Fallback: Bit-Maske via NumPy. Framebuffer = little-endian 16-Bit.
    try:
        raw = img.convert("RGB565").tobytes()
    except ValueError:
        import numpy as np
        arr = np.asarray(img, dtype=np.uint16)   # H x W x 3
        r5 = (arr[..., 0] >> 3) << 11
        g6 = (arr[..., 1] >> 2) << 5
        b5 = arr[..., 2] >> 3
        raw = (r5 | g6 | b5).astype("<u2").tobytes()
    if len(raw) != SIZE:
        sys.exit("Unerwartete Groesse: %d (erwartet %d)" % (len(raw), SIZE))

    os.makedirs(OUT_DIR, exist_ok=True)
    tmp_out = OUT + ".tmp"
    with open(tmp_out, "wb") as f:
        f.write(raw)
    os.replace(tmp_out, OUT)
    print("OK %s (%d bytes)" % (OUT, len(raw)))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Fehler leise loggen, Cron laesst das letzte gute Frame stehen
        sys.stderr.write("render_idle: %s\n" % e)
        sys.exit(1)
