#!/usr/bin/env bash
# Runs the tests on a private X server and session bus, so they never touch your desktop.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src" GDK_BACKEND=x11 NO_AT_BRIDGE=1
if ! command -v Xvfb >/dev/null; then
  echo "Xvfb not found; running without display tests (install xvfb for the full suite)." >&2
  exec python3 -m pytest -q "$@"
fi
DISPLAY_NUM=$(( 90 + RANDOM % 400 ))
Xvfb ":$DISPLAY_NUM" -screen 0 1600x1000x24 -nolisten tcp >/dev/null 2>&1 &
XVFB=$!
trap 'kill "$XVFB" 2>/dev/null' EXIT
sleep 1
env -u WAYLAND_DISPLAY -u I3SOCK DISPLAY=":$DISPLAY_NUM" dbus-run-session -- python3 -m pytest -q "$@"
