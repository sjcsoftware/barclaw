#!/usr/bin/env bash
# Regenerates README screenshots from the real widgets, on a private X server with fake data.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/make-icon.py docs/images/icon.png 256
DISPLAY_NUM=$(( 90 + RANDOM % 400 ))
Xvfb ":$DISPLAY_NUM" -screen 0 1920x1200x24 -nolisten tcp >/dev/null 2>&1 &
XVFB=$!
WORK="$(mktemp -d)"
trap 'kill "$XVFB" 2>/dev/null; rm -rf "$WORK"' EXIT
sleep 1
env -u WAYLAND_DISPLAY -u I3SOCK DISPLAY=":$DISPLAY_NUM" GDK_BACKEND=x11 GTK_THEME=Adwaita:dark NO_AT_BRIDGE=1 GTK_USE_PORTAL=0 \
  BARCLAW_CONFIG_DIR="$WORK/c" BARCLAW_DATA_DIR="$WORK/d" BARCLAW_TRAY=none PYTHONPATH="$PWD/src" \
  dbus-run-session -- python3 tools/screenshots.py docs/images
