"""Musik-/Radio-Suche für App und Satelliten (GET /api/music/search?q=&kind=).

Die Android-App (und später evtl. Satelliten ohne lokalen Index) fragt damit
den Master statt des mehrzig-MB-Index-Payloads:

  GET /api/music/search?q=<text>&kind=music|radio&limit=5

  kind=music  -> {"kind":"music","title":..., "results":[{title,relpath}],
                  "queue":[relpath, ...]}
  kind=radio  -> {"kind":"radio","title":..., "results":[{title,source}],
                  "queue":[]}

'queue' ist die komplette Wiedergabeliste in der Reihenfolge, in der die App
spielen soll (Künstler-Ordner = alle Tracks, einzelner Titel = nur der).
'source' bei Radio ist der M3U-Relativpfad (z.B. "TV/radio.m3u") — dieselbe
Konvention wie der Pi (playback.play_proxy_rtsp).

Suchlogik 1:1 kompatibel zum Pi (app/intent.py _fuzzy_match):
exact -> starts-with -> substring -> alle Wörter der Reihe nach.
"""
from __future__ import annotations

import logging
import os
import random
import re
import threading
import time

from . import config as master_cfg
from .indexer import AUDIO_EXTS

log = logging.getLogger("master.music_search")

_M3U_MOUNTS = ("radio.m3u", "world.m3u", "tv.m3u")
_radio_cache: dict = {"key": None, "stations": []}
_radio_lock = threading.Lock()


# ── Radio (M3U-Listen unter <MUSIC_ROOT>/TV) ─────────────────────────


def _load_radio_stations() -> list[dict]:
    """Alle drei M3U-Dateien parsen -> [{name, url, file}].

    Gleiche Station (Name+URL): erste Datei gewinnt (radio.m3u vor
    world.m3u vor tv.m3u). Cache nach mtime (bzw. 10 min wenn kein mtime).
    """
    tv_dir = os.path.join(master_cfg.MUSIC_ROOT, "TV")
    mtimes = {}
    for fname in _M3U_MOUNTS:
        p = os.path.join(tv_dir, fname)
        try:
            mtimes[fname] = os.path.getmtime(p)
        except OSError:
            mtimes[fname] = None
    cache_key = ("mtime",) if all(v is None for v in mtimes.values()) else \
        tuple(sorted((k, v) for k, v in mtimes.items() if v is not None))
    if all(v is None for v in mtimes.values()):
        cache_key = ("time", int(time.time() // 600))
    with _radio_lock:
        if _radio_cache.get("key") == cache_key:
            return _radio_cache["stations"]

    stations: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for fname in _M3U_MOUNTS:
        p = os.path.join(tv_dir, fname)
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
    log.info("music_search: Radio-Index: %d Sender aus %s",
             len(stations), ", ".join(_M3U_MOUNTS))
    return stations


# ── Fuzzy (Parität Pi app/intent.py _fuzzy_match) ────────────────────


def _norm(q: str) -> str:
    s = (q or "").lower().replace("'", "")
    s = s.replace("ä", "a").replace("ö", "o").replace("ü", "u")
    return s.strip()


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _norm(s))


def _fuzzy_match(query: str, candidates: list[str]) -> str | None:
    q = _norm(query)
    if not q:
        return None
    for c in candidates:
        if c.lower() == q:
            return c
    for c in candidates:
        if c.lower().startswith(q):
            return c
    for c in candidates:
        if q in c.lower():
            return c
    # Compact-Ebene: STT schreibt Sender gern zusammen oder ausein-
    # ander ("1LIVE" <-> "1 live", "WDR3" <-> "WDR 3") — Interpunktion
    # und Leerstellen beiderseits ignorieren.
    qc = _compact(q)
    if qc:
        for c in candidates:
            if _compact(c) == qc:
                return c
        for c in candidates:
            if qc in _compact(c):
                return c
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
            if len(indices) == len(qwords) and \
                    all(i <= j for i, j in zip(indices, indices[1:])):
                return c
    return None


def search_radio(q: str, limit: int = 5) -> dict:
    """Radio-Suche; leeres q = beliebiger Sender (wie 'musik an' beim Pi)."""
    stations = _load_radio_stations()
    if not stations:
        return {"kind": "radio", "title": None, "results": [], "queue": []}
    names = [s["name"] for s in stations]
    if not (q or "").strip():
        station = random.choice(stations)
        matched = station["name"]
    else:
        qc = _compact(q)
        matched = _fuzzy_match(q, names)
        if not matched:
            return {"kind": "radio", "title": None, "results": [],
                    "queue": []}
        station = next(s for s in stations if s["name"] == matched)
    results = [{"title": station["name"],
                "source": f"TV/{station['file']}"}]
    if (q or "").strip():
        # Weitere nahe Treffer (gleiche Schreibweise-Varianten)
        seen_names = {station["name"].lower()}
        for s in stations:
            if len(results) >= limit:
                break
            if s["name"].lower() in seen_names:
                continue
            sn = s["name"].lower()
            if _norm(q) in sn or (qc and qc in _compact(s["name"])):
                results.append({"title": s["name"],
                                "source": f"TV/{s['file']}"})
                seen_names.add(s["name"].lower())
    return {"kind": "radio", "title": station["name"],
            "results": results, "queue": []}


# ── Musik (Index-Payload des Master-Indexers) ─────────────────────────


def _stem(p: str) -> str:
    base = os.path.basename(p)
    return base.rsplit(".", 1)[0] if "." in base else base


def _dir_audio_files(dir_rel: str) -> list[str]:
    """Alle Audiodateien (rekursiv, sortiert) unter einem rel. Ordner."""
    root = master_cfg.MUSIC_ROOT.rstrip("/")
    d = os.path.normpath(os.path.join(root, dir_rel))
    if d != root and not d.startswith(root + os.sep):
        return []
    out: list[str] = []
    stack = [d]
    while stack:
        cur = stack.pop(0)
        try:
            entries = sorted(os.scandir(cur), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir():
                    stack.append(e.path)
                elif e.name.lower().endswith(AUDIO_EXTS):
                    out.append(os.path.relpath(e.path, root))
            except OSError:
                continue
    return out


def search_music(q: str, data: dict, limit: int = 5) -> dict:
    """Musik-Suche über den Master-Index.

    Priorität (wie Pi try_play_mp3, vereinfacht):
      1. exakter Titel (Basename == q)
      2. Token-Index-Match (mp3_idx): alle Treffer werden zur Queue
      3. Künstler-/Ordner-Match (dir_idx): Queue = alle Tracks im Ordner
      4. leeres q: zufälliger Titel
    """
    mp3_all = list(data.get("mp3_all") or [])
    if not mp3_all:
        return {"kind": "music", "title": None, "results": [], "queue": []}

    qn = _norm(q)
    stem_l = {p: _stem(p).lower() for p in mp3_all}

    # 1. exakter Titel
    exact = [p for p in mp3_all if stem_l[p] == qn] if qn else []
    if exact:
        exact.sort()
        return {"kind": "music", "title": _stem(exact[0]),
                "results": [{"title": _stem(p), "relpath": p}
                            for p in exact[:limit]],
                "queue": exact}

    if qn:
        words = [w for w in qn.split() if len(w) > 2] or qn.split()
        mp3_idx = data.get("mp3_idx") or {}
        cands: list[str] | None = None
        for w in words:
            hits = list(mp3_idx.get(w) or [])
            cands = hits if cands is None else [p for p in cands if p in set(hits)]
            if not cands:
                break
        if not cands:
            # Kein gemeinsames Token -> breitestes einzelnes Token
            for w in words:
                hits = mp3_idx.get(w) or []
                if hits:
                    cands = list(hits)
                    break
        if cands:
            if len(cands) > 200:
                # zu breit (u.a. ein-Wort-Allgemeinflut): nur noch
                # Start-mit-/enthält-Filter über die Basenames
                filtered = [p for p in cands
                            if qn in stem_l[p] or stem_l[p].startswith(qn)]
                if filtered:
                    cands = filtered
            cands.sort()
            results = [{"title": _stem(p), "relpath": p}
                       for p in cands[:limit]]
            if results:
                return {"kind": "music", "title": _stem(cands[0]),
                        "results": results, "queue": cands}

        # 3. Ordner/Interpret-Match
        dir_idx = data.get("dir_idx") or {}
        matched_dir = _fuzzy_match(qn, list(dir_idx.keys()))
        if matched_dir:
            for drel in (dir_idx.get(matched_dir) or []):
                files = _dir_audio_files(drel)
                if files:
                    return {"kind": "music", "title": matched_dir,
                            "results": [{"title": _stem(f), "relpath": f}
                                        for f in files[:limit]],
                            "queue": files}

    # 4. Fallback: zufälliger Titel
    pick = random.choice(mp3_all)
    return {"kind": "music", "title": _stem(pick),
            "results": [{"title": _stem(pick), "relpath": pick}],
            "queue": [pick]}
