#!/usr/bin/env bash
# Voice-Satellite: Release — Version hochzaehlen, taggen, pushen.
#
# Usage: scripts/release.sh [patch|minor|major]
#   patch (Default): Bugfix / kleine Change seit letztem Release
#   minor:           neues Feature
#   major:           Breaking Change
#
# Versionsquelle: der Build-Wert aus scripts/gen_version.py (Git-Tag + Commits
# seitdem) — er laeuft bei jedem Deploy automatisch weiter. Die Fallback-Werte
# in master/__init__.py und app/__init__.py sind nur der Quellstand ohne Build.
# Release setzt die Nummer auf den naechsten Wert UBER dem laufenden Build-Stand
# und markiert ihn mit einem Tag; ab dem Tag zaehlt gen_version weiter.
# Collisions sind damit ausgeschlossen: kein Tag nennt sich eine Nummer, die
# ein Build schon hatte. Das Skript:
#   1. Version hochzaehlen
#   2. Commit "chore(release): vX.Y.Z"
#   3. git tag vX.Y.Z
#   4. push an origin (GitHub) und gitlab
# GitHub-Release und Deploy danach:
#   git pull auf .24 + docker build -t voice-satellite-master:<version> \
#     -f Dockerfile.master . && bash deploy/recreate_master.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

BUMP="${1:-patch}"

CUR="$(python3 scripts/gen_version.py --print)"
if [ -z "$CUR" ]; then echo "FEHLER: Version nicht ermittelbar (git-Checkout noetig)" >&2; exit 1; fi
IFS=. read -r MA MI PA <<<"$CUR"
case "$BUMP" in
  major) MA=$((MA+1)); MI=0; PA=0 ;;
  minor) MI=$((MI+1)); PA=0 ;;
  patch) PA=$((PA+1)) ;;
  *) echo "Ungueltige Stufe: $BUMP (patch|minor|major)" >&2; exit 1 ;;
esac
NEXT="${MA}.${MI}.${PA}"
TAG="v${NEXT}"

if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  echo "FEHLER: Tag ${TAG} existiert bereits" >&2; exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "FEHLER: Arbeitsverzeichnis ist nicht sauber — erst committen, dann releasen" >&2
  exit 1
fi

# Fallback-Werte in beiden Paketen mitziehen (indentiert im except-Zweig).
for f in master/__init__.py app/__init__.py; do
  sed -i "s/^\([[:space:]]*\)__version__ = \".*\"$/\1__version__ = \"${NEXT}\"/" "$f"
done
python3 scripts/gen_version.py >/dev/null
git add master/__init__.py app/__init__.py
git commit -m "chore(release): ${TAG}"
git tag "$TAG"
git push origin main "$TAG"
git push gitlab main "$TAG" 2>/dev/null || echo "WARN: GitLab-Push fehlgeschlagen (Mirror holt es auf)"

echo ""
echo "=== Release ${TAG} fertig ==="
echo "Version: ${CUR} -> ${NEXT}"
echo "Naechste Schritte: GitHub-Release (API, Tag ${TAG}) + Deploy auf .24"
