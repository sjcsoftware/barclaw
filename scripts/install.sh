#!/usr/bin/env bash
# Installs BarClaw for the current user. Run from an unpacked release tarball; needs no root.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APP="$DATA/barclaw/app"

missing=""
python3 - <<'PY' 2>/dev/null || missing=1
import gi, cairo, cryptography
gi.require_version("Gtk", "3.0"); gi.require_version("Soup", "3.0")
from gi.repository import Gtk, Soup
PY
if [ -n "$missing" ]; then
  echo "BarClaw needs GTK 3, libsoup 3, and python3-cryptography. Install them with:" >&2
  if command -v apt-get >/dev/null; then echo "  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-soup-3.0 python3-cryptography" >&2
  elif command -v dnf >/dev/null; then echo "  sudo dnf install python3-gobject python3-gobject-base gtk3 libsoup3 python3-cryptography python3-cairo" >&2
  elif command -v pacman >/dev/null; then echo "  sudo pacman -S python-gobject python-cairo gtk3 libsoup3 python-cryptography" >&2
  elif command -v zypper >/dev/null; then echo "  sudo zypper install python3-gobject python3-gobject-cairo typelib-1_0-Gtk-3_0 typelib-1_0-Soup-3_0 python3-cryptography" >&2
  fi
  exit 1
fi

mkdir -p "$(dirname "$APP")" "$HOME/.local/bin" "$DATA/applications" "$DATA/icons/hicolor/256x256/apps"
rm -rf "$APP"
mkdir -p "$APP"
cp -r "$HERE/barclaw" "$APP/"
touch "$APP/.barclaw-install"
cat > "$HOME/.local/bin/barclaw" <<LAUNCH
#!/bin/sh
PYTHONPATH="$APP" exec python3 -m barclaw "\$@"
LAUNCH
chmod +x "$HOME/.local/bin/barclaw"
sed "s|^Exec=barclaw|Exec=$HOME/.local/bin/barclaw|" "$HERE/dev.barclaw.Barclaw.desktop" > "$DATA/applications/dev.barclaw.Barclaw.desktop"
cp "$HERE/icon.png" "$DATA/icons/hicolor/256x256/apps/dev.barclaw.Barclaw.png"
echo "Installed BarClaw $(cat "$APP/barclaw/VERSION"). Launch it from your apps menu or run: barclaw"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Note: add ~/.local/bin to your PATH to run it from a terminal." ;; esac
