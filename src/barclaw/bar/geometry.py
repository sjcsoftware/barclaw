"""Finds the part of a monitor that bars leave free.

GTK's work area is only right when the window manager publishes `_NET_WORKAREA` and every bar
reserves struts. Neither is reliable: i3 publishes no work area at all, and bars like Eww or polybar
often reserve nothing and rely on window manager gaps. So on X11 the bars are found directly.
"""
from __future__ import annotations

import ctypes
import ctypes.util
from ctypes import POINTER, Structure, byref, c_char_p, c_int, c_long, c_ubyte, c_uint, c_ulong, c_void_p
from typing import Iterable

from gi.repository import Gdk

from ..support import log
from ..vision.windows import session_type

Rect = tuple  # (x, y, width, height)

IS_VIEWABLE = 2
XA_ATOM = 4
# Window managers reparent ordinary clients, so a dock can sit below a frame; deeper levels are app internals.
MAX_DEPTH = 3
EDGE_TOLERANCE = 24
MIN_LENGTH_FRACTION = 0.4
MAX_THICKNESS = 120
MAX_THICKNESS_FRACTION = 0.15


class _Attributes(Structure):
    _fields_ = [
        ("x", c_int), ("y", c_int), ("width", c_int), ("height", c_int), ("border_width", c_int), ("depth", c_int),
        ("visual", c_void_p), ("root", c_ulong), ("c_class", c_int), ("bit_gravity", c_int), ("win_gravity", c_int),
        ("backing_store", c_int), ("backing_planes", c_ulong), ("backing_pixel", c_ulong), ("save_under", c_int),
        ("colormap", c_ulong), ("map_installed", c_int), ("map_state", c_int), ("all_event_masks", c_long),
        ("your_event_mask", c_long), ("do_not_propagate_mask", c_long), ("override_redirect", c_int), ("screen", c_void_p),
    ]


_ERROR_HANDLER = ctypes.CFUNCTYPE(c_int, c_void_p, c_void_p)


def _ignore_x_error(_display: object, _event: object) -> int:
    return 0  # A window can vanish between listing it and asking about it.


_error_handler = _ERROR_HANDLER(_ignore_x_error)


def _load_xlib() -> ctypes.CDLL:
    x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    x.XOpenDisplay.restype = c_void_p
    x.XOpenDisplay.argtypes = [c_char_p]
    x.XCloseDisplay.argtypes = [c_void_p]
    x.XDefaultRootWindow.restype = c_ulong
    x.XDefaultRootWindow.argtypes = [c_void_p]
    x.XInternAtom.restype = c_ulong
    x.XInternAtom.argtypes = [c_void_p, c_char_p, c_int]
    x.XQueryTree.argtypes = [c_void_p, c_ulong, POINTER(c_ulong), POINTER(c_ulong), POINTER(POINTER(c_ulong)), POINTER(c_uint)]
    x.XGetWindowAttributes.argtypes = [c_void_p, c_ulong, POINTER(_Attributes)]
    x.XGetWindowProperty.argtypes = [
        c_void_p, c_ulong, c_ulong, c_long, c_long, c_int, c_ulong, POINTER(c_ulong), POINTER(c_int),
        POINTER(c_ulong), POINTER(c_ulong), POINTER(POINTER(c_ubyte)),
    ]
    x.XFree.argtypes = [c_void_p]
    x.XSetErrorHandler.restype = _ERROR_HANDLER
    x.XSetErrorHandler.argtypes = [_ERROR_HANDLER]
    return x


def _is_dock(x: ctypes.CDLL, display: int, window: int, type_atom: int, dock_atom: int) -> bool:
    actual_type, actual_format = c_ulong(), c_int()
    count, remaining = c_ulong(), c_ulong()
    data = POINTER(c_ubyte)()
    status = x.XGetWindowProperty(
        display, window, type_atom, 0, 16, 0, XA_ATOM, byref(actual_type), byref(actual_format), byref(count), byref(remaining), byref(data),
    )
    if status != 0 or not data:
        return False
    try:
        atoms = ctypes.cast(data, POINTER(c_ulong))
        return actual_format.value == 32 and any(atoms[i] == dock_atom for i in range(count.value))
    finally:
        x.XFree(data)


def x11_bar_candidates(display_name: str) -> list:
    """Screen rectangles of mapped dock windows and of override-redirect top-level windows."""
    x = _load_xlib()
    display = x.XOpenDisplay(display_name.encode())
    if not display:
        return []
    previous = x.XSetErrorHandler(_error_handler)
    found: list = []
    try:
        type_atom = x.XInternAtom(display, b"_NET_WM_WINDOW_TYPE", 0)
        dock_atom = x.XInternAtom(display, b"_NET_WM_WINDOW_TYPE_DOCK", 0)

        def walk(window: int, offset_x: int, offset_y: int, depth: int) -> None:
            root, parent = c_ulong(), c_ulong()
            children = POINTER(c_ulong)()
            count = c_uint()
            if not x.XQueryTree(display, window, byref(root), byref(parent), byref(children), byref(count)):
                return
            try:
                ids = [children[i] for i in range(count.value)]
            finally:
                if children:
                    x.XFree(children)
            for child in ids:
                attributes = _Attributes()
                if not x.XGetWindowAttributes(display, child, byref(attributes)) or attributes.map_state != IS_VIEWABLE:
                    continue
                left, top = offset_x + attributes.x, offset_y + attributes.y
                top_level_overlay = depth == 1 and attributes.override_redirect
                if top_level_overlay or _is_dock(x, display, child, type_atom, dock_atom):
                    found.append((left, top, attributes.width, attributes.height))
                elif depth < MAX_DEPTH:
                    walk(child, left, top, depth + 1)

        walk(x.XDefaultRootWindow(display), 0, 0, 1)
    finally:
        x.XSetErrorHandler(previous)
        x.XCloseDisplay(display)
    return found


def clear_area(monitor: Rect, work_area: Rect, candidates: Iterable[Rect]) -> Rect:
    """The work area with any bar along a monitor edge taken out. Other windows are ignored."""
    mx, my, mw, mh = monitor
    left, top = work_area[0], work_area[1]
    right, bottom = left + work_area[2], top + work_area[3]
    for x, y, w, h in candidates:
        if x >= mx + mw or x + w <= mx or y >= my + mh or y + h <= my:
            continue  # On another monitor.
        horizontal = w >= mw * MIN_LENGTH_FRACTION and h <= max(MAX_THICKNESS, mh * MAX_THICKNESS_FRACTION)
        vertical = h >= mh * MIN_LENGTH_FRACTION and w <= max(MAX_THICKNESS, mw * MAX_THICKNESS_FRACTION)
        if horizontal and y - my <= EDGE_TOLERANCE:
            top = max(top, y + h)
        elif horizontal and my + mh - (y + h) <= EDGE_TOLERANCE:
            bottom = min(bottom, y)
        elif vertical and x - mx <= EDGE_TOLERANCE:
            left = max(left, x + w)
        elif vertical and mx + mw - (x + w) <= EDGE_TOLERANCE:
            right = min(right, x)
    return left, top, right - left, bottom - top


def monitor_clear_area(monitor: Gdk.Monitor) -> Gdk.Rectangle:
    """Where the panel may sit on this monitor, in GDK's coordinates."""
    geometry, work = monitor.get_geometry(), monitor.get_workarea()
    result = Gdk.Rectangle()
    result.x, result.y, result.width, result.height = work.x, work.y, work.width, work.height
    if session_type() != "x11":
        return result  # Wayland hides other windows; layer-shell surfaces keep clear of bars by themselves.
    try:
        scale = monitor.get_scale_factor()
        candidates = [(x // scale, y // scale, w // scale, h // scale) for x, y, w, h in x11_bar_candidates(monitor.get_display().get_name())]
    except OSError as error:
        log.error("Couldn't look for bars: %s", error)
        return result
    area = clear_area((geometry.x, geometry.y, geometry.width, geometry.height), (work.x, work.y, work.width, work.height), candidates)
    result.x, result.y, result.width, result.height = area
    return result
