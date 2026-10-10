"""Local Intent Recognition: 5 Befehle werden lokal aufgelöst, bevor sie ans LLM gehen.

    1. „spiel[e] die Playlist[e] <name>“  → M3U/M3U8 in MUSIC_ROOT abspielen
    2. „spiel[e] Radio <name>“            → Radio-Sender in radio.m3u finden + spielen
    3. „spiel[e] <name>“ (MP3)            → MP3/Playlist im mp3-Verzeichnis abspielen
    4. „Lautstärke <0-5>“                 → 5-Stufen-Alsa-Volume (0=stumm, 5=100%)
    5. „zeige Kamera <name>“             → Kamera-M3U lesen + Standbild auf Display

Alle Funktionen geben (handled: bool, response: str | None) zurück.
handled=True → Befehl wurde lokal verarbeitet, nicht ans LLM weitergeben.
"""
from __future__ import annotations

import glob
import fnmatch
import logging
import os
import re
import threading
import time

from . import config, display, playback, volume

log = logging.getLogger("intent")

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _fuzzy_match(query: str, candidates: list[str],
                  *, min_score: int = 1) -> str | None:
    """Return best-case-insensitive, substring/fuzzy match or None."""
    q = query.lower().replace("'", "").replace("ä", "a").replace("ö", "o").replace("ü", "u")
    # 1. Exact match (case-insensitive)
    for c in candidates:
        if c.lower() == q:
            return c
    # 2. Starts-with
    for c in candidates:
        if c.lower().startswith(q):
            return c
    # 3. Substring
    for c in candidates:
        if q in c.lower():
            return c
    # 4. Fuzzy: both words present in order
    qwords = q.split()
    if len(qwords) > 1:
        for c in candidates:
            cl = c.lower()
            indices = []
            for w in qwords:
                idx = cl.find(w)
                if idx < 0:
                    break
                indices.append(idx)
            if len(indices) == len(qwords) and all(indices[i] <= indices[i+1] for i in range(len(indices)-1)):
                return c
    return None


def _find_m3u_in_root(name: str, root: str) -> str | None:
    """Search root (and subdirs) for M3U/M3U8 file matching *name*."""
    patterns = [f"*{name}*.m3u", f"*{name}*.m3u8"]
    for pat in patterns:
        for f in glob.glob(os.path.join(root, pat), recursive=True):
            if os.path.isfile(f):
                return os.path.relpath(f, root)
    # Also try just the name as-is
    full = os.path.join(root, name)
    for ext in (".m3u", ".m3u8"):
        if os.path.isfile(full + ext):
            return os.path.relpath(full + ext, root)
    return None


# ---------------------------------------------------------------------------
# 1. Playlist
# ---------------------------------------------------------------------------

def try_play_playlist(text: str) -> tuple[bool, str | None]:
    """Match „spiel[e] (die) Playliste|playlist (ab|auf|ab|auf) <name>".
    
    Returns (True, „Playliste <name> wird abgespielt") or (False, None).
    """
    t = text.lower().strip()
    # Patterns: spiel playlist X / spiele die playliste X / spiel playliste X ab
    m = re.match(r"^(?:spiel|spiele)\s+(?:die\s+)?playlist(e)?\s+(.+)", t)
    if not m:
        return False, None

    name = m.group(2).strip()
    if not name:
        return True, "Welche Playlist?"
    name = name.replace(" ab", "").replace(" auf", "").rstrip()

    root = config.MUSIC_ROOT
    if not os.path.isdir(root):
        return True, f"Musikordner nicht erreichbar: {root}"

    match = _find_m3u_in_root(name, root)
    if match:
        log.info("intent: playlist match -> %s", match)
        err = playback.player.play_path(match)
        if err:
            return True, err
        return True, f"Playliste '{os.path.splitext(os.path.basename(match))[0]}' wird abgespielt"

    return True, f"Playliste '{name}' nicht gefunden"


# ---------------------------------------------------------------------------
# 2. Radio
# ---------------------------------------------------------------------------

# Cache for radio station list: (mtime, [stations])
_radio_cache = {}
_radio_lock = threading.Lock()


def _load_radio_stations() -> list[dict]:
    """Load radio.m3u and return list of {name, url}. Cached by mtime."""
    path = os.path.join(config.MUSIC_ROOT, "TV", "radio.m3u")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return []

    with _radio_lock:
        if _radio_cache and _radio_cache.get("mtime") == mt:
            return _radio_cache["stations"]

    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            raw = f.read()
    except OSError:
        return []

    stations = []
    current_name = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            # Extract name after last comma
            if "," in line:
                current_name = line.split(",", 1)[1].strip()
        elif line.startswith("rtsp://") or (line.startswith("http") and current_name):
            if current_name:
                stations.append({"name": current_name, "url": line})
            current_name = None

    with _radio_lock:
        _radio_cache["mtime"] = mt
        _radio_cache["stations"] = stations

    return stations


def try_play_radio(text: str) -> tuple[bool, str | None]:
    """Match „spiel[e] Radio <name>".
    
    Returns (True, „Radio <name> wird abgespielt") or (False, None).
    """
    t = text.lower().strip()
    m = re.match(r"^(?:spiel|spiele)\s+radio\s+(.+)", t)
    if not m:
        return False, None

    name = m.group(1).strip().rstrip()
    if not name:
        return True, "Welcher Radiosender?"

    stations = _load_radio_stations()
    if not stations:
        return True, "Radio-Playliste nicht gefunden"

    # Build display names for matching (lowercase, normalized)
    display_names = [s["name"] for s in stations]
    matched = _fuzzy_match(name, display_names)
    if matched:
        station = next(s for s in stations if s["name"] == matched)
        # NOTE: RTSP-URL enthält eingebettete Credentials → nie loggen
        log.info("intent: radio match -> %s", matched)
        err = playback.player.play_bridge_url(station["url"], title=matched)
        if err:
            return True, err
        return True, f"Radio {matched} wird abgespielt"

    return True, f"Radiosender '{name}' nicht gefunden"


# ---------------------------------------------------------------------------
# 3. MP3 / Musik
# ---------------------------------------------------------------------------

def try_play_mp3(text: str) -> tuple[bool, str | None]:
    """Match „spiel[e] <name>“ – try mp3 dir.
    
    Returns (True, „<name> wird abgespielt") or (False, None).
    
    Only matches if text doesn't already look like a playlist/radio command.
    """
    t = text.lower().strip()
    # Skip if it already matches playlist/radio patterns
    if re.match(r"^(?:spiel|spiele)\s+(?:die\s+)?playlist", t):
        return False, None
    if re.match(r"^(?:spiel|spiele)\s+radio", t):
        return False, None

    m = re.match(r"^(?:spiel|spiele)\s+(.+)", t)
    if not m:
        return False, None

    name = m.group(1).strip().rstrip(" ab auf").rstrip()
    if not name:
        return True, "Was soll ich abspielen?"

    root = config.MUSIC_ROOT
    mp3_dir = os.path.join(root, "mp3")
    if not os.path.isdir(mp3_dir):
        return True, f"MP3-Ordner nicht erreichbar: {mp3_dir}"

    # Strategy: try name in mp3 dir and subdirs
    # 1. Exact file match
    for ext in (".m3u", ".m3u8", ".mp3"):
        fp = os.path.join(mp3_dir, name + ext)
        if os.path.isfile(fp):
            rel = os.path.relpath(fp, root)
            err = playback.player.play_path(rel)
            if err:
                return True, err
            return True, f"'{name}{ext}' wird abgespielt"

    # 2. Glob match for m3u/m3u8 playlists
    for pat in (f"*{name}*.m3u", f"*{name}*.m3u8"):
        for f in glob.glob(os.path.join(mp3_dir, pat), recursive=False):
            if os.path.isfile(f):
                rel = os.path.relpath(f, root)
                err = playback.player.play_path(rel)
                if err:
                    return True, err
                return True, f"Playlist '{os.path.basename(f)}' wird abgespielt"
            # Also recursive
        for f in glob.glob(os.path.join(mp3_dir, "**", pat), recursive=True):
            if os.path.isfile(f):
                rel = os.path.relpath(f, root)
                err = playback.player.play_path(rel)
                if err:
                    return True, err
                return True, f"Playlist '{os.path.basename(f)}' wird abgespielt"

    # 3. Directory match
    dir_path = os.path.join(mp3_dir, name)
    if os.path.isdir(dir_path):
        rel = os.path.relpath(dir_path, root)
        err = playback.player.play_path(rel)
        if err:
            return True, err
        return True, f"Ordner '{name}' wird abgespielt"

    # 4. Search in subdirectories for mp3 files
    for root_dir, _dirs, files in os.walk(mp3_dir):
        for f in files:
            if name.lower() in f.lower() and f.lower().endswith((".mp3", ".m3u", ".m3u8")):
                full = os.path.join(root_dir, f)
                rel = os.path.relpath(full, root)
                err = playback.player.play_path(rel)
                if err:
                    return True, err
                return True, f"'{f}' wird abgespielt"

    return True, f"'{name}' nicht gefunden"


# ---------------------------------------------------------------------------
# 4. Volume (5 steps: 0-5)
# ---------------------------------------------------------------------------

_VOLUME_STEPS = {
    0: (True, None),    # mute
    1: (20, False),     # 20%
    2: (40, False),     # 40%
    3: (60, False),     # 60%
    4: (80, False),     # 80%
    5: (100, False),    # 100%
}


def try_set_volume(text: str) -> tuple[bool, str | None]:
    """Match „Lautstärke|Lautstaerke <0-5>“.
    
    Returns (True, „Lautstärke auf Stufe X gesetzt") or (False, None).
    """
    t = text.lower().strip()
    # Exclude "Prozent" pattern – that's the old LLM handler
    if "prozent" in t:
        return False, None

    m = re.match(r"^(?:lautstaerke|lautstärke|volume)\s+(\d+)\s*(?:prozent)?\s*$", t)
    if not m:
        return False, None

    step = int(m.group(1))
    if not (0 <= step <= 5):
        return True, "Lautstärke muss 0-5 sein"

    if step == 0:
        volume.set_volume(muted=True)
        return True, "Stumm"

    pct, unmute = _VOLUME_STEPS[step]
    volume.set_volume(pct, muted=unmute)
    return True, f"Lautstärke auf Stufe {step} ({pct}%) gesetzt"


# ---------------------------------------------------------------------------
# 5. Camera
# ---------------------------------------------------------------------------

def try_show_camera(text: str) -> tuple[bool, str | None]:
    """Match „zeige Kamera|camera <name>“.
    
    Returns (True, „Kamera X wird angezeigt") or (False, None).
    """
    t = text.lower().strip()
    m = re.match(r"^(?:zeige|show)\s+(?:kamera|camera)\s+(.+)", t)
    if not m:
        return False, None

    name = m.group(1).strip().rstrip()
    if not name:
        return True, "Welche Kamera?"

    kamera_dir = os.path.join(config.MUSIC_ROOT, "Kamera")
    if not os.path.isdir(kamera_dir):
        return True, f"Kamera-Ordner nicht erreichbar: {kamera_dir}"

    # Build camera name list from M3U filenames (strip CAM_ prefix and .m3u)
    camera_files = sorted(f for f in os.listdir(kamera_dir)
                          if f.endswith(".m3u"))
    # Create display names: CAM_Garten_Hinten.m3u -> "Garten Hinten"
    display_names = []
    for f in camera_files:
        base = f[:-4]  # strip .m3u
        # Remove "CAM_" prefix
        if base.upper().startswith("CAM_"):
            base = base[4:]
        # Replace underscores with spaces
        display_names.append(base.replace("_", " "))

    matched_display = _fuzzy_match(name, display_names)
    if matched_display:
        # Find the actual file
        for f in camera_files:
            base = f[:-4]
            if base.upper().startswith("CAM_"):
                base = base[4:]
            display = base.replace("_", " ")
            if display == matched_display:
                # Read the RTSP URL from the M3U file
                fp = os.path.join(kamera_dir, f)
                try:
                    with open(fp, "rb") as fh:
                        raw = fh.read()
                    # Try different encodings
                    rtsp_url = None
                    for enc in ("utf-8", "utf-16", "utf-8-sig", "latin-1"):
                        try:
                            text_content = raw.decode(enc)
                            for line in text_content.splitlines():
                                line = line.strip()
                                if line.startswith("rtsp://"):
                                    rtsp_url = line
                                    break
                        except (UnicodeDecodeError, ValueError):
                            continue
                    if rtsp_url:
                        # NOTE: RTSP-URL enthält eingebettete Credentials → nie loggen
                        log.info("intent: camera match '%s' -> %s", matched_display, f)
                        # Grab a snapshot via Pi-Bridge and show on display
                        return True, _camera_snapshot_and_show(
                            matched_display, rtsp_url)
                    else:
                        return True, f"Kamera '{matched_display}': keine RTSP-URL gefunden"
                except OSError as e:
                    return True, f"Kamera-Datei lesefehler: {e}"

    return True, f"Kamera '{name}' nicht gefunden"


def _camera_snapshot_and_show(camera_name: str, rtsp_url: str) -> str:
    """Kamera-Snapshot via Pi-Bridge holen und auf Display anzeigen."""
    import json as _json
    import urllib.request as _urllib
    from . import config, display
    
    # Frame auf Pi-Server anfragen
    try:
        req = _urllib.Request(f"{config.BRIDGE_URL}/frame/{camera_name}")
        with _urllib.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        if not raw or len(raw) < 100:
            raise ValueError("zu kurzer Frame")
        # Frame auf Pi ablegen (über Bridge oder direkt per ssh — hier: Bridge-Relay)
        display.show_camera(camera_name)
        return f"Kamera {camera_name} wird angezeigt"
    except Exception as e:
        log.error("intent: camera snapshot error: %s", e)
        display.show("Kamera", f"{camera_name}: {e}")
        return f"Kamera {camera_name} konnte nicht angezeigt werden"
