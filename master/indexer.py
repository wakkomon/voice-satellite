"""Master-Indexer: CIFS-Scan über /m (wie app/intent.py, aber standalone).

Gibt das fertige Payload zurück:
{
  "m3u_idx": {name_lower: [relpaths]},
  "m3u_all": [relpaths],
  "mp3_idx": {name_lower: [relpaths]},
  "mp3_all": [relpaths],
  "dir_idx": {name_lower: [relpaths]}
}

Verwendet dieselbe Scoring-/Token-Logik wie satellite intent.py —
damit sind Index und Suche 1:1 kompatibel.
"""
from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("master.indexer")

_IGNORE_TOKENS = frozenset({
    "von", "der", "die", "das", "den", "dem", "des", "ein", "eine",
    "einer", "eines", "am", "im", "um", "zu", "mit", "auf", "an",
    "in", "für", "fuer", "und", "oder", "ist", "sind", "war",
    "bei", "nach", "aus", "vor", "über", "ueber", "unter",
    "bis", "doch", "ich", "du", "er", "sie", "es", "mal",
})

AUDIO_EXTS = (".mp3", ".flac", ".ogg", ".wav", ".m4a", ".opus")


def _build_media_index(root: str, max_depth: int = 3,
                       on_progress=None) -> dict:
    """Scan root/mp3 (max_depth Ebenen) in EINEM Walk.

    Rückgabe: Payload dict, identisch zum satellite-intent.py Format.
    """
    m3u_idx: dict[str, list[str]] = {}
    m3u_all: list[str] = []
    mp3_idx: dict[str, list[str]] = {}
    mp3_all: list[str] = []
    dir_idx: dict[str, list[str]] = {}

    scan_root = os.path.join(root, "mp3")
    if not os.path.isdir(scan_root):
        return {
            "m3u_idx": m3u_idx, "m3u_all": m3u_all,
            "mp3_idx": mp3_idx, "mp3_all": mp3_all,
            "dir_idx": dir_idx,
        }

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
        meaningful = [t for t in tokens if t not in _IGNORE_TOKENS and len(t) > 2]
        for token in sorted(set(meaningful)):
            idx.setdefault(token, []).append(rel)
        if base.lower() not in meaningful and base.lower() not in _IGNORE_TOKENS:
            idx.setdefault(base.lower(), []).append(rel)

    def scandir_sorted(d: str) -> list:
        try:
            return sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return []

    def _index_dir(p: str, entries=None) -> bool:
        rel = os.path.relpath(p, root)
        nm = os.path.basename(p.rstrip(os.sep)).lower()
        has_audio = False
        if entries is None:
            entries = scandir_sorted(p)
        for e2 in entries:
            base = e2.name if hasattr(e2, "name") else str(e2)
            if base.lower().endswith(AUDIO_EXTS) and not e2.is_dir():
                has_audio = True
                break
        if nm and len(nm) > 1 and nm not in _IGNORE_TOKENS:
            dir_idx.setdefault(nm, []).append(rel)
        return has_audio

    def rec(d: str, depth: int) -> None:
        for e in scandir_sorted(d):
            try:
                if e.is_file():
                    base = e.name
                    if base.lower().endswith((".m3u", ".m3u8")):
                        add(m3u_idx, m3u_all, e.path)
                    elif base.lower().endswith(AUDIO_EXTS):
                        add(mp3_idx, mp3_all, e.path)
                elif depth < max_depth and e.is_dir():
                    _index_dir(e.path)
                    rec(e.path, depth + 1)
            except OSError:
                continue

    entries = scandir_sorted(scan_root)
    # Pass 1: alle Level-1-Dateien (schnell, garantiert)
    for e in entries:
        try:
            if e.is_file():
                base = e.name
                if base.lower().endswith((".m3u", ".m3u8")):
                    add(m3u_idx, m3u_all, e.path)
                elif base.lower().endswith(AUDIO_EXTS):
                    add(mp3_idx, mp3_all, e.path)
        except OSError:
            continue
    # Pass 2: Unterverzeichnisse bis max_depth (kein Zeitbudget)
    rec(scan_root, 0)
    if on_progress is not None:
        try:
            on_progress(seen[0])
        except Exception:
            pass
    # De-duplizierung
    m3u_all = list(dict.fromkeys(m3u_all))
    mp3_all = list(dict.fromkeys(mp3_all))
    for k in dir_idx:
        dir_idx[k] = list(dict.fromkeys(dir_idx[k]))

    return {
        "m3u_idx": m3u_idx, "m3u_all": m3u_all,
        "mp3_idx": mp3_idx, "mp3_all": mp3_all,
        "dir_idx": dir_idx,
    }


def save_index(payload: dict, cache_dir: str) -> None:
    """Payload atomar im Cache-Verzeichnis persistieren."""
    path = os.path.join(cache_dir, "media_index.json")
    os.makedirs(cache_dir, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), **payload}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("Index-Persist fehlgeschlagen: %s", e)


def load_index_cache(cache_dir: str) -> dict | None:
    """Persistierten Index laden. Gibt Payload zurück oder None."""
    path = os.path.join(cache_dir, "media_index.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
