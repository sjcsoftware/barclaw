"""The menu bar icon.

`SniTray` speaks StatusNotifierItem and DBusMenu directly, which covers KDE, GNOME with the
AppIndicator extension, Xfce, Cinnamon, MATE, Budgie, LXQt, Waybar, Eww and others. It needs no
indicator library and, unlike libappindicator, receives left clicks. `XEmbedTray` is the fallback
for bars that only host legacy tray icons, such as i3bar, polybar, and tint2.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

import cairo
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, PangoCairo

from ..support import log
from ..vision.windows import session_type
from .theme import SIRI

WATCHER_BUS = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
ANIMATION_INTERVAL_MS = 125  # About 8 frames a second: enough for a 22 px icon, cheap for the bar.

SNI_XML = """
<node><interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/><property name="IconThemePath" type="s" access="read"/>
  <property name="IconName" type="s" access="read"/><property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/><property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/><property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionMovieName" type="s" access="read"/><property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/><property name="Menu" type="o" access="read"/>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewTitle"/><signal name="NewIcon"/><signal name="NewAttentionIcon"/><signal name="NewOverlayIcon"/>
  <signal name="NewToolTip"/><signal name="NewStatus"><arg type="s"/></signal>
</interface></node>
"""

MENU_XML = """
<node><interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/><property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/><property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
    <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
  <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/></method>
  <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
  <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
  <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
  <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
  <signal name="ItemActivationRequested"><arg type="i"/><arg type="u"/></signal>
</interface></node>
"""


@dataclass(frozen=True)
class TrayState:
    emoji: str = "🦞"
    tooltip: str = "BarClaw"
    # idle | offline | listening | thinking | attention_reply | attention_input | attention_error
    mode: str = "idle"
    usage: Optional[float] = None  # 0…1, drawn as a thin meter under the icon.

    @property
    def animated(self) -> bool:
        return self.mode in ("listening", "thinking") or self.mode.startswith("attention")


def _emoji_layout(c: cairo.Context, emoji: str, size: float) -> Any:
    layout = PangoCairo.create_layout(c)
    description = Pango.FontDescription()
    description.set_family("Noto Color Emoji, Apple Color Emoji, Twemoji, Segoe UI Emoji, sans-serif")
    description.set_absolute_size(size * Pango.SCALE)
    layout.set_font_description(description)
    layout.set_text(emoji, -1)
    return layout


def render_icon(size: int, state: TrayState, phase: float) -> cairo.ImageSurface:
    """The icon at one size: the agent's emoji with a state ring. `phase` runs 0…1 and loops."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    c = cairo.Context(surface)
    meter = 3 if state.usage is not None and size >= 20 else 0
    field = size - meter
    cx, cy = size / 2, field / 2
    radius = field / 2 - max(1.0, size * 0.05)

    if state.mode.startswith("attention"):
        pulse = 0.65 + 0.35 * math.sin(phase * 2 * math.pi)
        _ring(c, cx, cy, radius, max(1.6, size * 0.11), phase, pulse)
    elif state.mode == "thinking":
        c.set_line_cap(cairo.LINE_CAP_ROUND)
        c.set_line_width(max(1.5, size * 0.09))
        for k in range(3):
            start = phase * 2 * math.pi + k * 2 * math.pi / 3
            c.set_source_rgba(*SIRI[k + 1], 1)
            c.arc(cx, cy, radius, start, start + 1.3)
            c.stroke()
    elif state.mode == "listening":
        c.set_line_width(max(1.5, size * 0.1))
        c.set_source_rgba(*SIRI[0], 0.55 + 0.45 * abs(math.sin(phase * math.pi * 2)))
        c.arc(cx, cy, radius, 0, 2 * math.pi)
        c.stroke()

    inner = field * 0.62 if state.mode != "idle" and state.mode != "offline" else field * 0.78
    layout = _emoji_layout(c, state.emoji, inner)
    width, height = layout.get_pixel_size()
    c.move_to(cx - width / 2, cy - height / 2)
    if state.mode == "offline":
        c.push_group()
        PangoCairo.show_layout(c, layout)
        pattern = c.pop_group()
        c.set_source_rgba(0.62, 0.62, 0.62, 0.8)
        c.mask(pattern)
        c.set_source_rgba(0.95, 0.3, 0.27, 1)
        c.arc(size - size * 0.2, field - field * 0.2, max(1.8, size * 0.13), 0, 2 * math.pi)
        c.fill()
    else:
        PangoCairo.show_layout(c, layout)

    if meter:
        used = min(1.0, max(0.0, state.usage or 0))
        c.set_source_rgba(0.5, 0.5, 0.55, 0.55)
        c.rectangle(2, size - 2, size - 4, 2)
        c.fill()
        c.set_source_rgba(*(SIRI[0] if used >= 0.85 else SIRI[3]), 1)
        c.rectangle(2, size - 2, (size - 4) * used, 2)
        c.fill()
    surface.flush()
    return surface


def _ring(c: cairo.Context, cx: float, cy: float, radius: float, width: float, phase: float, alpha: float) -> None:
    c.set_line_width(width)
    c.set_line_cap(cairo.LINE_CAP_BUTT)
    segments = 40
    for i in range(segments):
        fraction = (i / segments + phase) % 1.0
        position = fraction * 5
        low, mix = int(position) % 5, position - int(position)
        high = (low + 1) % 5
        color = [SIRI[low][j] + (SIRI[high][j] - SIRI[low][j]) * mix for j in range(3)]
        c.set_source_rgba(*color, alpha)
        start = i / segments * 2 * math.pi
        c.arc(cx, cy, radius, start, start + 2 * math.pi / segments + 0.03)
        c.stroke()


def pixmap(surface: cairo.ImageSurface) -> tuple:
    """Cairo's native-endian ARGB32 as the big-endian bytes StatusNotifierItem expects."""
    width, height = surface.get_width(), surface.get_height()
    raw = bytes(surface.get_data())
    stride = surface.get_stride()
    out = bytearray(width * height * 4)
    for y in range(height):
        row = raw[y * stride: y * stride + width * 4]
        out[y * width * 4: (y + 1) * width * 4] = bytes(b for i in range(0, len(row), 4) for b in (row[i + 3], row[i + 2], row[i + 1], row[i]))
    return width, height, bytes(out)


class TrayBase:
    """Backends report clicks through these; `(x, y)` is screen coordinates, or (0, 0) when unknown."""

    def __init__(self) -> None:
        self.on_activate: Callable[[int, int], None] = lambda _x, _y: None
        self.on_secondary: Callable[[], None] = lambda: None
        self.menu_items: list = []  # (label or None for a separator, callback)
        self.state = TrayState()
        self._animation = 0
        self._phase = 0.0

    def update(self, state: TrayState) -> None:
        if state == self.state:
            return
        previous, self.state = self.state, state
        self._apply(previous)
        if state.animated and not self._animation:
            self._animation = GLib.timeout_add(ANIMATION_INTERVAL_MS, self._tick)
        elif not state.animated and self._animation:
            GLib.source_remove(self._animation)
            self._animation = 0

    def _tick(self) -> bool:
        self._phase = (self._phase + 0.05) % 1.0
        self._redraw()
        return GLib.SOURCE_CONTINUE

    def _apply(self, previous: TrayState) -> None:
        raise NotImplementedError

    def _redraw(self) -> None:
        raise NotImplementedError

    def is_visible(self) -> bool:
        raise NotImplementedError

    def rebuild_menu(self) -> None:
        pass


class SniTray(TrayBase):
    SIZES = (22, 32, 48)

    def __init__(self, bus: Gio.DBusConnection) -> None:
        super().__init__()
        self.bus = bus
        self.service = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._revision = 1
        self._registered = False
        self.on_gone: Callable[[], None] = lambda: None
        item = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self._item_id = bus.register_object(ITEM_PATH, item, self._item_method, self._item_property)
        self._menu_id = bus.register_object(MENU_PATH, menu, self._menu_method, self._menu_property)
        self._name_id = Gio.bus_own_name_on_connection(bus, self.service, Gio.BusNameOwnerFlags.NONE, self._name_acquired, None)
        self._watch = Gio.bus_watch_name_on_connection(bus, WATCHER_BUS, Gio.BusNameWatcherFlags.NONE, self._watcher_appeared, self._watcher_vanished)

    def is_visible(self) -> bool:
        return self._registered

    def _name_acquired(self, *_args: Any) -> None:
        self._register()

    def _watcher_appeared(self, *_args: Any) -> None:
        self._register()

    def _watcher_vanished(self, *_args: Any) -> None:
        if self._registered:
            self._registered = False
            self.on_gone()

    def _register(self) -> None:
        def finished(connection: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
            try:
                connection.call_finish(result)
                self._registered = True
                log.info("Registered with the StatusNotifier watcher")
            except GLib.Error as error:
                log.info("StatusNotifier registration failed: %s", error.message)

        self.bus.call(
            WATCHER_BUS, WATCHER_PATH, WATCHER_BUS, "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (self.service,)), None, Gio.DBusCallFlags.NONE, 5000, None, finished,
        )

    def _pixmaps(self) -> GLib.Variant:
        return GLib.Variant("a(iiay)", [pixmap(render_icon(size, self.state, self._phase)) for size in self.SIZES])

    def _emit(self, name: str, args: Optional[GLib.Variant] = None) -> None:
        try:
            self.bus.emit_signal(None, ITEM_PATH, "org.kde.StatusNotifierItem", name, args)
        except GLib.Error as error:
            log.debug("Signal %s failed: %s", name, error.message)

    def _status(self) -> str:
        return "NeedsAttention" if self.state.mode.startswith("attention") else "Active"

    def _apply(self, previous: TrayState) -> None:
        self._emit("NewIcon")
        self._emit("NewAttentionIcon")
        if previous.tooltip != self.state.tooltip:
            self._emit("NewToolTip")
            self._emit("NewTitle")
        if previous.mode.startswith("attention") != self.state.mode.startswith("attention"):
            self._emit("NewStatus", GLib.Variant("(s)", (self._status(),)))

    def _redraw(self) -> None:
        self._emit("NewIcon")
        if self.state.mode.startswith("attention"):
            self._emit("NewAttentionIcon")

    def _item_property(self, _bus, _sender, _path, _iface, name: str) -> Optional[GLib.Variant]:
        empty = GLib.Variant("a(iiay)", [])
        values = {
            "Category": lambda: GLib.Variant("s", "ApplicationStatus"),
            "Id": lambda: GLib.Variant("s", "barclaw"),
            "Title": lambda: GLib.Variant("s", self.state.tooltip),
            "Status": lambda: GLib.Variant("s", self._status()),
            "WindowId": lambda: GLib.Variant("i", 0),
            "IconThemePath": lambda: GLib.Variant("s", ""),
            "IconName": lambda: GLib.Variant("s", ""),
            "IconPixmap": self._pixmaps,
            "OverlayIconName": lambda: GLib.Variant("s", ""),
            "OverlayIconPixmap": lambda: empty,
            "AttentionIconName": lambda: GLib.Variant("s", ""),
            "AttentionIconPixmap": self._pixmaps,
            "AttentionMovieName": lambda: GLib.Variant("s", ""),
            "ToolTip": lambda: GLib.Variant("(sa(iiay)ss)", ("", [], "BarClaw", self.state.tooltip)),
            "ItemIsMenu": lambda: GLib.Variant("b", False),
            "Menu": lambda: GLib.Variant("o", MENU_PATH),
        }
        return values[name]() if name in values else None

    def _item_method(self, _bus, _sender, _path, _iface, method: str, params: GLib.Variant, invocation: Gio.DBusMethodInvocation) -> None:
        invocation.return_value(None)
        args = params.unpack()
        if method == "Activate":
            GLib.idle_add(self.on_activate, args[0], args[1])
        elif method == "SecondaryActivate":
            GLib.idle_add(self.on_secondary)
        elif method == "ContextMenu":
            GLib.idle_add(self._show_context_menu)

    def _show_context_menu(self) -> bool:
        popup_gtk_menu(self.menu_items)
        return GLib.SOURCE_REMOVE

    # MARK: DBusMenu

    def rebuild_menu(self) -> None:
        self._revision += 1
        try:
            self.bus.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated", GLib.Variant("(ui)", (self._revision, 0)))
        except GLib.Error:
            pass

    def _item_properties(self, item_id: int) -> dict:
        if item_id == 0:
            return {"children-display": GLib.Variant("s", "submenu")}
        label = self.menu_items[item_id - 1][0]
        if label is None:
            return {"type": GLib.Variant("s", "separator")}
        return {"label": GLib.Variant("s", label), "enabled": GLib.Variant("b", True), "visible": GLib.Variant("b", True)}

    def _layout_variant(self) -> GLib.Variant:
        children = [GLib.Variant("(ia{sv}av)", (i + 1, self._item_properties(i + 1), [])) for i in range(len(self.menu_items))]
        return GLib.Variant("(u(ia{sv}av))", (self._revision, (0, self._item_properties(0), children)))

    def _menu_property(self, _bus, _sender, _path, _iface, name: str) -> Optional[GLib.Variant]:
        return {
            "Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", []),
        }.get(name)

    def _menu_method(self, _bus, _sender, _path, _iface, method: str, params: GLib.Variant, invocation: Gio.DBusMethodInvocation) -> None:
        args = params.unpack()
        if method == "GetLayout":
            invocation.return_value(self._layout_variant())
        elif method == "GetGroupProperties":
            valid = [i for i in args[0] if 0 <= i <= len(self.menu_items)] or list(range(len(self.menu_items) + 1))
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(i, self._item_properties(i)) for i in valid],)))
        elif method == "GetProperty":
            properties = self._item_properties(args[0]) if 0 <= args[0] <= len(self.menu_items) else {}
            invocation.return_value(GLib.Variant("(v)", (properties.get(args[1], GLib.Variant("s", "")),)))
        elif method == "Event":
            invocation.return_value(None)
            if args[1] == "clicked" and 1 <= args[0] <= len(self.menu_items):
                GLib.idle_add(self.menu_items[args[0] - 1][1])
        elif method == "EventGroup":
            for item_id, event, _data, _time in args[0]:
                if event == "clicked" and 1 <= item_id <= len(self.menu_items):
                    GLib.idle_add(self.menu_items[item_id - 1][1])
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def close(self) -> None:
        Gio.bus_unwatch_name(self._watch)
        Gio.bus_unown_name(self._name_id)
        self.bus.unregister_object(self._item_id)
        self.bus.unregister_object(self._menu_id)
        if self._animation:
            GLib.source_remove(self._animation)
            self._animation = 0


def popup_gtk_menu(items: list) -> None:
    menu = Gtk.Menu()
    for label, callback in items:
        entry = Gtk.SeparatorMenuItem() if label is None else Gtk.MenuItem(label=label)
        if label is not None:
            entry.connect("activate", lambda _w, cb=callback: cb())
        menu.append(entry)
    menu.show_all()
    # Keeps the menu alive until it closes.
    menu.connect("deactivate", lambda m: GLib.timeout_add(500, lambda: (m.destroy(), GLib.SOURCE_REMOVE)[1]))
    menu.popup_at_pointer(None)


class XEmbedTray(TrayBase):
    """`Gtk.StatusIcon` is deprecated but remains the only GTK route onto XEmbed trays."""

    def __init__(self) -> None:
        super().__init__()
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self.icon = Gtk.StatusIcon()
            self.icon.set_title("BarClaw")
            self.icon.set_visible(True)
        self.icon.connect("activate", self._activated)
        self.icon.connect("popup-menu", self._popup)
        self.icon.connect("size-changed", lambda _i, _s: (self._redraw(), True)[1])
        self._size = 22
        self._redraw()

    def is_visible(self) -> bool:
        with _quiet():
            return self.icon.is_embedded()

    def _activated(self, icon: Gtk.StatusIcon) -> None:
        with _quiet():
            ok, _screen, area, _orientation = icon.get_geometry()
        if ok:
            self.on_activate(area.x + area.width // 2, area.y + area.height)
        else:
            self.on_activate(0, 0)

    def _popup(self, _icon: Gtk.StatusIcon, _button: int, _time: int) -> None:
        popup_gtk_menu(self.menu_items)

    def _apply(self, previous: TrayState) -> None:
        self._redraw()
        with _quiet():
            self.icon.set_tooltip_text(self.state.tooltip)

    def _redraw(self) -> None:
        with _quiet():
            size = max(16, self.icon.get_size() or 22)
            surface = render_icon(size, self.state, self._phase)
            self.icon.set_from_pixbuf(Gdk.pixbuf_get_from_surface(surface, 0, 0, size, size))


class _quiet:
    """GtkStatusIcon calls raise DeprecationWarning under PyGObject; they're expected here."""

    def __enter__(self) -> None:
        import warnings

        self._context = warnings.catch_warnings()
        self._context.__enter__()
        warnings.simplefilter("ignore", DeprecationWarning)

    def __exit__(self, *args: Any) -> None:
        self._context.__exit__(*args)


class NoTray(TrayBase):
    """No host anywhere: the app still runs, opened by `barclaw --toggle`."""

    def is_visible(self) -> bool:
        return False

    def _apply(self, previous: TrayState) -> None:
        pass

    def _redraw(self) -> None:
        pass


def has_watcher(bus: Gio.DBusConnection) -> bool:
    try:
        reply = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", (WATCHER_BUS,)), GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 1500, None,
        )
    except GLib.Error:
        return False
    return reply.unpack()[0]


def make_tray() -> TrayBase:
    override = os.environ.get("BARCLAW_TRAY")  # sni | xembed | none, for odd setups.
    try:
        bus: Optional[Gio.DBusConnection] = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error:
        bus = None
    if override == "none":
        return NoTray()
    if override == "sni" and bus is not None:
        return SniTray(bus)
    if override == "xembed":
        return XEmbedTray()
    if bus is not None and has_watcher(bus):
        return SniTray(bus)
    if session_type() == "x11":
        return XEmbedTray()
    if bus is not None:
        return SniTray(bus)  # Registers as soon as a host starts.
    return NoTray()
