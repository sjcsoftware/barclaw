#!/usr/bin/env bash
# Installs the extension for the current user. Log out and back in on Wayland, then enable it.
set -euo pipefail
UUID="barclaw@sjcsoftware.github.io"
TARGET="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID"
mkdir -p "$TARGET"
cp "$(dirname "$0")/$UUID/"* "$TARGET/"
echo "Installed to $TARGET."
echo "Log out and back in, then run: gnome-extensions enable $UUID"
