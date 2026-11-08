"""App-Update-Verteilung: APK-Dateien fuer die Android-Apps.

Der Master legt APKs in der APPS-Ablage ab (Unterordner 'apps' des
Config-Verzeichnisses, persistent ueber das Data-Mount) und benennt sie
voice-satellite-app-<version>.apk. Meldet eine App in ihrem Heartbeat
ihre Version, vergleicht der Master und schickt ihr im Heartbeat-Ack ein
'update'-Feld (version/file/url/size). Die App zeigt den Hinweis an und
holt die Datei ueber GET /api/app/download?file=... .

Stdlib-only, lokal testbar (tests/test_app_update.py).
"""
from __future__ import annotations

import os
import re
from urllib.parse import quote

# Kanonischer APK-Name mit Version (versionName der App, z. B. 2.4.21).
APK_NAME_RE = re.compile(r"^voice-satellite-app-([0-9]+(?:\.[0-9]+)*)\.apk$")


def apps_dir() -> str:
    """APK-Ablage: ENV APPS_DIR, sonst <VS_CONFIG_DIR>/apps.

    ENV wird pro Aufruf gelesen (satconfig.CONFIG_DIR ist eine
    Import-Zeit-Konstante und fuer Tests unbrauchbar).
    """
    d = os.environ.get("APPS_DIR", "").strip()
    if not d:
        base = os.environ.get(
            "VS_CONFIG_DIR",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"),
        )
        d = os.path.join(base, "apps")
    return d


def parse_version(s) -> tuple:
    """'2.4.21' -> (2, 4, 21); nicht-numerische Teile entfallen, '' -> ()."""
    return tuple(int(p) for p in re.findall(r"\d+", str(s or "")))


def list_apks(base: str | None = None) -> list[dict]:
    """Alle APKs der Ablage, neueste Version zuerst."""
    base = base or apps_dir()
    out = []
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        m = APK_NAME_RE.match(name)
        if not m:
            continue
        path = os.path.join(base, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({"version": m.group(1), "file": name,
                    "size": st.st_size, "mtime": int(st.st_mtime)})
    out.sort(key=lambda a: (parse_version(a["version"]), a["mtime"]),
             reverse=True)
    return out


def latest(base: str | None = None) -> dict | None:
    apks = list_apks(base)
    return apks[0] if apks else None


def update_hint(reported, kind: str = "",
                base: str | None = None) -> dict | None:
    """Update-Hinweis fuer den Heartbeat-Ack.

    Nur Android-Geraete (kind == 'android') und nur wenn die APK in der
    Ablage strikt hoeher ist als die gemeldete versionName. Pi-Satelliten
    melden eine andere Software-Version und bekommen nie einen Hinweis.
    """
    if (kind or "").lower() != "android":
        return None
    rep = parse_version(reported)
    if not rep:
        return None
    top = latest(base)
    if not top or parse_version(top["version"]) <= rep:
        return None
    return {"version": top["version"], "file": top["file"],
            "url": "/api/app/download?file=" + quote(top["file"]),
            "size": top["size"]}


def resolve_apk(name, base: str | None = None) -> str | None:
    """Download-Dateiname sicher aufloesen: nur basename, nur das
    kanonische Namensmuster — Pfad-Traversal und Fremdnamen sind raus."""
    name = os.path.basename(str(name or ""))
    if not APK_NAME_RE.match(name):
        return None
    path = os.path.join(base or apps_dir(), name)
    return path if os.path.isfile(path) else None
