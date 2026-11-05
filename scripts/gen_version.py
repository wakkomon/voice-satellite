#!/usr/bin/env python3
"""Build-Version erzeugen: scripts/gen_version.py

Rumpelt die Versionsnummer automatisch weiter, damit jeder Deploy eine eigene,
nachvollziehbare Nummer hat — ohne dass jemand von Hand an __version__ dreht.

Regel:
  Basis   = letzter Git-Tag vMA.MI.PA (sonst __version__ aus master/__init__.py)
  Patch   = Basis-Patch + Anzahl Commits seit diesem Tag
  Beispiel: Tag v2.0.0, 3 Commits danach  ->  2.0.3

Geschrieben werden app/_version.py und master/_version.py (beide in
.gitignore — sie sind Build-Artefakte, kein Quellcode). Beim Start gewinnt
diese Datei; fehlt sie (Entwicklungsstand ohne Build), gilt der Wert aus
__init__.py.

Benutzung (vor dem Image-Build, im Git-Checkout):
    python3 scripts/gen_version.py            # schreibt beide Dateien
    python3 scripts/gen_version.py --print    # nur auf stdout

Ausnahmen:
  * kein git / kein Tag  -> Basis aus __init__.py, Patch bleibt unveraendert
  * VS_VERSION=2.1.0 gesetzt (z.B. CI) -> diese Version wird benutzt
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = (ROOT / "app" / "_version.py", ROOT / "master" / "_version.py")
BASE_FILE = ROOT / "master" / "__init__.py"


def _git(*args) -> str:
    try:
        r = subprocess.run(("git", "-C", str(ROOT)) + args,
                           capture_output=True, text=True, timeout=20)
    except Exception:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _base_version() -> str:
    """__version__ aus master/__init__.py (Fallback ohne Git-Tags)."""
    try:
        m = re.search(r'^__version__\s*=\s*"([^"]+)"',
                      BASE_FILE.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "0.0.0"


def _latest_tag() -> str:
    """Letzter vMA.MI.PA-Tag, der zu diesem Checkout gehoert ('' wenn keiner)."""
    tag = _git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*.[0-9]*.[0-9]*")
    if re.fullmatch(r"v\d+\.\d+\.\d+", tag or ""):
        return tag
    return ""


def compute() -> tuple[str, str]:
    """(version, herkunft) — herkunft fuer die Log-/Build-Ausgabe."""
    env = (os.environ.get("VS_VERSION") or "").strip()
    if env:
        return env, "VS_VERSION"
    base = _base_version()
    tag = _latest_tag()
    if not tag:
        return base, "basis (kein git-tag)"
    commits = _git("rev-list", "--count", f"{tag}..HEAD")
    n = int(commits) if commits.isdigit() else 0
    parts = (tag.lstrip("v") or base).split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return base, "basis (ungueltiger tag)"
    ma, mi, pa = (int(p) for p in parts)
    return f"{ma}.{mi}.{pa + n}", f"{tag}+{n}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Build-Version erzeugen")
    ap.add_argument("--print", action="store_true", dest="only_print",
                    help="nur ausgeben, nichts schreiben")
    args = ap.parse_args()

    version, origin = compute()
    sha = _git("rev-parse", "--short", "HEAD")
    if args.only_print:
        print(version)
        return 0
    body = (
        '"""Build-Artefakt von scripts/gen_version.py — nicht editieren, nicht\n'
        'committen (steht in .gitignore). Beim Start schlaegt dieser Wert\n'
        '__version__ aus __init__.py, damit jeder Deploy eine eigene Nummer hat."""\n'
        f'__version__ = "{version}"\n'
        f'_ORIGIN = "{origin}"\n_GIT = "{sha}"\n')
    for path in TARGETS:
        if not path.parent.is_dir():
            continue
        path.write_text(body, encoding="utf-8")
    print(f"Version {version} ({origin}, git {sha or 'unbekannt'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
