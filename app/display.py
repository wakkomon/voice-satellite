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
CELL_W, CELL_H = GLYPH_W + 1, GLYPH_H + 2   # 1 px seitlich, 2 px vertikal
MARGIN = 4
HEADER_H = 20                                # Kopfzeile inkl. Trennlinie
PAGE_SEC = config.FB_PAGE_SEC                    # Auto-Blätterrhythmus


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
            print(f"[display] {device} nicht öffenbar ({e}) – Display deaktiviert")
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

    # ── Worker ────────────────────────────────────────────────
    def _run(self):
        pages, idx, head = [], 0, ""
        shown_at = 0.0
        while not self._stop.is_set():
            frame = None
            with self._cv:
                if self._pending is not None:
                    frame = self._pending
                    self._pending = None
                else:
                    self._cv.wait(timeout=0.25)
            now = time.time()
            if frame is not None:
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
        body_rows = max(1, (self._h - HEADER_H - MARGIN) // CELL_H)
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

    def _header(self, heading: str):
        x = MARGIN
        for ch in heading[: (self._w - 2 * MARGIN) // CELL_W]:
            self._glyph(ch, x, 5, COL_HI)
            x += CELL_W
        self._fill(0, HEADER_H - 2, self._w, 2, COL_HI)

    def _pager(self, idx: int, total: int):
        s = f"{idx + 1}/{total}"
        x = self._w - MARGIN - len(s) * CELL_W
        y = self._h - GLYPH_H - 2
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
        if g is None or x + GLYPH_W > self._w or y + GLYPH_H > self._h:
            return
        for ry, bits in enumerate(g):
            for rx in range(GLYPH_W):
                if bits & (1 << (GLYPH_W - 1 - rx)):
                    off = ((y + ry) * self._w + x + rx) * 2
                    self._buf[off:off + 2] = color


# ── Modul-Fassade (von main.py genutzt) ───────────────────────
_instance = None


def init():
    global _instance
    if _instance is None:
        _instance = Display()
        _instance.init()


def show_text(heading: str, body: str = "") -> None:
    if _instance is not None:
        _instance.show(heading, body)


def shutdown() -> None:
    global _instance
    if _instance is not None:
        _instance.shutdown()
        _instance = None
