"""System-wide hold-to-talk.

X11 grabs the key on the root window through Xlib, which reports press and release. Wayland uses
the XDG GlobalShortcuts portal (KDE, GNOME 48+, Hyprland), whose Activated and Deactivated signals
carry the same information. Anything else can bind `barclaw --talk-start` / `--talk-stop` itself.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
from ctypes import POINTER, Structure, byref, c_char_p, c_int, c_long, c_uint, c_ulong, c_void_p
from typing import Any, Callable, Optional

from gi.repository import Gdk, Gio, GLib, Gtk

from ..support import log
from ..vision.windows import session_type

KEY_PRESS, KEY_RELEASE = 2, 3
GRAB_MODE_ASYNC = 1
MOD_MASKS = {"shift": 1 << 0, "control": 1 << 2, "alt": 1 << 3, "super": 1 << 6}
# NumLock (Mod2) and CapsLock must not change whether the shortcut matches.
LOCK_COMBOS = (0, 1 << 1, 1 << 4, (1 << 1) | (1 << 4))


class _XKeyEvent(Structure):
    _fields_ = [
        ("type", c_int), ("serial", c_ulong), ("send_event", c_int), ("display", c_void_p),
        ("window", c_ulong), ("root", c_ulong), ("subwindow", c_ulong), ("time", c_ulong),
        ("x", c_int), ("y", c_int), ("x_root", c_int), ("y_root", c_int),
        ("state", c_uint), ("keycode", c_uint), ("same_screen", c_int),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("type", c_int), ("xkey", _XKeyEvent), ("pad", c_long * 24)]


class _XErrorEvent(Structure):
    _fields_ = [
        ("type", c_int), ("display", c_void_p), ("resourceid", c_ulong), ("serial", c_ulong),
        ("error_code", ctypes.c_ubyte), ("request_code", ctypes.c_ubyte), ("minor_code", ctypes.c_ubyte),
    ]


_ERROR_HANDLER = ctypes.CFUNCTYPE(c_int, c_void_p, POINTER(_XErrorEvent))


def describe_accelerator(accelerator: str) -> str:
    """`<Alt>space` → `Alt+Space`."""
    key, mods = Gtk.accelerator_parse(accelerator)
    return Gtk.accelerator_get_label(key, mods) if key else accelerator


class X11HotKey:
    """Its own Xlib connection, watched from the GLib loop, so GTK's event handling is undisturbed."""

    def __init__(self) -> None:
        self.on_press: Optional[Callable[[], None]] = None
        self.on_release: Optional[Callable[[], None]] = None
        self._x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        self._x.XOpenDisplay.restype = c_void_p
        self._x.XOpenDisplay.argtypes = [c_char_p]
        self._x.XDefaultRootWindow.restype = c_ulong
        self._x.XDefaultRootWindow.argtypes = [c_void_p]
        self._x.XConnectionNumber.argtypes = [c_void_p]
        self._x.XPending.argtypes = [c_void_p]
        self._x.XNextEvent.argtypes = [c_void_p, POINTER(_XEvent)]
        self._x.XSync.argtypes = [c_void_p, c_int]
        self._x.XGrabKey.argtypes = [c_void_p, c_int, c_uint, c_ulong, c_int, c_int, c_int]
        self._x.XUngrabKey.argtypes = [c_void_p, c_int, c_uint, c_ulong]
        self._x.XKeysymToKeycode.restype = ctypes.c_ubyte
        self._x.XKeysymToKeycode.argtypes = [c_void_p, c_ulong]
        self._x.XkbSetDetectableAutoRepeat.argtypes = [c_void_p, c_int, POINTER(c_int)]
        name = os.environ.get("DISPLAY", "").encode() or None
        self._display = self._x.XOpenDisplay(name)
        if not self._display:
            raise OSError("Couldn't open the X display")
        self._root = self._x.XDefaultRootWindow(self._display)
        supported = c_int(0)
        self._x.XkbSetDetectableAutoRepeat(self._display, 1, byref(supported))
        self._keycode = 0
        self._modifiers = 0
        self._is_down = False
        self._error_seen = False
        # Must stay referenced for as long as Xlib may call it.
        self._handler = _ERROR_HANDLER(self._on_x_error)
        self._x.XSetErrorHandler.argtypes = [_ERROR_HANDLER]
        self._x.XSetErrorHandler.restype = _ERROR_HANDLER
        channel = GLib.IOChannel.unix_new(self._x.XConnectionNumber(self._display))
        GLib.io_add_watch(channel, GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN, self._readable)
        self._channel = channel

    def _on_x_error(self, _display: Any, event: Any) -> int:
        self._error_seen = True
        return 0

    def register(self, accelerator: Optional[str]) -> bool:
        """Registers the shortcut (or clears it with None). False if another app already owns it."""
        self.unregister()
        if not accelerator:
            return True
        keyval, mods = Gtk.accelerator_parse(accelerator)
        if not keyval:
            log.error("Unparsable shortcut %s", accelerator)
            return False
        keycode = self._x.XKeysymToKeycode(self._display, keyval)
        if not keycode:
            return False
        modifiers = 0
        if mods & Gdk.ModifierType.SHIFT_MASK:
            modifiers |= MOD_MASKS["shift"]
        if mods & Gdk.ModifierType.CONTROL_MASK:
            modifiers |= MOD_MASKS["control"]
        if mods & Gdk.ModifierType.MOD1_MASK:
            modifiers |= MOD_MASKS["alt"]
        if mods & (Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.MOD4_MASK):
            modifiers |= MOD_MASKS["super"]
        previous = self._x.XSetErrorHandler(self._handler)
        self._error_seen = False
        for lock in LOCK_COMBOS:
            self._x.XGrabKey(self._display, keycode, modifiers | lock, self._root, 1, GRAB_MODE_ASYNC, GRAB_MODE_ASYNC)
        self._x.XSync(self._display, 0)
        self._x.XSetErrorHandler(previous)
        if self._error_seen:
            for lock in LOCK_COMBOS:
                self._x.XUngrabKey(self._display, keycode, modifiers | lock, self._root)
            self._x.XSync(self._display, 0)
            log.error("XGrabKey refused %s: another client owns it", accelerator)
            return False
        self._keycode, self._modifiers = keycode, modifiers
        log.info("Registered push-to-talk %s", describe_accelerator(accelerator))
        return True

    def unregister(self) -> None:
        if self._keycode:
            for lock in LOCK_COMBOS:
                self._x.XUngrabKey(self._display, self._keycode, self._modifiers | lock, self._root)
            self._x.XSync(self._display, 0)
            self._keycode = 0
        if self._is_down:
            self._is_down = False
            if self.on_release:
                self.on_release()

    def _readable(self, *_args: Any) -> bool:
        event = _XEvent()
        while self._x.XPending(self._display):
            self._x.XNextEvent(self._display, byref(event))
            if event.type == KEY_PRESS and event.xkey.keycode == self._keycode:
                self._edge(True)
            elif event.type == KEY_RELEASE and event.xkey.keycode == self._keycode:
                self._edge(False)
        return GLib.SOURCE_CONTINUE

    def _edge(self, pressed: bool) -> None:
        # Key auto-repeat re-sends "pressed"; only act on edges.
        if pressed == self._is_down:
            return
        self._is_down = pressed
        callback = self.on_press if pressed else self.on_release
        if callback:
            callback()


PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_IFACE = "org.freedesktop.portal.GlobalShortcuts"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SHORTCUT_ID = "push-to-talk"


def to_portal_trigger(accelerator: str) -> str:
    """`<Alt>space` → `ALT+space`, the XDG shortcuts spec form."""
    key, mods = Gtk.accelerator_parse(accelerator)
    parts = []
    for mask, name in ((Gdk.ModifierType.CONTROL_MASK, "CTRL"), (Gdk.ModifierType.MOD1_MASK, "ALT"), (Gdk.ModifierType.SHIFT_MASK, "SHIFT"), (Gdk.ModifierType.SUPER_MASK, "LOGO")):
        if mods & mask:
            parts.append(name)
    parts.append(Gdk.keyval_name(key) or "space")
    return "+".join(parts)


class PortalHotKey:
    """The XDG GlobalShortcuts portal. The desktop shows its own dialog the first time."""

    def __init__(self) -> None:
        self.on_press: Optional[Callable[[], None]] = None
        self.on_release: Optional[Callable[[], None]] = None
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.session: Optional[str] = None
        self.is_down = False
        self._counter = 0
        self._wanted: Optional[str] = None
        for name, handler in (("Activated", self._activated), ("Deactivated", self._deactivated)):
            self.bus.signal_subscribe(PORTAL_BUS, PORTAL_IFACE, name, PORTAL_PATH, None, Gio.DBusSignalFlags.NONE, handler)

    @classmethod
    def available(cls) -> bool:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            result = bus.call_sync(
                PORTAL_BUS, PORTAL_PATH, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (PORTAL_IFACE, "version")), None, Gio.DBusCallFlags.NONE, 1500, None,
            )
            return bool(result.unpack()[0])
        except GLib.Error:
            return False

    def _request_path(self) -> tuple:
        self._counter += 1
        token = f"barclaw{os.getpid()}_{self._counter}"
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        return token, f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

    def _call(self, method: str, args: GLib.Variant, path: str, done: Callable[[dict], None]) -> None:
        subscription = 0

        def response(_bus, _sender, _path, _iface, _signal, params) -> None:
            nonlocal subscription
            self.bus.signal_unsubscribe(subscription)
            code, results = params.unpack()
            if code == 0:
                done(results)
            else:
                log.error("GlobalShortcuts %s was declined (%s)", method, code)

        subscription = self.bus.signal_subscribe(PORTAL_BUS, REQUEST_IFACE, "Response", path, None, Gio.DBusSignalFlags.NONE, response)

        def finished(connection, result) -> None:
            try:
                connection.call_finish(result)
            except GLib.Error as error:
                self.bus.signal_unsubscribe(subscription)
                log.error("GlobalShortcuts %s failed: %s", method, error.message)

        self.bus.call(PORTAL_BUS, PORTAL_PATH, PORTAL_IFACE, method, args, GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None, finished)

    def register(self, accelerator: Optional[str]) -> bool:
        self._wanted = accelerator
        if accelerator is None:
            return True
        if self.session is None:
            self._create_session()
        else:
            self._bind()
        return True

    def _create_session(self) -> None:
        token, path = self._request_path()
        session_token = f"barclawsession{os.getpid()}"
        options = {"handle_token": GLib.Variant("s", token), "session_handle_token": GLib.Variant("s", session_token)}

        def created(results: dict) -> None:
            self.session = results.get("session_handle")
            self._bind()

        self._call("CreateSession", GLib.Variant("(a{sv})", (options,)), path, created)

    def _bind(self) -> None:
        if self.session is None or self._wanted is None:
            return
        token, path = self._request_path()
        description = {
            "description": GLib.Variant("s", "Hold to talk to your agent"),
            "preferred_trigger": GLib.Variant("s", to_portal_trigger(self._wanted)),
        }
        args = GLib.Variant("(oa(sa{sv})sa{sv})", (self.session, [(SHORTCUT_ID, description)], "", {"handle_token": GLib.Variant("s", token)}))
        self._call("BindShortcuts", args, path, lambda _r: log.info("Bound the portal shortcut"))

    def unregister(self) -> None:
        self._wanted = None

    def _activated(self, _bus, _sender, _path, _iface, _signal, params) -> None:
        session, shortcut, _ts, _options = params.unpack()
        if session == self.session and shortcut == SHORTCUT_ID and not self.is_down:
            self.is_down = True
            if self.on_press:
                self.on_press()

    def _deactivated(self, _bus, _sender, _path, _iface, _signal, params) -> None:
        session, shortcut, _ts, _options = params.unpack()
        if session == self.session and shortcut == SHORTCUT_ID and self.is_down:
            self.is_down = False
            if self.on_release:
                self.on_release()


class NoHotKey:
    """No global shortcut mechanism here. `barclaw --talk-start/--talk-stop` still work."""

    on_press = None
    on_release = None

    def register(self, accelerator: Optional[str]) -> bool:
        return accelerator is None

    def unregister(self) -> None:
        pass


def make_hotkey() -> Any:
    if session_type() == "x11":
        try:
            return X11HotKey()
        except OSError as error:
            log.error("X11 hotkey unavailable: %s", error)
            return NoHotKey()
    if PortalHotKey.available():
        return PortalHotKey()
    log.info("No GlobalShortcuts portal; bind `barclaw --talk-start` / `--talk-stop` in your compositor")
    return NoHotKey()
