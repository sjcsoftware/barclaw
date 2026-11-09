#!/usr/bin/env bash
# Builds the release artifacts in dist/:
#   barclaw-<version>.tar.gz   self-contained user install (and what in-app updates use)
#   barclaw_<version>_all.deb  Debian/Ubuntu package
# plus a .sha256 for each.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="$(cat VERSION)"
DIST="$PWD/dist"
rm -rf "$DIST"
mkdir -p "$DIST"
ICON="$DIST/icon.png"
python3 scripts/make-icon.py "$ICON" 256

copy_package() {
  mkdir -p "$1"
  cp -r src/barclaw "$1/"
  find "$1/barclaw" -name __pycache__ -type d -prune -exec rm -rf {} +
  echo "$VERSION" > "$1/barclaw/VERSION"
}

echo "==> Tarball"
ROOT="$DIST/barclaw-$VERSION"
copy_package "$ROOT"
cp scripts/install.sh "$ROOT/install.sh"
cp data/dev.barclaw.Barclaw.desktop "$ICON" "$ROOT/"
tar -C "$DIST" --owner=0 --group=0 -czf "$DIST/barclaw-$VERSION.tar.gz" "barclaw-$VERSION"
rm -rf "$ROOT"

echo "==> Debian package"
DEB="$DIST/deb"
copy_package "$DEB/usr/lib/python3/dist-packages"
install -Dm755 packaging/barclaw.launcher "$DEB/usr/bin/barclaw"
install -Dm644 data/dev.barclaw.Barclaw.desktop "$DEB/usr/share/applications/dev.barclaw.Barclaw.desktop"
install -Dm644 "$ICON" "$DEB/usr/share/icons/hicolor/256x256/apps/dev.barclaw.Barclaw.png"
mkdir -p "$DEB/DEBIAN"
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$(du -sk "$DEB/usr" | cut -f1)/" packaging/deb.control > "$DEB/DEBIAN/control"
dpkg-deb --root-owner-group -b "$DEB" "$DIST/barclaw_${VERSION}_all.deb" >/dev/null
rm -rf "$DEB" "$ICON"

echo "==> Checksums"
(cd "$DIST" && for f in *.tar.gz *.deb; do sha256sum "$f" > "$f.sha256"; done)
ls -l "$DIST"
