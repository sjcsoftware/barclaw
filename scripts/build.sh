#!/usr/bin/env bash
# Builds the release assets in dist/, each with a .sha256:
#   barclaw-<version>.tar.gz                  the app, from this checkout
#   gnome-shell-extension-<version>.tar.gz    the GNOME Shell button, from the platform/gnome-shell branch
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="$(cat VERSION)"
DIST="$PWD/dist"
GNOME_REF="origin/platform/gnome-shell"
rm -rf "$DIST"
mkdir -p "$DIST"

echo "==> App"
ROOT="$DIST/barclaw-$VERSION"
mkdir -p "$ROOT"
cp -r src/barclaw "$ROOT/"
find "$ROOT/barclaw" -name __pycache__ -type d -prune -exec rm -rf {} +
echo "$VERSION" > "$ROOT/barclaw/VERSION"
cp install.sh data/dev.barclaw.Barclaw.desktop "$ROOT/"
python3 scripts/make-icon.py "$ROOT/icon.png" 256
tar -C "$DIST" --owner=0 --group=0 -czf "$DIST/barclaw-$VERSION.tar.gz" "barclaw-$VERSION"
rm -rf "$ROOT"

echo "==> GNOME Shell extension"
git fetch -q origin "+refs/heads/platform/gnome-shell:refs/remotes/$GNOME_REF"
git archive --format=tar.gz --prefix="gnome-shell-extension-$VERSION/" -o "$DIST/gnome-shell-extension-$VERSION.tar.gz" "$GNOME_REF" gnome-shell

echo "==> Checksums"
(cd "$DIST" && for f in *.tar.gz; do sha256sum "$f" > "$f.sha256"; done)
ls -l "$DIST"
