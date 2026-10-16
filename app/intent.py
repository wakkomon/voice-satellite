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
_lock = threading.Lock()

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


# ---------------------------------------------------------------------------
# Background M3U index — built at startup + refreshed periodically
# Avoids slow CIFS scans on every playlist request
# ---------------------------------------------------------------------------

_m3u_index: dict[str, list[str]] | None = None  # name.lower() -> [relpaths (rel. MUSIC_ROOT)]
_m3u_all: list[str] | None = None               # alle Playlists (rel. MUSIC_ROOT)
_mp3_index: dict[str, list[str]] | None = None  # name.lower() -> [relpaths] für .mp3/.flac/...
_dir_index: dict[str, list[str]] | None = None  # Ordnernamen.lower() -> [relpaths] (z.B. Interpret-Ordner)
_indexer_started = False
_index_build_evt = threading.Event()   # manueller Reindex (WebUI-Button / „Neu indizieren")
_INDEX_REBUILD_INTERVAL = 10800         # s: wie oft der Medien-Index automatisch neu gescannt wird (3h)
_index_building = False                # True, solange ein (Re-)Build läuft (Web-UI/SSE)
_index_files_seen = 0                  # Laufzähler: Audios + Playlists im laufenden Build
_index_last_build = None               # (ts, m3u_n, mp3_n) nach letztem fertigem Build

# Kurze Füllwörter (deutsche Grammatik): tauchen in fast jedem zweiten
# Dateinamen/Befehl auf und machen eine Substring-Suche unsinnig
# ("von" in "musik von falco" → False-Positiv auf "Ritter Von Roßbach").
_IGNORE_TOKENS = frozenset({
    "von", "der", "die", "das", "den", "dem", "des", "ein", "eine",
    "einer", "eines", "am", "im", "um", "zu", "mit", "auf", "an",
    "in", "für", "fuer", "und", "oder", "ist", "sind", "war",
    "bei", "nach", "aus", "vor", "über", "ueber", "unter",
    "bis", "doch", "ich", "du", "er", "sie", "es", "mal",
})


def _build_media_index(root: str, max_depth: int = 3,
                       on_progress=None
                       ) -> tuple[dict[str, list[str]], list[str],
                                  dict[str, list[str]], list[str],
                                  dict[str, list[str]]]:
    """Scan root/mp3 (max_depth Ebenen) in EINEM Walk und baut:
      1. M3U-Index:  name.lower() -> [relpaths]  (Playlists .m3u/.m3u8)
      2. MP3-Index:  name.lower() -> [relpaths]  (Audiodateien .mp3/.flac/...)
      3. Dir-Index:  Ordnernamen.lower() -> [relpaths] (Interpret-Ordner etc.)

    Begrenzung: nur mp3-Unterverzeichnis, nur max_depth Ebenen. Kein
    Zeitbudget — der Baum liegt auf CIFS/SMB und ein abgeschnittener Scan
    würde Songs dauerhaft aus dem Index lassen. on_progress(n_seen) wird
    nach jeder Datei aufgerufen (Live-Zähler für Web-UI/SSE).
    """
    m3u_idx: dict[str, list[str]] = {}
    m3u_all: list[str] = []
    mp3_idx: dict[str, list[str]] = {}
    mp3_all: list[str] = []
    dir_idx: dict[str, list[str]] = {}
    scan_root = os.path.join(root, "mp3")
    if not os.path.isdir(scan_root):
        return m3u_idx, m3u_all, mp3_idx, mp3_all, dir_idx
    AUDIO_EXTS = (".mp3", ".flac", ".ogg", ".wav", ".m4a", ".opus")
    seen = [0]  # Laufzähler für on_progress (Playlists + Audios)

    def _tick() -> None:
        if on_progress is not None:
            try:
                on_progress(seen[0])
            except Exception:
                pass

    def add(idx: dict, paths: list, path: str) -> None:
        rel = os.path.relpath(path, root)
        if rel in paths:
            return
        paths.append(rel)
        seen[0] += 1
        _tick()
        base = os.path.basename(path).rsplit(".", 1)[0]
        tokens = [t.lower() for t in base.replace("_", " ").split() if t] or [base.lower()]
        # Kurze/unspezifische Token ausschließen
        meaningful = [t for t in tokens if t not in _IGNORE_TOKENS and len(t) > 2]
        for token in sorted(set(meaningful)):
            idx.setdefault(token, []).append(rel)
        if base.lower() not in meaningful and base.lower() not in _IGNORE_TOKENS:
            idx.setdefault(base.lower(), []).append(rel)

    def listdir_sorted(d: str) -> list[str]:
        try:
            return sorted(os.listdir(d))
        except OSError:
            return []

    def _index_dir(p: str) -> bool:
        """Ordner *p* in dir_idx eintragen (Schlüssel: Ordnernamen.lower()).
        Gibt True zurück, wenn der Ordner selbst Audio-Dateien enthält."""
        rel = os.path.relpath(p, root)
        nm = os.path.basename(p.rstrip(os.sep)).lower()
        has_audio = False
        for e2 in listdir_sorted(p):
            e2p = os.path.join(p, e2)
            if e2.lower().endswith(AUDIO_EXTS):
                try:
                    if os.path.isfile(e2p):
                        has_audio = True
                except OSError:
                    pass
            elif has_audio:
                break
        if nm and len(nm) > 1 and nm not in _IGNORE_TOKENS:
            dir_idx.setdefault(nm, []).append(rel)
        return has_audio

    def rec(d: str, depth: int) -> None:
        for e in listdir_sorted(d):
            p = os.path.join(d, e)
            try:
                if os.path.isfile(p):
                    if e.lower().endswith((".m3u", ".m3u8")):
                        add(m3u_idx, m3u_all, p)
                    elif e.lower().endswith(AUDIO_EXTS):
                        add(mp3_idx, mp3_all, p)
                elif depth < max_depth and os.path.isdir(p):
                    # Ordnernamen indexieren (Interpreten wie „Falco",
                    # „Beatles", …) — Namen holen ist ein listdir, kein walk.
                    _index_dir(p)
                    rec(p, depth + 1)
            except OSError:
                continue

    entries = listdir_sorted(scan_root)
    # Pass 1: alle Level-1-Dateien (schnell, garantiert)
    for e in entries:
        p = os.path.join(scan_root, e)
        try:
            if os.path.isfile(p):
                if e.lower().endswith((".m3u", ".m3u8")):
                    add(m3u_idx, m3u_all, p)
                elif e.lower().endswith(AUDIO_EXTS):
                    add(mp3_idx, mp3_all, p)
        except OSError:
            continue
    # Pass 2: Unterverzeichnisse bis max_depth (komplett, kein Zeitbudget)
    rec(scan_root, 0)
    if on_progress is not None:
        try:
            on_progress(seen[0])
        except Exception:
            pass
    # De-duplizierung (Pass 1 + rec überlappen sich bei Level-1-Dateien)
    m3u_all = list(dict.fromkeys(m3u_all))
    mp3_all = list(dict.fromkeys(mp3_all))
    for k in dir_idx:
        dir_idx[k] = list(dict.fromkeys(dir_idx[k]))
    return m3u_idx, m3u_all, mp3_idx, mp3_all, dir_idx


def _index_cache_path() -> str:
    d = config.DATA_DIR
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "media_index.json")


def _save_index(m3u_idx, m3u_all, mp3_idx, mp3_all, dir_idx) -> None:
    """Index atomar auf der Platte persistieren, damit der Container-Start
    mit einem warmen Index startet (kein CIFS-Scan) – atomic via rename."""
    import json as _json
    path = _index_cache_path()
    payload = {"ts": time.time(),
               "m3u_idx": m3u_idx, "m3u_all": m3u_all,
               "mp3_idx": mp3_idx, "mp3_all": mp3_all,
               "dir_idx": dir_idx}
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("intent: Index-Persist fehlgeschlagen: %s", e)


def _load_index_cache() -> bool:
    """Persistierten Index laden. True, wenn ein Index geladen wurde und der
    erste CIFS-Rebuild ein volles Intervall warten darf."""
    import json as _json
    global _m3u_index, _m3u_all, _mp3_index, _dir_index
    path = _index_cache_path()
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = _json.load(f)
        mp3_count = len(payload.get("mp3_all") or [])
        with _lock:
            _m3u_index = payload.get("m3u_idx") or {}
            _m3u_all = payload.get("m3u_all") or []
            _mp3_index = payload.get("mp3_idx") or {}
            _dir_index = payload.get("dir_idx") or {}
        log.info("intent: Medien-Index aus Cache geladen — %d Playlists, %d Audios (Alter: %.0f min)",
                 len(_m3u_all or []), mp3_count,
                 (time.time() - payload.get("ts", time.time())) / 60.0)
        return True
    except (OSError, ValueError) as e:
        log.warning("intent: Index-Cache nicht lesbar, wird neu gebaut: %s", e)
        return False


def reindex_now() -> None:
    """Manueller Reindex (WebUI-Button / „Neu indizieren"): der Indexer
    wachst aus der Intervall-Wartung auf und baut sofort um."""
    _index_build_evt.set()


def start_m3u_indexer(root: str, interval: int | None = None) -> None:
    """Start background thread: build media index, refresh every `interval` s.

    Indexiert Playlists (.m3u) UND Audiodateien im Hintergrund, damit
    'spiel <name>' ohne CIFS-Traversal in O(1) auflösbar ist. Der fertige
    Index wird unter DATA_DIR persistiert: beim Container-Start wird er
    geladen (kein CIFS-Scan), ein neuer Scan läuft erst nach `interval`
    Sekunden oder nach einem manuellen Reindex (reindex_now).
    Threaded (not asyncio) – die App läuft ohne Event-Loop.
    """
    global _indexer_started
    if interval is None:
        interval = _INDEX_REBUILD_INTERVAL
    if _indexer_started:
        return
    _indexer_started = True
    _load_index_cache()

    def _progress(n: int) -> None:
        global _index_files_seen
        _index_files_seen = n

    def _worker():
        global _m3u_index, _m3u_all, _mp3_index, _dir_index, _index_building, _index_last_build
        # Warm Cache? -> erst nach einem vollen Intervall neu scannen;
        # sonst (erster Start ohne Cache) sofort bauen.
        with _lock:
            ready = _m3u_index is not None or _mp3_index is not None
        while True:
            _index_build_evt.wait(interval if ready else 0)
            _index_build_evt.clear()
            ready = True
            try:
                _index_building = True
                _index_files_seen = 0
                log.info("intent: Medien-Index wird gebaut …")
                t0 = time.time()
                m3u_idx, m3u_all, mp3_idx, mp3_all, dir_idx = _build_media_index(
                    root, on_progress=_progress)
                _save_index(m3u_idx, m3u_all, mp3_idx, mp3_all, dir_idx)
                with _lock:
                    _m3u_index = m3u_idx
                    _m3u_all = m3u_all
                    _mp3_index = mp3_idx
                    _dir_index = dir_idx
                _index_last_build = (time.time(), len(m3u_all), len(mp3_all))
                log.info("intent: Medien-Index bereit — %d Playlists, %d Audios, %d Interpreten, %.1f s",
                         len(m3u_all), len(mp3_all), len(dir_idx), time.time() - t0)
            except Exception:
                log.exception("intent: Medien-Index-Refresh fehlgeschlagen")
            finally:
                _index_building = False

    threading.Thread(target=_worker, name="media-indexer", daemon=True).start()


def _find_in_index(idx: dict[str, list[str]] | None, name: str, root: str,
                   exts: tuple[str, ...], min_score: int = 1) -> str | None:
    """Suche in einem In-Memory-Index. Gibt relativen Pfad zurück oder None.

    Scoring statt First-Match: ein Treffer ist (a) exakter Schlüssel-Abgleich
    oder (b) jeder Such-Token, der in einem Schlüssel vorkommt. Der Schlüssel
    mit den meisten Treffern gewinnt; bei < min_score wird nichts zurückgeben
    (verhindert, dass ein einzelnes Wort wie „falco" auf „falcos" oder
    „filmmusik" landet)."""
    if idx:
        n = name.lower().strip()
        matches = idx.get(n)
        if matches:
            return matches[0]
        # Token-Scoring: zählt pro Schlüssel, wie viele Such-Token enthalten sind.
        q_tokens = [t for t in n.replace("_", " ").split() if len(t) > 2]
        if not q_tokens:
            return None
        best_score = 0
        best_paths: list[str] | None = None
        for k, paths in idx.items():
            score = 0
            for qt in q_tokens:
                if qt in k:
                    score += 1
            if score > best_score:
                best_score = score
                best_paths = paths
        if best_paths and best_score >= max(1, min_score):
            return best_paths[0]
    # Fallback: direct file check (instant, no walk)
    full = os.path.join(root, name)
    for ext in exts:
        if os.path.isfile(full + ext):
            return os.path.relpath(full + ext, root)
    return None


def _find_m3u_in_root(name: str, root: str) -> str | None:
    """Search root (and subdirs) for M3U/M3U8 file matching *name*."""
    return _find_in_index(_m3u_index, name, root, (".m3u", ".m3u8"))


def _find_mp3_in_root(name: str, root: str) -> str | None:
    """Suche Audiodatei *name* im mp3-Baum über den In-Memory-Index.
    Vermeidet den langsamen os.walk über CIFS bei jedem Request."""
    mp3_dir = os.path.join(root, "mp3")
    return _find_in_index(_mp3_index, name, mp3_dir,
                          (".mp3", ".flac", ".ogg", ".wav", ".m4a", ".opus"))


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

# M3U-Dateien mit Radio-/TV-Streams. Der Player spielt über die Pi-Bridge
# (RTSP→HTTP), alle drei Listen kommen in den Index. radio.m3u zuerst, da es
# die Fritzbox-Standardsender (192.168.81.1, RTSP) enthält.
_M3U_MOUNTS = ("radio.m3u", "world.m3u", "tv.m3u")


def _load_radio_stations() -> list[dict]:
    """Lädt alle drei M3U-Dateien aus /m/TV und gibt [{name, url}] zurück.

    Radio- und TV-Streams (RTSP/HTTP) aus radio.m3u, world.m3u, tv.m3u
    werden vereinigt. Bei gleicher Station (gleicher Name+URL) gewinnt die
    erste Datei (radio.m3u vor world.m3u vor tv.m3u). Ge-Cached nach den
    mtime-Werten aller Dateien.
    """
    tv_dir = os.path.join(config.MUSIC_ROOT, "TV")
    mtimes = {}
    for fname in _M3U_MOUNTS:
        p = os.path.join(tv_dir, fname)
        try:
            mtimes[fname] = os.path.getmtime(p)
        except OSError:
            mtimes[fname] = None

    cache_key = tuple(sorted(mtimes.items()))
    with _radio_lock:
        if _radio_cache and _radio_cache.get("key") == cache_key:
            return _radio_cache["stations"]

    stations: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for fname in _M3U_MOUNTS:
        p = os.path.join(tv_dir, fname)
        if mtimes.get(fname) is None:
            continue
        try:
            with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
                raw = f.read()
        except OSError:
            continue
        current_name = None
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("#EXTINF:"):
                if "," in line:
                    current_name = line.split(",", 1)[1].strip()
            elif line.startswith(("rtsp://", "http://", "https://")) and current_name:
                key = (current_name, line)
                if key not in seen:
                    seen.add(key)
                    stations.append({"name": current_name, "url": line,
                                     "file": fname})
                current_name = None

    with _radio_lock:
        _radio_cache["key"] = cache_key
        _radio_cache["stations"] = stations

    log.info("intent: Radio-Index: %d Sender aus %s",
             len(stations), ", ".join(f for f in _M3U_MOUNTS))
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

def try_play_music(text: str) -> tuple[bool, str | None]:
    """Allgemeine Musik-Befehle: „Musik an/abspielen", „Spiele Musik",
    „Mach Musik an", „Musik aus", „Kannst du Musik machen?" etc.

    Returns (True, response) oder (False, None).
    """
    t = text.lower().strip()

    # --- Stopp ---
    if re.search(r"\bmusik\b", t) and re.search(r"\b(aus|stopp|stop|stoppe)\b", t):
        stopped = playback.player.stop()
        if stopped:
            return True, "Musik gestoppt."
        return True, "Es lief gerade keine Musik."

    # --- Start (allgemein, ohne spezifischen Titel) ---
    # Muster: "Musik an", "Musik abspielen", "Spiele Musik", "Mach Musik an",
    # "Kannst du Musik machen?", "Mach mal Musik", "Spiele bitte Musik"
    if re.search(r"\bmusik\b", t):
        if re.search(r"\b(aus|stumm|stopp|stop|stoppe)\b", t):
            return False, None  # wurde oben schon abgefangen
        if re.search(r"\b(an|abspielen|spiele|Mach|mach|starte|kannst du|magst du|könntest du|würdest du|bitte)\b", t):
            # Suche nach M3U-Playlists im mp3-Verzeichnis
            mp3_dir = os.path.join(config.MUSIC_ROOT, "mp3")
            if not os.path.isdir(mp3_dir):
                return True, "MP3-Ordner nicht erreichbar."
            playlists: list[str] = []
            with _lock:
                if _m3u_all is not None:
                    playlists = list(_m3u_all)
            if not playlists:
                # Index noch nicht fertig (Startphase) → einmaliger Scan
                for root_dir, _dirs, files in os.walk(mp3_dir):
                    for f in files:
                        if f.lower().endswith((".m3u", ".m3u8")):
                            playlists.append(
                                os.path.relpath(os.path.join(root_dir, f), config.MUSIC_ROOT)
                            )
            if playlists:
                import random as _rand
                selected = _rand.choice(playlists)
                err = playback.player.play_path(selected)
                if err:
                    return True, err
                title = os.path.splitext(os.path.basename(selected))[0]
                return True, f"Musik wird abgespielt ({title})."
            return True, "Keine Musik gefunden."
        return False, None

    return False, None


# ---------------------------------------------------------------------------
# 5. Next / Prev Track (vorwärts / rückwärts)
# ---------------------------------------------------------------------------

def try_next_prev(text: str) -> tuple[bool, str | None]:
    """Match „vorwärts“, „rückwärts“, „nächster“, „vorheriger“ Titel.

    Returns (True, „Titel XYZ wird abgespielt“) or (False, None).
    """
    t = text.lower().strip()
    if t in ("vorwärts", "vorwarts", "naechster", "nächster", "weiter", "skip"):
        result = playback.player.next_track()
        return True, result
    if t in ("rückwärts", "rückwerts", "zurück", "zurueck", "vorheriger", "voreheriger", "back"):
        result = playback.player.prev_track()
        return True, result
    return False, None


# ---------------------------------------------------------------------------
# 6. Shuffle Mode (zufallswiedergabe)
# ---------------------------------------------------------------------------

def try_shuffle(text: str) -> tuple[bool, str | None]:
    """Match „zufallswiedergabe playlist xyz" oder „shuffle playlist xyz".

    Startet Playlist in der Zufallswiedergabe.
    Returns (True, „Playlist XYZ wird im Zufallsmodus abgespielt") or (False, None).
    """
    t = text.lower().strip()

    # Pattern: "zufallswiedergabe playlist xyz" oder "zufallswiedergabe xyz"
    m = re.match(r"^zufalls?wiedergabe\s+(?:playlist\s+)?(.+)", t)
    if not m:
        return False, None

    playlist_name = m.group(1).strip()
    if not playlist_name:
        return True, "Welche Playlist soll im Zufallsmodus abgespielt werden?"

    root = config.MUSIC_ROOT
    mp3_dir = os.path.join(root, "mp3")
    if not os.path.isdir(mp3_dir):
        mp3_dir = root  # Fallback

    # Suche die Playlist
    rel = _find_m3u_in_root(playlist_name, root)
    if not rel:
        for pat in (f"*{playlist_name}*.m3u", f"*{playlist_name}*.m3u8"):
            for f in glob.glob(os.path.join(mp3_dir, pat), recursive=False):
                if os.path.isfile(f):
                    rel = os.path.relpath(f, root)
                    break
            if rel:
                break

    if not rel:
        return True, f"Playlist '{playlist_name}' nicht gefunden"

    # Playlist öffnen, Shuffle aktivieren, abspielen
    err = playback.player.play_path(rel)
    if err:
        return True, err
    playback.player.enable_shuffle()
    title = os.path.splitext(os.path.basename(rel))[0]
    return True, f"Playlist '{title}' wird im Zufallsmodus abgespielt"


# ---------------------------------------------------------------------------
# 7. Resume last playlist (weiter) - LOCAL, no LLM
# ---------------------------------------------------------------------------

def try_resume(text: str) -> tuple[bool, str | None]:
    """Match „weiter", „fortsetzen", „fortsetzung", „letztes".

    Setzt die letzte Playlist fort - OHNE LLM-Abfrage.
    Returns (True, „Playlist XYZ wird fortgesetzt") or (False, None).
    """
    t = text.lower().strip()
    if t not in ("weiter", "fortsetzen", "fortsetzung", "letztes", "resume", "next", "more"):
        return False, None

    # Versuche zuerst die gemerkte Playlist
    last = playback.player.get_last_playlist()
    if last:
        result = playback.player.resume_last_playlist()
        return True, result

    # Fallback: versuche über den Status, ob eine Playlist pausiert ist
    status = playback.player.status()
    if status.get("paused") or status.get("playing"):
        return True, f"Aktuelle Wiedergabe: {status.get('title', 'Unbekannt')}"

    return True, "Keine vorherige Wiedergabe gefunden"


# ---------------------------------------------------------------------------
# 7b. Reindex (Neu indizieren) - LOCAL, no LLM
# ---------------------------------------------------------------------------

_REINDEX_RE = re.compile(
    r"^(?:neu\s+indizieren|indizier\s*neu|reindex(?:\s+jetzt)?)\s*$")


def try_reindex(text: str) -> tuple[bool, str | None]:
    """Match „Neu indizieren" / „Reindex" – Medien-Index sofort neu bauen.

    Lokal, OHNE LLM. Triggert den Indexer-Thread (reindex_now), der sofort
    (nicht erst nach dem Intervall) den CIFS-Scan startet.
    Returns (True, „Neu indizieren …") or (False, None).
    """
    t = text.lower().strip().rstrip("!")
    if not _REINDEX_RE.match(t):
        return False, None
    reindex_now()
    return True, "Neu indizieren – der Medien-Index wird jetzt aktualisiert."


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

    # „musik von X" / „die musik von X" → nur der Interpret X ist der Suchbegriff
    artist_match = re.search(r"musik\s+(?:von|by)\s+(.+)", name)
    if artist_match:
        name = artist_match.group(1).strip()
    if not name:
        return True, "Welchen Interpreten suchst du?"

    root = config.MUSIC_ROOT
    mp3_dir = os.path.join(root, "mp3")
    if not os.path.isdir(mp3_dir):
        return True, f"MP3-Ordner nicht erreichbar: {mp3_dir}"

    # 1. Exakte Datei direkt in mp3/ (case-insensitive: Dateinamen auf
    #    CIFS/Windows-Sharen sind oft gemischt, „Falco - 191.mp3" soll auch
    #    bei „falco - 191" gefunden werden).
    for ext in (".m3u", ".m3u8", ".mp3"):
        fp = os.path.join(mp3_dir, name + ext)
        if os.path.isfile(fp):
            rel = os.path.relpath(fp, root)
            err = playback.player.play_path(rel)
            if err:
                return True, err
            return True, f"'{name}{ext}' wird abgespielt"

    # 1b. Case-insensitive Direktfile-Check in mp3/ und mp3/*/ (eine Ebene
    #     tiefer) — ein listdir statt eines langsamen Walk. Explizit
    #     case-insensitive vergleichen (funktioniert auf ext4 UND CIFS;
    #     glob ist auf ext4 case-sensitive, würde nur auf Windows-Sharen
    #     durchgehen).
    name_l = name.lower()
    audio_exts = (".m3u", ".m3u8", ".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac")
    def _ci_dir_match(d: str) -> tuple[str | None, str | None]:
        """(pfad, base) des Datei mit base == name_l (case-insens) in d."""
        try:
            entries = os.listdir(d)
        except OSError:
            return None, None
        for e in entries:
            ext = os.path.splitext(e)[1].lower()
            if ext not in audio_exts:
                continue
            if not os.path.isfile(os.path.join(d, e)):
                continue
            base = os.path.splitext(e)[0].lower()
            if base == name_l:
                return os.path.join(d, e), base
        return None, None
    for sub in ("", "/*"):
        if sub == "/*":
            # eine Ebene tiefer: Unterordner von mp3/
            try:
                subs = [os.path.join(mp3_dir, s) for s in os.listdir(mp3_dir)]
            except OSError:
                subs = []
            subs = [s for s in subs if os.path.isdir(s)]
        else:
            subs = [mp3_dir]
        for sd in subs:
            found, _base = _ci_dir_match(sd)
            if found:
                rel = os.path.relpath(found, root)
                err = playback.player.play_path(rel)
                if err:
                    return True, err
                return True, f"'{os.path.splitext(os.path.basename(found))[0]}' wird abgespielt"

    # 2. Ordner-Match über den In-Memory-Dir-Index (z.B. „Falco" → Interpreten/E/Falco).
    #    Scoring: Ordner mit den meisten Such-Token-Treffern gewinnt; bei Gleichstand
    #    der längste Name (spezifischste Ebene, z.B. „falco" statt „music").
    with _lock:
        d_idx = dict(_dir_index) if _dir_index else {}
    if d_idx:
        q = [t for t in name.lower().split() if len(t) > 2]
        best: tuple[int, int, str] | None = None  # (score, keylen, path)
        for k, paths in d_idx.items():
            score = sum(1 for qt in q if qt in k) + (1 if k in name.lower() else 0)
            if score <= 0:
                continue
            cand = (score, len(k), paths[0])
            if best is None or (cand[0], cand[1]) > (best[0], best[1]):
                best = cand
        if best is not None:
            err = playback.player.play_path(best[2])
            if err:
                return True, err
            return True, f"Ordner '{os.path.basename(best[2])}' wird abgespielt"
    # 2b. Direkt: mp3_dir/name ist ein Ordner (exakter Name, ohne Unterordner)
    dir_path = os.path.join(mp3_dir, name)
    if os.path.isdir(dir_path):
        rel = os.path.relpath(dir_path, root)
        err = playback.player.play_path(rel)
        if err:
            return True, err
        return True, f"Ordner '{name}' wird abgespielt"

    # 3. Audiodatei (MP3/FLAC/...) über den In-Memory-Index —
    #    O(1) statt langsamem os.walk über CIFS bei jedem Request.
    rel = _find_mp3_in_root(name, root)
    if rel:
        err = playback.player.play_path(rel)
        if err:
            return True, err
        return True, f"'{os.path.basename(rel)}' wird abgespielt"

    # 4. Playlist match via M3U-Index — zuletzt, damit „musik von X"
    #    (Interpret-Suche) nicht auf Playlists wie „Filmmusik" landet.
    rel = _find_m3u_in_root(name, root)
    if rel:
        err = playback.player.play_path(rel)
        if err:
            return True, err
        return True, f"Playlist '{os.path.splitext(os.path.basename(rel))[0]}' wird abgespielt"

    # 5. Index noch nicht fertig (Startphase) → einmaliger, begrenzter Glob
    for pat in (f"*{name}*.mp3", f"*{name}*.flac", f"*{name}*.m4a", f"*{name}*.ogg"):
        for f in glob.glob(os.path.join(mp3_dir, pat), recursive=False):
            if os.path.isfile(f):
                rel = os.path.relpath(f, root)
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
    """Match Kamera-Befehle: „zeige Kamera X", "show camera X",
    „Was ist in Kamera X los?", „Kamera X zeigen", „Zeig mir Kamera X" etc.

    Returns (True, „Kamera X wird angezeigt") or (False, None).
    """
    t = text.lower().strip()

    # Erweiterte Patterns für natürliche Formulierungen
    # 1. "Zeige/Zeig Kamera X" / "show camera X"
    m = re.match(r"^(?:zeige|zeig|show)\s+(?:die\s+)?(?:kamera|camera)\s+(.+)", t)
    if not m:
        # 2. "Was ist in Kamera X los?" / "Was läuft bei Kamera X?" / "Kamera X Status"
        m = re.search(r"\b(?:was\s+ist|was\s+läuft|was\s+gibt's|was\s+gibts)\s+(?:in|an|bei|auf)\s+(?:die\s+)?(?:kamera|camera)\s+(\w+)", t)
    if not m:
        # 3. "Kamera X" + Status-Keywords
        m = re.search(r"\b(?:kamera|camera)\s+(\w+)\s*(?:los|status|bild|anzeige|zeigen|check|schau|an)\b", t)
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
