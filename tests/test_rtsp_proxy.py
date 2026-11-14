#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RTSP-Proxy-Logik des Masters: M3U-Auflösung ohne ffmpeg/HTTP-Stack.

Prueft die Sicherheitsregeln des /proxy/rtsp-Ersatzes fuer die Pi-Bridge:
  * resolve_source: UTF-8- und UTF-16-M3U, EXTINF-Titelzuordnung,
    erste URL als Fallback, Traversal-Sperre, nur .m3u/.m3u8
  * Fehlermeldungen nennen NIE eine Strom-/rtsp-URL
  * _inject_credentials nur bei rtsp-URLs ohne '@'
  * Zensur (_sanitize) ueberall

Aufruf:  python3 tests/test_rtsp_proxy.py   (im Master-Image/oekosystem,
benoetigt starlette wie der Master selbst)
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
    from master import rtsp

    tmp = tempfile.mkdtemp(prefix="vsrtsp-")
    orig_root = master_cfg.MUSIC_ROOT
    master_cfg.MUSIC_ROOT = tmp
    try:
        # TV/radio.m3u (UTF-8, mehrere Sender mit EXTINF)
        os.makedirs(os.path.join(tmp, "TV"))
        with open(os.path.join(tmp, "TV", "radio.m3u"), "w",
                  encoding="utf-8") as f:
            f.write("#EXTM3U\n"
                    "#EXTINF:-1,FluxFM\nrtsp://user:pw@192.168.81.1/live/1\n"
                    "#EXTINF:-1,DRK\nrtsp://x:y@192.168.81.1/live/2\n")
        # Kamera/CAM_Test.m3u (UTF-16 — wie die echten Kamera-Dateien)
        os.makedirs(os.path.join(tmp, "Kamera"))
        with open(os.path.join(tmp, "Kamera", "CAM_Test.m3u"), "wb") as f:
            f.write("rtsp://cam:secret@192.168.82.15/ch0\n".encode("utf-16"))

        url, err = rtsp.resolve_source("TV/radio.m3u", "FluxFM")
        check(err is None and url == "rtsp://user:pw@192.168.81.1/live/1",
              "EXTINF-Titelwahl (FluxFM)")
        url, err = rtsp.resolve_source("TV/radio.m3u", "Unbekannt")
        check(err is None and url == "rtsp://user:pw@192.168.81.1/live/1",
              "Titel-Fallback: erste URL")
        url, err = rtsp.resolve_source("TV/radio.m3u")
        check(err is None and url.endswith("/live/1"), "ohne Titel: erste URL")

        url, err = rtsp.resolve_source("Kamera/CAM_Test.m3u")
        check(err is None and url == "rtsp://cam:secret@192.168.82.15/ch0",
              "UTF-16-M3U (Kamera)")

        url, err = rtsp.resolve_source("../../etc/passwd.m3u")
        check(url is None and err is not None, "Traversal gesperrt")

        url, err = rtsp.resolve_source("TV/geheim.txt")
        check(url is None and err == "nur M3U-Dateipfade erlaubt",
              "nur .m3u/.m3u8 als Quelle")

        url, err = rtsp.resolve_source("TV/missing.m3u")
        check(url is None and err is not None and "rtsp://" not in err,
              "fehlende Datei: Fehler ohne URL")

        # Credentials-Injektur nur bei rtsp ohne '@'
        os.environ["RTSP_USER"] = "u"
        os.environ["RTSP_PASS"] = "p"
        check(rtsp._inject_credentials("rtsp://1.2.3.4/live")
              == "rtsp://u:p@1.2.3.4/live", "Injektur rtsp ohne '@'")
        check(rtsp._inject_credentials("rtsp://a:b@1.2.3.4/live")
              == "rtsp://a:b@1.2.3.4/live", "Injektur ignoriert vorhandenes '@'")
        check(rtsp._inject_credentials("http://example.org/a.mp3")
              == "http://example.org/a.mp3", "http bleibt unangetastet")
        del os.environ["RTSP_USER"], os.environ["RTSP_PASS"]

        check(rtsp._sanitize("failed rtsp://user:pw@host/live x")
              == "failed rtsp://*** x", "Zensur in Logs")

        pairs = rtsp._m3u_pairs("#EXTINF:0,World\nhttp://a/b.mp3\n"
                                "#EXTINF:-1,TV\nrtsp://c/d\n")
        check(pairs == [("World", "http://a/b.mp3"), ("TV", "rtsp://c/d")],
              "_m3u_pairs mischt http+rtsp")
    finally:
        master_cfg.MUSIC_ROOT = orig_root

    print(f"\n{'FEHLER: ' + str(len(fails)) if fails else 'ALLE TESTS OK'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
