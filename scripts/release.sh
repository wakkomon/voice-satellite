#!/usr/bin/env bash
# Voice-Satellite: Release — Version hochzaehlen, taggen, pushen.
#
# Usage: scripts/release.sh [patch|minor|major]
#   patch (Default): Bugfix / kleine Change seit letztem Release
#   minor:           neues Feature
#   major:           Breaking Change
#
# Versionsquelle: __version__ in master/__init__.py UND app/__init__.py
# (beide synchron hochzaehlen; nirgends sonst Version hartkodieren).
# Das Skript:
#   1. __version__ hochzaehlen
#   2. Commit "chore(release): vX.Y.Z"
#   3. git tag vX.Y.Z
#   4. push an origin (GitHub) und gitlab
# GitHub-Release und Deploy danach:
#   git pull auf .24 + docker build -t voice-satellite-master:<version> \
#     -f Dockerfile.master . && bash deploy/recreate_master.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

BUMP="${1:-patch}"

CUR=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' master/__init__.py | head -1)
if [ -z "$CUR" ]; then echo "FEHLER: __version__ in master/__init__.py fehlt" >&2; exit 1; fi
APP_VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' app/__init__.py | head -1)
if [ "$APP_VER" != "$CUR" ]; then
  echo "WARN: app/__init__.py (${APP_VER}) != master/__init__.py (${CUR}) — beide werden auf die neue Version gesetzt"
fi
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

for f in master/__init__.py app/__init__.py; do
  sed -i "s/^__version__ = \".*\"$/__version__ = \"${NEXT}\"/" "$f"
done
git add master/__init__.py app/__init__.py
git commit -m "chore(release): ${TAG}"
git tag "$TAG"
git push origin main "$TAG"
git push gitlab main "$TAG" 2>/dev/null || echo "WARN: GitLab-Push fehlgeschlagen (Mirror holt es auf)"

echo ""
echo "=== Release ${TAG} fertig ==="
echo "Version: ${CUR} -> ${NEXT}"
echo "Naechste Schritte: GitHub-Release (API, Tag ${TAG}) + Deploy auf .24"
