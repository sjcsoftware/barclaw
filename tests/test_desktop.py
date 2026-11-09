"""Rendering and desktop integration. Needs a display (scripts/test.sh provides a private Xvfb)."""
import json
import math
import os

import cairo
import pytest
from gi.repository import Gio, GLib

from .conftest import has_display

pytestmark = pytest.mark.skipif(not has_display(), reason="needs a display server")

import barclaw  # noqa: E402
from barclaw.bar import tray as T  # noqa: E402
from barclaw.markdown.charts import paint_chart, parse_chart  # noqa: E402
from barclaw.markdown.mathrender import render_math  # noqa: E402
from barclaw.markdown.parser import inline_markup  # noqa: E402
from barclaw.markdown.view import _split_markup_words  # noqa: E402
from barclaw.support import run_until  # noqa: E402
from barclaw.vision import reader as R  # noqa: E402
from barclaw.voice.controller import rms_level  # noqa: E402
from barclaw.voice.hotkey import describe_accelerator, to_portal_trigger  # noqa: E402


def inked_pixels(surface):
    surface.flush()
    return sum(1 for b in bytes(surface.get_data())[3::4] if b)


@pytest.mark.parametrize("latex", [
    r"\frac{a+b}{c}", r"\sqrt[3]{x^2}", r"\sum_{i=0}^{n} i^2", r"\int_0^1 x\,dx", r"\left[\begin{matrix}1&2\\3&4\end{matrix}\right]",
    r"\begin{cases}a&x>0\\b&\text{else}\end{cases}", r"\hat{x}\tilde{y}\vec{z}\dot{a}\ddot{b}\breve{c}\mathring{d}", r"\underbrace{a+b}_{c}\overbrace{d}^{e}",
    r"\boxed{E=mc^2}", r"\left\{\frac{1}{2}\right\}", r"\left\langle a \right\rangle", r"\left\lfloor x \right\rfloor \left\| y \right\|", r"\sqrt{\frac{a}{b}}", r"\text{가나다} x",
])
def test_math_renders_ink(latex):
    rendered = render_math(latex, 16, True)
    assert rendered.width > 2 and rendered.ascent > 0
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, math.ceil(rendered.width) + 2, math.ceil(rendered.ascent + rendered.descent) + 2)
    context = cairo.Context(surface)
    context.set_source_rgb(1, 1, 1)
    rendered.paint(context, 0, rendered.ascent)
    assert inked_pixels(surface) > 0


def test_inline_math_is_shorter_than_display():
    assert render_math(r"\sum_{i}^{n}", 16, True).ascent > render_math(r"\sum_{i}^{n}", 16, False).ascent


@pytest.mark.parametrize("kind", ["bar", "line", "area", "point", "pie"])
def test_charts_paint(kind):
    spec = parse_chart(json.dumps({"type": kind, "title": "T", "x": "X", "y": "Y", "unit": "$", "data": [{"x": "a", "y": 1, "series": "s"}, {"x": "b", "y": 3, "series": "s"}, {"x": "a", "y": 2, "series": "t"}]}))
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 320, 200)
    paint_chart(cairo.Context(surface), spec, 320, 200)
    assert inked_pixels(surface) > 500


def test_chart_with_one_point_and_negative_values():
    for data in ([{"x": "a", "y": 5}], [{"x": "a", "y": -5}, {"x": "b", "y": 5}], [{"x": "a", "y": 0}]):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 320, 200)
        paint_chart(cairo.Context(surface), parse_chart(json.dumps({"type": "bar", "data": data})), 320, 200)


def test_tray_pixmap_is_big_endian_argb():
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 2, 1)
    context = cairo.Context(surface)
    context.set_source_rgba(1, 0, 0, 1)
    context.rectangle(0, 0, 1, 1)
    context.fill()
    surface.flush()
    width, height, data = T.pixmap(surface)
    assert (width, height) == (2, 1) and data[:4] == bytes([255, 255, 0, 0]) and data[4:8] == bytes(4)


@pytest.mark.parametrize("mode", ["idle", "offline", "listening", "thinking", "attention_reply", "attention_input", "attention_error"])
@pytest.mark.parametrize("size", [16, 22, 48])
def test_tray_icon_renders_every_state(mode, size):
    assert inked_pixels(T.render_icon(size, T.TrayState(mode=mode, usage=0.5), 0.3)) > 0


def test_accelerators():
    assert describe_accelerator("<Alt>space") == "Alt+Space"
    assert to_portal_trigger("<Control><Alt>space") == "CTRL+ALT+space"


def test_pango_markup_from_inline_markdown_is_valid():
    from gi.repository import Pango

    for text in ("**a** *b* `c` [d](http://e) ~~f~~ <g> & h", "***x*** **unclosed", "[a](http://b?x=1&y=2)"):
        for chunk in _split_markup_words(inline_markup(text)):
            ok, *_ = Pango.parse_markup(chunk, -1, "\0")
            assert ok


def test_fit_within_respects_the_byte_limit():
    from gi.repository import GdkPixbuf

    pixels = bytes((x * y) % 251 for y in range(400) for x in range(1200) for _ in range(3))
    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(pixels), GdkPixbuf.Colorspace.RGB, False, 8, 1200, 400, 3600)
    options = R.CaptureOptions(max_bytes=20000, max_width=1200)
    data, fmt, width, _height = R.fit_within(pixbuf, options)
    assert len(data) <= 20000 and fmt == "jpeg" and width <= 1200


def test_scaling_keeps_aspect():
    from gi.repository import GdkPixbuf

    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 2000, 1000)
    scaled = R._scaled(pixbuf, 1000)
    assert (scaled.get_width(), scaled.get_height()) == (1000, 500)


def test_rms_level():
    assert rms_level(b"\0\0" * 100) == 0
    assert rms_level(b"\xff\x7f" * 100) == pytest.approx(1, abs=0.01)
    assert rms_level(b"") == 0


# MARK: StatusNotifierItem, against a fake watcher on a private bus


@pytest.fixture
def bus():
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        pytest.skip("needs a session bus")
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


WATCHER_XML = """<node><interface name="org.kde.StatusNotifierWatcher"><method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method></interface></node>"""


def test_sni_registers_and_serves_properties(bus):
    registered = []

    def method(_c, sender, _p, _i, _m, params, invocation):
        registered.append((sender, params.unpack()[0]))
        invocation.return_value(None)

    registration = bus.register_object(T.WATCHER_PATH, Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0], method, None)
    owner = Gio.bus_own_name_on_connection(bus, T.WATCHER_BUS, Gio.BusNameOwnerFlags.NONE, None, None)
    assert run_until(lambda: T.has_watcher(bus), 2)
    tray = T.SniTray(bus)
    clicked = []
    tray.on_activate = lambda x, y: clicked.append((x, y))
    menu_hits = []
    tray.menu_items = [("Open", lambda: menu_hits.append(1)), (None, None), ("Quit", lambda: menu_hits.append(2))]
    try:
        assert run_until(lambda: registered and tray.is_visible(), 3)
        name = tray.service
        assert registered[0][1] == name

        def call(path, iface, method_name, args, reply=None):
            # Asynchronous, because the service answering lives on this same main loop.
            results = []
            bus.call(name, path, iface, method_name, args, reply, Gio.DBusCallFlags.NONE, 2000, None, lambda c, r: results.append(c.call_finish(r)))
            assert run_until(lambda: results, 3)
            return results[0]

        def get(prop):
            return call(T.ITEM_PATH, "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", ("org.kde.StatusNotifierItem", prop))).unpack()[0]

        assert get("Id") == "barclaw" and get("Status") == "Active" and get("Menu") == T.MENU_PATH
        pixmaps = get("IconPixmap")
        assert [p[0] for p in pixmaps] == [22, 32, 48] and len(pixmaps[0][2]) == 22 * 22 * 4

        tray.update(T.TrayState(mode="attention_reply", tooltip="done"))
        assert get("Status") == "NeedsAttention" and get("ToolTip")[3] == "done"

        call(T.ITEM_PATH, "org.kde.StatusNotifierItem", "Activate", GLib.Variant("(ii)", (120, 8)))
        assert run_until(lambda: clicked == [(120, 8)], 2)

        layout = call(T.MENU_PATH, "com.canonical.dbusmenu", "GetLayout", GLib.Variant("(iias)", (0, -1, [])), GLib.VariantType("(u(ia{sv}av))")).unpack()
        children = layout[1][2]
        assert [c[1].get("label", c[1].get("type")) for c in children] == ["Open", "separator", "Quit"]
        call(T.MENU_PATH, "com.canonical.dbusmenu", "Event", GLib.Variant("(isvu)", (3, "clicked", GLib.Variant("s", ""), 0)))
        assert run_until(lambda: menu_hits == [2], 2)
    finally:
        tray.close()
        Gio.bus_unown_name(owner)
        bus.unregister_object(registration)
