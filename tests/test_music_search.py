#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Musik-/Radio-Suche des Masters (master/music_search.py).

Prueft Paritaet zur Pi-Suche (_fuzzy_match) und die API-Form:
  * radio: exact/substring-Fuzzy, Quell-Pfad "TV/<file>", Dedupe, leeres q
  * music: exakter Titel, Token-Queue (Interpret -> alle Tracks),
           Ordner-Match, Zufall bei leerem q, leerer Index -> leeres Ergebnis

Aufruf:  python3 tests/test_music_search.py   (nur stdlib noetig)
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(cond, msg):
    if cond:
        print(f"ok   — {msg}")
    else:
        print(f"FAIL — {msg}")
        fails.append(msg)


def main() -> int:
    from master import config as master_cfg
    from master import music_search

    # ── Radio: M3U unter <MUSIC_ROOT>/TV ──
    tmp = tempfile.mkdtemp(prefix="vssearch-")
    orig_root = master_cfg.MUSIC_ROOT
    master_cfg.MUSIC_ROOT = tmp
    tv = os.path.join(tmp, "TV")
    os.makedirs(tv)
    with open(os.path.join(tv, "radio.m3u"), "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n"
                "#EXTINF:-1,1LIVE\nrtsp://x/a\n"
                "#EXTINF:-1,WDR 2\nrtsp://x/b\n"
                "#EXTINF:-1,1LIVE (Diggi)\nrtsp://x/c\n"
                "#EXTINF:-1,WDR 1 Live (720p)\nhttps://x/d.m3u8\n")
    with open(os.path.join(tv, "world.m3u"), "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n#EXTINF:-1,00s Replay\nhttps://y/z.m3u8\n")
    music_search._radio_cache["key"] = None  # Cache neu aufbauen

    r = music_search.search_radio("1LIVE")
    check(r["kind"] == "radio" and r["title"] == "1LIVE"
          and r["results"][0]["source"] == "TV/radio.m3u",
          "radio exact match -> title + TV/radio.m3u")
    check(any(x["title"] == "1LIVE (Diggi)" for x in r["results"]),
          "radio: nahe Variante in results")
    r = music_search.search_radio("wdr 2")
    check(r["title"] == "WDR 2", "radio: Wort-fuer-Wort-Match")
    r = music_search.search_radio("replay")
    check(r["title"] == "00s Replay"
          and r["results"][0]["source"] == "TV/world.m3u",
          "radio: world.m3u Quelle")
    r = music_search.search_radio("1 live")
    check(r["title"] == "1LIVE", "radio: STT-Zerstoer '1 live' -> 1LIVE")
    check(any(x["title"] == "1LIVE (Diggi)" for x in r["results"]),
          "radio: compact-nahe Variante in results ('1 live')")
    r = music_search.search_radio("1live diggi")
    check(r["title"] == "1LIVE (Diggi)", "radio: '1live diggi' -> Variante")
    r = music_search.search_radio("wdr2")
    check(r["title"] == "WDR 2", "radio: 'wdr2' -> WDR 2")
    r = music_search.search_radio("gibtsnicht")
    check(r["title"] is None and not r["results"], "radio: kein Match -> leer")
    r = music_search.search_radio("")
    check(r["title"] in ("1LIVE", "WDR 2", "1LIVE (Diggi)", "00s Replay"),
          "radio: leeres q -> zufaelliger Sender")

    # ── Musik: synthetischer Index ──
    data = {
        "mp3_all": [
            "mp3/Interpreten/F/Falco/Falco - Rock Me Amadeus.mp3",
            "mp3/Interpreten/F/Falco/Falco - Jeanny.mp3",
            "mp3/Interpreten/Q/Queen/Queen - Bohemian Rhapsody.mp3",
            "mp3/Sonstiges/Best Of 2020.mp3",
        ],
        "mp3_idx": {
            "falco": ["mp3/Interpreten/F/Falco/Falco - Rock Me Amadeus.mp3",
                      "mp3/Interpreten/F/Falco/Falco - Jeanny.mp3"],
            "rock": ["mp3/Interpreten/F/Falco/Falco - Rock Me Amadeus.mp3"],
            "amadeus": ["mp3/Interpreten/F/Falco/Falco - Rock Me Amadeus.mp3"],
            "jeanny": ["mp3/Interpreten/F/Falco/Falco - Jeanny.mp3"],
            "queen": ["mp3/Interpreten/Q/Queen/Queen - Bohemian Rhapsody.mp3"],
            "bohemian": ["mp3/Interpreten/Q/Queen/Queen - Bohemian Rhapsody.mp3"],
            "best": ["mp3/Sonstiges/Best Of 2020.mp3"],
            "of": ["mp3/Sonstiges/Best Of 2020.mp3"],
            "2020": ["mp3/Sonstiges/Best Of 2020.mp3"],
        },
        "dir_idx": {
            "falco": ["mp3/Interpreten/F/Falco"],
            "queen": ["mp3/Interpreten/Q/Queen"],
            "sonstiges": ["mp3/Sonstiges"],
        },
        "m3u_all": [], "m3u_idx": {},
    }

    r = music_search.search_music("falco jeanny", data)
    check(r["kind"] == "music" and r["title"] == "Falco - Jeanny"
          and r["queue"] == ["mp3/Interpreten/F/Falco/Falco - Jeanny.mp3"],
          "musik: Titel-Match via Token-Intersection")

    r = music_search.search_music("rock me amadeus", data)
    check(r["title"] == "Falco - Rock Me Amadeus"
          and r["queue"] == ["mp3/Interpreten/F/Falco/"
                             "Falco - Rock Me Amadeus.mp3"],
          "musik: Wort-in-Reihenfolge-Match")

    r = music_search.search_music("queen", data)
    check(r["queue"] == ["mp3/Interpreten/Q/Queen/"
                         "Queen - Bohemian Rhapsody.mp3"],
          "musik: Kuenstler-Token -> alle Tracks als Queue")

    r = music_search.search_music("best of 2020", data)
    check(r["title"] == "Best Of 2020", "musik: Mehrwort-Titel")

    r = music_search.search_music("", data)
    check(r["title"] and r["queue"], "musik: leeres q -> Zufallstitel")

    r = music_search.search_music("irgendwasunbekanntes", data)
    check(r["title"] and r["queue"],
          "musik: Unbekannt -> Zufalls-Fallback (wie 'Musik an')")

    r = music_search.search_music("falco", data)
    # Falco-Tracks: Token-Treffer; Ordner-Queue greift nur wenn Token-Pfad leer
    check(r["title"].startswith("Falco") and len(r["queue"]) == 2,
          "musik: 'falco' -> beide Falco-Tracks")

    r = music_search.search_music("x", {"mp3_all": [], "mp3_idx": {},
                                        "dir_idx": {}})
    check(r["title"] is None and not r["queue"],
          "musik: leerer Index -> leeres Ergebnis")

    master_cfg.MUSIC_ROOT = orig_root
    print()
    if fails:
        print(f"{len(fails)} FAILURES")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
