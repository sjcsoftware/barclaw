"""What the user is looking at: session type and the active window's app and title."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Optional

from gi.repository import Gdk


def session_type() -> str:
    display = Gdk.Display.get_default()
    if display is not None:
        name = type(display).__name__
        if "Wayland" in name:
            return "wayland"
        if "X11" in name:
            return "x11"
    return os.environ.get("XDG_SESSION_TYPE", "x11")


def _x11_active_window() -> tuple:
    try:
        from gi.repository import GdkX11  # Only present on X11 builds of GTK.
    except ImportError:
        return None, None
    display = Gdk.Display.get_default()
    if not isinstance(display, GdkX11.X11Display):
        return None, None
    screen = display.get_default_screen()
    active = screen.get_active_window() if hasattr(screen, "get_active_window") else None
    if active is None:
        return None, None
    xid = active.get_xid()
    try:
        output = subprocess.run(
            ["xprop", "-id", str(xid), "WM_CLASS", "_NET_WM_NAME"], capture_output=True, text=True, timeout=1,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    app, title = None, None
    for line in output.splitlines():
        if line.startswith("WM_CLASS") and '"' in line:
            parts = line.split('"')
            app = parts[3] if len(parts) >= 4 else parts[1]
        elif line.startswith("_NET_WM_NAME") and '"' in line:
            title = line.split('"', 1)[1].rsplit('"', 1)[0]
    return app, title


def _sway_active_window() -> tuple:
    if not shutil.which("swaymsg"):
        return None, None
    try:
        tree = json.loads(subprocess.run(["swaymsg", "-t", "get_tree"], capture_output=True, text=True, timeout=1).stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, None
    stack = [tree]
    while stack:
        node = stack.pop()
        if node.get("focused"):
            props = node.get("window_properties") or {}
            return node.get("app_id") or props.get("class"), node.get("name")
        stack += node.get("nodes", []) + node.get("floating_nodes", [])
    return None, None


def _hyprland_active_window() -> tuple:
    if not shutil.which("hyprctl"):
        return None, None
    try:
        info = json.loads(subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True, timeout=1).stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, None
    return info.get("class"), info.get("title")


def active_window() -> tuple[Optional[str], Optional[str]]:
    """(app, window title), each None when the desktop won't say (GNOME and KDE on Wayland)."""
    if session_type() == "x11":
        return _x11_active_window()
    if os.environ.get("SWAYSOCK"):
        return _sway_active_window()
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return _hyprland_active_window()
    return None, None
