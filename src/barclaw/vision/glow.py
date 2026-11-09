"""A click-through glow around the screen edges while any capture runs."""
from __future__ import annotations

import math
from typing import Optional

import cairo
from gi.repository import Gdk, GLib, Gtk

from ..bar.theme import SIRI

EDGE = 14


class _Strip(Gtk.Window):
    def __init__(self, x: int, y: int, width: int, height: int, side: str) -> None:
        super().__init__(type=Gtk.WindowType.POPUP)
        self.side = side
        self.phase = 0.0
        self.set_app_paintable(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        self.composited = visual is not None and screen.is_composited()
        if self.composited:
            self.set_visual(visual)
        self.set_default_size(width, height)
        self.move(x, y)
        self.connect("draw", self._draw)
        self.connect("realize", self._make_click_through)

    def _make_click_through(self, _widget: Gtk.Widget) -> None:
        self.input_shape_combine_region(cairo.Region())

    def _draw(self, _widget: Gtk.Widget, c: cairo.Context) -> bool:
        allocation = self.get_allocation()
        width, height = allocation.width, allocation.height
        horizontal = self.side in ("top", "bottom")
        length = width if horizontal else height
        if horizontal:
            gradient = cairo.LinearGradient(0, 0, length, 0)
        else:
            gradient = cairo.LinearGradient(0, 0, 0, length)
        for i in range(9):
            r, g, b = SIRI[int(i + self.phase * 5) % 5]
            gradient.add_color_stop_rgba(i / 8, r, g, b, 0.95)
        c.set_operator(cairo.OPERATOR_SOURCE)
        if not self.composited:
            c.set_source(gradient)
            c.paint()
            return False
        c.set_source_rgba(0, 0, 0, 0)
        c.paint()
        c.set_source(gradient)
        fade = {
            "top": cairo.LinearGradient(0, 0, 0, height),
            "bottom": cairo.LinearGradient(0, height, 0, 0),
            "left": cairo.LinearGradient(0, 0, width, 0),
            "right": cairo.LinearGradient(width, 0, 0, 0),
        }[self.side]
        fade.add_color_stop_rgba(0, 0, 0, 0, 1)
        fade.add_color_stop_rgba(1, 0, 0, 0, 0)
        c.mask(fade)
        return False


class ScreenEdgeGlow:
    def __init__(self) -> None:
        self._strips: list = []
        self._source = 0
        self._phase = 0.0

    def begin(self, monitor: Gdk.Monitor) -> None:
        self.end()
        area = monitor.get_geometry()
        x, y, w, h = area.x, area.y, area.width, area.height
        self._strips = [
            _Strip(x, y, w, EDGE, "top"),
            _Strip(x, y + h - EDGE, w, EDGE, "bottom"),
            _Strip(x, y + EDGE, EDGE, h - 2 * EDGE, "left"),
            _Strip(x + w - EDGE, y + EDGE, EDGE, h - 2 * EDGE, "right"),
        ]
        for strip in self._strips:
            strip.show()
        self._source = GLib.timeout_add(33, self._tick)

    def _tick(self) -> bool:
        self._phase = (self._phase + 0.02) % 1.0
        for strip in self._strips:
            strip.phase = self._phase
            strip.queue_draw()
        return GLib.SOURCE_CONTINUE

    def end(self) -> None:
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0
        for strip in self._strips:
            strip.destroy()
        self._strips = []
