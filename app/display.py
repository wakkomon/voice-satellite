"""Framebuffer-Display: schreibt Text direkt auf /dev/fb1 (Pillow/fbi-frei).

Mechanik wie getw.sh auf dem Pi: 480x320, 16-Bit RGB565 (little-endian), das
Framebuffer-Device wird als Ganzes überschrieben. Ein eigener 5x7-Bitmap-Font
mit Paginierung ersetzt das externe Rendering – der armv7l-Container braucht
damit weder Pillow noch fbi/fbconversions.

Der Worker läuft in einem Daemon-Thread; show() ist nicht-blockierend und
legt nur den neuesten Frame ab (Condition-Variable + Stop-Event, damit es
keinen Queue.join()-Deadlock geben kann). Lange Antworten werden alle
FB_PAGE_SEC automatisch weitergeblättert; nach FB_TIMEOUT ohne neuen Inhalt
schwarmt das Display aus.
"""
from __future__ import annotations

import mmap
import os
import struct
import threading
import time
import logging
import zlib

log = logging.getLogger(__name__)

from . import config

# ---------------------------------------------------------------------------
# 5x7-Bitmap-Font: 7 Zeilen à 5 Bit (MSB = linke Spalte).
# ---------------------------------------------------------------------------
_G = {
    " ": (0, 0, 0, 0, 0, 0, 0),
    "!": (4, 4, 4, 4, 0, 4, 0),
    '"': (5, 5, 0, 0, 0, 0, 0),
    "#": (5, 7, 5, 5, 7, 5, 0),
    "$": (2, 6, 5, 2, 5, 3, 2),
    "%": (5, 1, 2, 2, 4, 4, 5),
    "&": (2, 5, 2, 5, 3, 5, 2),
    "'": (4, 4, 0, 0, 0, 0, 0),
    "(": (2, 4, 4, 4, 4, 4, 2),
    ")": (2, 1, 1, 1, 1, 1, 2),
    "*": (0, 5, 2, 7, 2, 5, 0),
    "+": (0, 2, 2, 7, 2, 2, 0),
    ",": (0, 0, 0, 0, 0, 2, 4),
    "-": (0, 0, 0, 7, 0, 0, 0),
    ".": (0, 0, 0, 0, 0, 0, 2),
    "/": (1, 1, 2, 2, 4, 4, 4),
    "0": (2, 5, 5, 5, 5, 5, 2),
    "1": (2, 6, 2, 2, 2, 2, 7),
    "2": (2, 5, 1, 2, 4, 4, 7),
    "3": (7, 1, 1, 2, 1, 5, 2),
    "4": (1, 3, 3, 5, 7, 1, 1),
    "5": (7, 4, 6, 1, 1, 5, 2),
    "6": (2, 4, 4, 6, 5, 5, 2),
    "7": (7, 1, 2, 2, 4, 4, 4),
    "8": (2, 5, 5, 2, 5, 5, 2),
    "9": (2, 5, 5, 3, 1, 1, 2),
    ":": (0, 0, 2, 0, 0, 2, 0),
    ";": (0, 0, 2, 0, 0, 2, 4),
    "<": (1, 2, 4, 0, 4, 2, 1),
    "=": (0, 0, 7, 0, 7, 0, 0),
    ">": (4, 2, 1, 0, 1, 2, 4),
    "?": (2, 5, 1, 2, 0, 2, 0),
    "@": (2, 5, 5, 7, 4, 4, 2),
    "A": (2, 5, 5, 7, 5, 5, 5),
    "B": (6, 5, 5, 6, 5, 5, 6),
    "C": (3, 4, 4, 4, 4, 4, 3),
    "D": (6, 5, 5, 5, 5, 5, 6),
    "E": (7, 4, 4, 6, 4, 4, 7),
    "F": (7, 4, 4, 6, 4, 4, 4),
    "G": (3, 4, 4, 5, 5, 5, 3),
    "H": (5, 5, 5, 7, 5, 5, 5),
    "I": (7, 2, 2, 2, 2, 2, 7),
    "J": (1, 1, 1, 1, 1, 5, 2),
    "K": (5, 5, 6, 4, 6, 5, 5),
    "L": (4, 4, 4, 4, 4, 4, 7),
    "M": (5, 7, 7, 5, 5, 5, 5),
    "N": (5, 7, 5, 5, 5, 5, 5),
    "O": (2, 5, 5, 5, 5, 5, 2),
    "P": (6, 5, 5, 6, 4, 4, 4),
    "Q": (2, 5, 5, 5, 5, 6, 3),
    "R": (6, 5, 5, 6, 5, 5, 5),
    "S": (3, 4, 4, 2, 1, 1, 6),
    "T": (7, 2, 2, 2, 2, 2, 2),
    "U": (5, 5, 5, 5, 5, 5, 2),
    "V": (5, 5, 5, 5, 5, 2, 2),
    "W": (5, 5, 5, 5, 7, 7, 5),
    "X": (5, 5, 2, 2, 2, 5, 5),
    "Y": (5, 5, 2, 2, 2, 2, 2),
    "Z": (7, 1, 2, 2, 4, 4, 7),
    "[": (3, 2, 2, 2, 2, 2, 3),
    "]": (6, 2, 2, 2, 2, 2, 6),
    "_": (0, 0, 0, 0, 0, 0, 7),
}
# Umlaute (Zeile 2 = Trema/Punkt statt Balken)
_G.update({
    "Ä": (2, 5, 0, 2, 5, 5, 5),
    "Ö": (2, 5, 0, 2, 5, 5, 2),
    "Ü": (2, 5, 0, 5, 5, 5, 2),
    "ß": (6, 5, 6, 5, 5, 6, 2),
})

GLYPH_W, GLYPH_H = 5, 7
SCALE = config.FB_FONT_SCALE               # 1 = klein, 2 = doppelt (Default)
CELL_W, CELL_H = (GLYPH_W + 1) * SCALE, (GLYPH_H + 2) * SCALE
MARGIN = 4
HEADER_H = 20 * SCALE                      # Kopfzeile inkl. Trennlinie
PAGE_SEC = config.FB_PAGE_SEC                    # Auto-Blätterrhythmus

# ---------------------------------------------------------------------------
# PNG-Ausgabe für die Web-UI (stdlib zlib, kein Pillow auf armv7l).
# ---------------------------------------------------------------------------
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

_LUT565 = None    # list[bytes]: RGB565-Wert -> RGB888-3-Bytes


def _lut565():
    """RGB565→RGB888-Lookupliste (einmalig gebaut)."""
    global _LUT565
    if _LUT565 is None:
        _LUT565 = [bytes((( (i >> 11) & 31) * 255 // 31,
                          ((i >> 6) & 31) * 255 // 31,
                          (i & 31) * 255 // 31)) for i in range(65536)]
    return _LUT565


def _encode_png(rgb: bytes, w: int, h: int) -> bytes:
    """RGB888-Buffer -> PNG (8-Bit, Farbtyp 2, nicht interlaced)."""
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    stride = w * 3
    raw = bytearray()
    for y in range(h):
        raw.append(0)                                   # Filter: none
        raw += rgb[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _png_chunk(b"IEND", b""))


def _c565(r: int, g: int, b: int) -> bytes:
    v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    return struct.pack("<H", v)


COL_FG = _c565(255, 255, 255)     # Rumpftext
COL_BG = _c565(0, 0, 0)           # Hintergrund
COL_HI = _c565(110, 190, 255)    # Kopfzeile
COL_DIM = _c565(160, 160, 160)   # Seitenanzeige


def _sanitize(text: str) -> str:
    out = []
    for ch in text:
        if ch in _G or ch == "\n":
            out.append(ch)
        else:
            up = ch.upper()
            out.append(up if up in _G else "?")
    return "".join(out)


class Display:
    """show(heading, body) legt Frames ab; der Worker malt und blättert."""

    def __init__(self):
        self._fd = None
        self._mm = None
        self._buf = None
        self._w = config.FB_WIDTH
        self._h = config.FB_HEIGHT
        self._lock = threading.Lock()
        self._cv = threading.Condition()
        self._pending = None          # (heading, body) vom Caller
        self._stop = threading.Event()
        self._worker = None
        self._live = False            # Seite zeigt Inhalt (nicht blackout)
        # Musik-Info-Zustand (von main.py Idle-Cycle genutzt)
        self._playing = False
        self._music_info_at = None    # Zeitstempel: wann Musik-Info angezeigt wurde

    # ── Lifecycle ─────────────────────────────────────────────
    def init(self):
        device = config.FB_DEVICE
        if not os.path.exists(device):
            print(f"[display] {device} nicht vorhanden – Display deaktiviert")
            return
        size = self._w * self._h * 2
        try:
            self._fd = os.open(device, os.O_RDWR)
            self._mm = mmap.mmap(self._fd, size)
        except OSError as e:
            hint = " – Fehler: User fehlt in der video-Gruppe (GID 44)" if e.errno == 13 else ""
            print(f"[display] {device} nicht öffenbar ({e}) – Display deaktiviert{hint}")
            self.close()
            return
        self._buf = bytearray(size)
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name="fb-display")
        self._worker.start()
        self.show("Bereit", f"Codewort: {config.WAKE_WORD}")
        print(f"[display] {device} aktiv ({self._w}x{self._h})")

    def shutdown(self):
        with self._cv:
            self._stop.set()
            self._cv.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
        self.close()

    def close(self):
        if self._mm is not None:
            try:
                self._mm.close()
            except (ValueError, OSError):
                pass
            self._mm = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    # ── Public API (nie blockierend) ──────────────────────────
    def show(self, heading: str, body: str = "") -> None:
        if self._mm is None or self._stop.is_set():
            return
        with self._cv:
            self._pending = (heading, body)
            self._cv.notify_all()

    # ── Musik-Info & Weather (für Idle/Alternierung) ─────────────
    def show_music_info(self, heading: str, body: str = "") -> None:
        """Zeige Musik-Info-Overlay (temporär während Playback)."""
        if self._mm is None or self._stop.is_set():
            return
        with self._cv:
            self._playing = True
            self._music_info_at = time.time()
            self._pending = (heading, body)
            self._cv.notify_all()

    def show_weather(self) -> None:
        """Zeige Weather-PNG aus /data/idle/ als Hintergrund."""
        if self._mm is None or self._stop.is_set():
            return
        with self._cv:
            # Musik-Info verfallen lassen wenn älter als config.IDLE_TIMEOUT
            if self._music_info_at and (time.time() - self._music_info_at > config.IDLE_TIMEOUT):
                self._playing = False
                self._music_info_at = None
            self._pending = ("__weather__", "")  # Marker für Worker
            self._cv.notify_all()

    def is_playing(self) -> bool:
        """Prüfe ob Musik-Info aktiv ist."""
        with self._cv:
            return self._playing

    def set_not_playing(self):
        """Playback beendet: Musik-Info zurücksetzen."""
        with self._cv:
            self._playing = False
            self._music_info_at = None

    def show_camera(self, camera_name: str) -> None:
        """Kamera-Snapshot vom Pi holen und direkt in Framebuffer blitten.
        
        Bridge liefert 307200 Bytes (480×320 RGB565 raw) über /frame/<name>.
        """
        import urllib.request as _urllib
        
        if self._mm is None or self._stop.is_set() or self._buf is None:
            return
        
        # Frame von Bridge holen (rohe RGB565-Daten)
        try:
            url = f"{config.BRIDGE_URL}/frame/{camera_name}"
            req = _urllib.Request(url)
            with _urllib.urlopen(req, timeout=15) as resp:
                raw = resp.read()
        except Exception as e:
            log.warning("show_camera: Bridge-Fehler für %s: %s", camera_name, e)
            self.show("Kamera", f"Verbindung fehlgeschlagen")
            return
        
        expected = config.FB_WIDTH * config.FB_HEIGHT * 2  # 307200
        if len(raw) != expected:
            log.warning("show_camera: Frame %s: %d Bytes erwartet, %d erhalten",
                        camera_name, expected, len(raw))
            self.show("Kamera", f"Frame-Fehler: {len(raw)} Bytes")
            return
        
        # Direkt in Buffer + mmap blitten (keine Konvertierung nötig!)
        try:
            buf = bytearray(raw)
            with self._lock:
                self._buf[:] = buf
                try:
                    self._mm.seek(0)
                    self._mm.write(buf)
                except (ValueError, OSError):
                    pass
        except Exception as e:
            log.error("show_camera: Blit-Fehler für %s: %s", camera_name, e)
            self.show("Kamera", f"Blit-Fehler")
            return
        
        # Kurze Bestätigung als Text-Overlay
        self.show("Kamera", f"{camera_name}: Live")

    # ── Snapshot für die Web-UI ───────────────────────────────
    def snapshot_png(self) -> bytes | None:
        """Aktueller Framebuffer (was grade auf dem Display steht) als PNG.

        None wenn kein Framebuffer (Display deaktiviert). Konsistenter
        Snapshot: Buffer-Kopie unter Lock, Konvertierung+Enc. außerhalb.
        """
        if self._buf is None:
            return None
        with self._lock:
            fb = bytes(self._buf)
        w, h = self._w, self._h
        lut = _lut565()
        rgb = bytearray(w * h * 3)
        # RGB565 (klein-Endian) -> RGB888 via Lookupliste
        for px in range(w * h):
            rgb[px * 3:px * 3 + 3] = lut[fb[px * 2] | (fb[px * 2 + 1] << 8)]
        return _encode_png(bytes(rgb), w, h)

    # ── Worker ────────────────────────────────────────────────
    def _run(self):
        pages, idx, head = [], 0, ""
        shown_at = 0.0
        # Wetter-Blitting in Hintergrund-Thread (blockiert nicht den UI-Thread).
        # Der Flag _weather_done signalisiert, wann das letzte Wetter-Frame
        # bereitsteht – dann wird es im Haupt-Thread nur noch geblittet.
        self._weather_thread: threading.Thread | None = None
        self._weather_pending: bool = False
        self._weather_lock = threading.Lock()

        while not self._stop.is_set():
            frame = None
            with self._cv:
                if self._pending is not None:
                    frame = self._pending
                    self._pending = None
                else:
                    self._cv.wait(timeout=0.25)
            now = time.time()
            if frame is not None and frame[0] == "__weather__":
                # Wetter-Frame asynchron laden und konvertieren.
                # Das eigentliche Blitten passiert im Haupt-Thread (unter Lock).
                if not self._weather_pending:
                    self._weather_pending = True
                    t = threading.Thread(
                        target=self._load_weather, daemon=True, name="weather-load"
                    )
                    t.start()
                    self._weather_thread = t
                # Sofort ein kurzes "Wetter ..." als Platzhalter,
                # damit die UI nicht blockiert.
                self._live = True
                shown_at = now
            elif frame is not None and self._playing:
                # Musik-Info: Text als Overlay ueber Weather-Hintergrund
                pages, head = self._layout(*frame)
                idx = 0
                self._blit_weather()
                if self._buf is not None:
                    with self._lock:
                        self._paint_overlay(pages, idx, head)
                self._live = True
                shown_at = now
            elif frame is not None:
                pages, head = self._layout(*frame)
                idx = 0
                self._paint(pages, idx, head)
                self._live = True
                shown_at = now
            elif pages:
                # Auto-Blättern bei mehreren Seiten, Blackout nach FB_TIMEOUT
                if now - shown_at > config.FB_TIMEOUT:
                    pages, idx, head = [], 0, ""
                    self._paint(pages, idx, head)
                    self._live = False
                elif len(pages) > 1 and now - shown_at > PAGE_SEC:
                    idx = (idx + 1) % len(pages)
                    self._paint(pages, idx, head)
                    shown_at = now

    def _load_weather(self):
        # Hintergrund-Thread: Wetter-Bild laden, RGB565 konvertieren,
        # im shared buffer speichern. Das eigentliche Blitten passiert
        # im Haupt-Thread (unter Lock).
        import PIL.Image as _Image
        import urllib.request as _urllib
        from io import BytesIO

        expected = self._w * self._h * 2
        raw = None
        try:
            req = _urllib.Request(
                config.DEFAULT_IMAGE_URL,
                headers={"User-Agent": "voice-satellite/1.0"},
            )
            with _urllib.urlopen(req, timeout=15) as resp:
                data = resp.read()
            if len(data) < 1000:
                log.warning("_load_weather: zu kleines Bild (%d bytes)", len(data))
                with self._weather_lock:
                    self._weather_pending = False
                return
            img = _Image.open(BytesIO(data)).convert("RGB")
            sw, sh = img.size
            target_ratio = self._w / self._h
            if sw / sh > target_ratio:
                nw = int(sh * target_ratio)
                x0 = (sw - nw) // 2
                img = img.crop((x0, 0, x0 + nw, sh))
            else:
                nh = int(sw / target_ratio)
                y0 = (sh - nh) // 2
                img = img.crop((0, y0, sw, y0 + nh))
            img = img.resize((self._w, self._h), _Image.Resampling.LANCZOS)
            try:
                raw = img.convert("RGB565").tobytes()
            except (ValueError, AttributeError):
                log.warning("_load_weather: kein RGB565, ignoriere Bild")
                with self._weather_lock:
                    self._weather_pending = False
                return
        except Exception as e:
            log.warning("_load_weather: Fehler (%s)", e)
            with self._weather_lock:
                self._weather_pending = False
            return

        if len(raw) != expected or self._buf is None or self._mm is None:
            with self._weather_lock:
                self._weather_pending = False
            return

        # Buffer-Kopie unter Lock (kein Blitten, nur Speichern)
        with self._weather_lock:
            if self._buf is not None:
                self._buf[:] = raw[:expected]
            self._weather_pending = False

    def _blit_weather(self):
        # Wetter-Frame aus dem vorberechneten Buffer (Hintergrund-Thread)
        # auf den fb1 blitten. Falls noch kein fertiger Buffer da ist,
        # wird kurz gewartet (max. 2 s), dann übersprungen.
        raw = None
        wait_start = time.monotonic()
        while self._weather_pending and time.monotonic() - wait_start < 2:
            time.sleep(0.05)

        with self._weather_lock:
            if self._buf is not None and not self._weather_pending:
                raw = bytes(self._buf)

        if raw is None:
            return

        if self._mm is None:
            return

        try:
            self._mm.seek(0)
            self._mm.write(raw)
        except (ValueError, OSError):
            pass

    def _layout(self, heading: str, body: str):
        heading = _sanitize(heading or "").replace("\n", " ")
        body = _sanitize(body or "")
        max_chars = max(4, (self._w - 2 * MARGIN) // CELL_W)
        lines = []
        for para in body.split("\n"):
            words = para.split()
            if not words:
                lines.append("")
                continue
            cur = ""
            for w in words:
                trial = (cur + " " + w).strip()
                if len(trial) <= max_chars:
                    cur = trial
                else:
                    if cur:
                        lines.append(cur)
                    while len(w) > max_chars:      # hart brechen
                        lines.append(w[:max_chars])
                        w = w[max_chars:]
                    cur = w
            if cur:
                lines.append(cur)
        body_rows = max(1, (self._h - HEADER_H - MARGIN
                            - GLYPH_H * SCALE - 2) // CELL_H)
        pages = [lines[i:i + body_rows] for i in range(0, max(len(lines), 1),
                                                    body_rows)]
        return pages, heading

    def _paint(self, pages, idx, heading: str):
        if self._mm is None or self._buf is None:
            return
        with self._lock:
            self._buf[:] = COL_BG * (self._w * self._h)
            if pages:
                self._header(heading)
                y = HEADER_H
                for ln in pages[idx]:
                    x = MARGIN
                    for ch in ln:
                        self._glyph(ch, x, y, COL_FG)
                        x += CELL_W
                    y += CELL_H
                if len(pages) > 1:
                    self._pager(idx, len(pages))
            try:
                self._mm.seek(0)
                self._mm.write(self._buf)
            except (ValueError, OSError):
                pass

    def _paint_overlay(self, pages, idx, heading: str):
        """Text-Overlay ueber dem Weather-Hintergrund (Buffer bleibt
        weather-bebildet, nur die Text-Flaeche wird schwarz abgedeckt).
        Rufer hält _lock."""
        if self._mm is None or self._buf is None:
            return
        if pages:
            w = (self._w - 2 * MARGIN) * 2
            # Kopfzeile + Textblöcke abdecken, damit der Text lesbar ist
            self._fill(0, 0, self._w, HEADER_H + 2, COL_BG)
            y = HEADER_H
            for ln in pages[idx]:
                tw = (len(ln) * CELL_W + MARGIN * 2) * 2
                self._fill(MARGIN, y, tw, CELL_H - 2, COL_BG)
                y += CELL_H
            self._header(heading)
            y = HEADER_H
            for ln in pages[idx]:
                x = MARGIN
                for ch in ln:
                    self._glyph(ch, x, y, COL_FG)
                    x += CELL_W
                y += CELL_H
            if len(pages) > 1:
                self._pager(idx, len(pages))
        try:
            self._mm.seek(0)
            self._mm.write(self._buf)
        except (ValueError, OSError):
            pass

    def _header(self, heading: str):
        x = MARGIN
        for ch in heading[: (self._w - 2 * MARGIN) // CELL_W]:
            self._glyph(ch, x, 5, COL_HI)
            x += CELL_W
        self._fill(0, HEADER_H - 2, self._w, 2, COL_HI)

    def _pager(self, idx: int, total: int):
        s = f"{idx + 1}/{total}"
        x = self._w - MARGIN - len(s) * CELL_W
        y = self._h - GLYPH_H * SCALE - 2
        for ch in s:
            self._glyph(ch, x, y, COL_DIM)
            x += CELL_W

    def _fill(self, x0, y0, w, h, color: bytes):
        row = color * w
        for y in range(y0, min(y0 + h, self._h)):
            off = (y * self._w + x0) * 2
            self._buf[off:off + w * 2] = row

    def _glyph(self, ch: str, x: int, y: int, color: bytes):
        g = _G.get(ch)
        if g is None or x + GLYPH_W * SCALE > self._w or y + GLYPH_H * SCALE > self._h:
            return
        for ry, bits in enumerate(g):
            for rx in range(GLYPH_W):
                if bits & (1 << (GLYPH_W - 1 - rx)):
                    self._fill(x + rx * SCALE, y + ry * SCALE,
                               SCALE, SCALE, color)


# ── Modul-Fassade (von main.py genutzt) ───────────────────────
_instance = None


def init():
    global _instance
    if _instance is None:
        _instance = Display()
        _instance.init()


def show(heading: str, body: str = "") -> None:
    """Display-Text ausgeben (von intent.py genutzt)."""
    if _instance is not None:
        _instance.show(heading, body)


def show_text(heading: str, body: str = "") -> None:
    if _instance is not None:
        _instance.show(heading, body)


def show_weather() -> None:
    """Weather-Frame anzeigen (hostseitig gerendert)."""
    if _instance is not None:
        _instance.show("__weather__", "")


def show_music_info(heading: str, body: str) -> None:
    """Musik-Info-Overlay auf Weather-Hintergrund malen."""
    if _instance is not None:
        _instance.show_music_info(heading, body)


def reset_music_info() -> None:
    """Playback beendet: Overlay-Zustand zuruecksetzen."""
    if _instance is not None:
        _instance.set_not_playing()


def shutdown() -> None:
    global _instance
    if _instance is not None:
        _instance.shutdown()
        _instance = None


def snapshot_png() -> bytes | None:
    """Aktuelles Display-Bild als PNG (Web-UI); None wenn Display aus."""
    if _instance is not None:
        return _instance.snapshot_png()
    return None


def show_camera(camera_name: str) -> None:
    """Modul-Fassade: Kamera-Snapshot vom Pi holen und auf Display blitten."""
    if _instance is not None:
        _instance.show_camera(camera_name)


# ── Ende ──────────────────────────────────────────────────────
