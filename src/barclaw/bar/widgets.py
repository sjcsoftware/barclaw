"""Small custom-drawn widgets: the agent stage, meters, and the speech waveform."""
from __future__ import annotations

import math
import time
from typing import Callable, Optional

import cairo
from gi.repository import Gtk, Pango, PangoCairo

from .theme import SIRI


def animations_enabled() -> bool:
    return bool(Gtk.Settings.get_default().get_property("gtk-enable-animations"))


class Animated(Gtk.DrawingArea):
    """Redraws every frame only while `animating()` holds and it's on screen."""

    def __init__(self) -> None:
        super().__init__()
        self._tick_id = 0
        self.connect("draw", self._on_draw)
        self.connect("map", lambda _w: self.sync_animation())
        self.connect("unmap", lambda _w: self._stop())

    def animating(self) -> bool:
        return False

    def sync_animation(self) -> None:
        if self.animating() and animations_enabled() and self.get_mapped():
            if not self._tick_id:
                self._tick_id = self.add_tick_callback(self._tick)
        else:
            self._stop()
        self.queue_draw()

    def _tick(self, _widget: Gtk.Widget, _clock: object) -> bool:
        self.queue_draw()
        return True

    def _stop(self) -> None:
        if self._tick_id:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = 0

    def _on_draw(self, _widget: Gtk.Widget, c: cairo.Context) -> bool:
        allocation = self.get_allocation()
        self.paint(c, allocation.width, allocation.height, time.monotonic())
        return False

    def paint(self, c: cairo.Context, width: int, height: int, t: float) -> None:
        raise NotImplementedError


def _conic(c: cairo.Context, cx: float, cy: float, radius: float, width: float, start: float, sweep: float, palette: list, alpha: float = 1.0) -> None:
    c.set_line_width(width)
    c.set_line_cap(cairo.LINE_CAP_ROUND)
    steps = max(8, int(sweep * 12))
    for i in range(steps):
        position = i / steps * (len(palette) - 1)
        low = int(position)
        high = min(low + 1, len(palette) - 1)
        mix = position - low
        color = [palette[low][j] + (palette[high][j] - palette[low][j]) * mix for j in range(3)]
        c.set_source_rgba(*color, alpha)
        c.arc(cx, cy, radius, start + sweep * i / steps, start + sweep * (i + 1) / steps + 0.02)
        c.stroke()


class AgentStage(Animated):
    """The big emoji. Ripples with your voice while listening, orbits while thinking, floats when idle."""

    def __init__(self, on_click: Callable[[], None]) -> None:
        super().__init__()
        self.set_size_request(108, 108)
        self.emoji = "🦞"
        self.mode = "idle"  # idle | listening | thinking
        self.level = 0.0
        self.connected = True
        self.hovering = False
        self.add_events(0x100 | 0x1000 | 0x2000)  # BUTTON_PRESS, ENTER, LEAVE
        self.connect("button-press-event", lambda _w, _e: (on_click(), True)[1])
        self.connect("enter-notify-event", self._hover, True)
        self.connect("leave-notify-event", self._hover, False)

    def _hover(self, _widget: Gtk.Widget, _event: object, value: bool) -> bool:
        self.hovering = value
        self.queue_draw()
        return False

    def animating(self) -> bool:
        return True

    def paint(self, c: cairo.Context, width: int, height: int, t: float) -> None:
        cx, cy = width / 2, height / 2
        if self.mode == "listening":
            glow = cairo.RadialGradient(cx, cy, 6, cx, cy, 54)
            glow.add_color_stop_rgba(0, *SIRI[1], 0.25 + 0.35 * self.level)
            glow.add_color_stop_rgba(1, *SIRI[3], 0)
            c.set_source(glow)
            c.paint()
            for i in range(3):
                phase = (t * 0.9 + i / 3) % 1
                radius = 30 * (0.8 + phase * (0.6 + self.level * 0.3))
                _conic(c, cx, cy, radius, 1.5 + self.level * 2.5, t * 2, 2 * math.pi, SIRI + [SIRI[0]], (1 - phase) * (0.5 + 0.5 * self.level))
        elif self.mode == "thinking":
            _conic(c, cx, cy, 42, 3, (t * 3.8) % (2 * math.pi), 4.08, SIRI)
            _conic(c, cx, cy, 35, 2, -(t * 2.8) % (2 * math.pi), 2.2, list(reversed(SIRI)), 0.8)
        else:
            glow = cairo.RadialGradient(cx, cy, 4, cx, cy, 54)
            glow.add_color_stop_rgba(0, *SIRI[1], 0.32 if self.hovering else 0.12)
            glow.add_color_stop_rgba(1, *SIRI[1], 0)
            c.set_source(glow)
            c.paint()

        float_offset = math.sin(t * 1.65) * 2.5 if self.mode != "listening" and animations_enabled() else 0
        scale = 1 + self.level * 0.25 if self.mode == "listening" else (1.06 if self.hovering else 1)
        layout = PangoCairo.create_layout(c)
        description = Pango.FontDescription()
        description.set_family("Noto Color Emoji, Apple Color Emoji, Twemoji, sans-serif")
        description.set_absolute_size(46 * Pango.SCALE)
        layout.set_font_description(description)
        layout.set_text(self.emoji, -1)
        ew, eh = layout.get_pixel_size()
        c.save()
        c.translate(cx, cy + float_offset)
        c.scale(scale, scale)
        c.move_to(-ew / 2, -eh / 2)
        if self.connected:
            PangoCairo.show_layout(c, layout)
        else:
            c.push_group()
            PangoCairo.show_layout(c, layout)
            pattern = c.pop_group()
            c.set_source_rgba(0.7, 0.7, 0.7, 0.55)
            c.mask(pattern)
        c.restore()
        if not self.connected:
            c.set_source_rgba(0.9, 0.3, 0.25, 1)
            c.arc(cx + 26, cy + 26, 9, 0, 2 * math.pi)
            c.fill()
            c.set_source_rgba(1, 1, 1, 1)
            c.set_line_width(2)
            c.set_line_cap(cairo.LINE_CAP_ROUND)
            c.move_to(cx + 22.5, cy + 22.5)
            c.line_to(cx + 29.5, cy + 29.5)
            c.move_to(cx + 29.5, cy + 22.5)
            c.line_to(cx + 22.5, cy + 29.5)
            c.stroke()


class ContextRing(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__()
        self.fraction = 0.0
        self.set_size_request(14, 14)
        self.connect("draw", self._draw)

    def _draw(self, _widget: Gtk.Widget, c: cairo.Context) -> bool:
        c.set_line_width(2)
        c.set_source_rgba(1, 1, 1, 0.16)
        c.arc(7, 7, 5.5, 0, 2 * math.pi)
        c.stroke()
        c.set_source_rgba(*(SIRI[0] if self.fraction >= 0.8 else SIRI[3]), 1)
        c.arc(7, 7, 5.5, -math.pi / 2, -math.pi / 2 + self.fraction * 2 * math.pi)
        c.stroke()
        return False


class UsageBar(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__()
        self.fraction = 0.0
        self.set_size_request(46, 6)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self._draw)

    def _draw(self, _widget: Gtk.Widget, c: cairo.Context) -> bool:
        allocation = self.get_allocation()
        w, h = allocation.width, allocation.height
        c.set_source_rgba(1, 1, 1, 0.14)
        self._pill(c, 0, 0, w, h)
        c.fill()
        if self.fraction > 0:
            gradient = cairo.LinearGradient(0, 0, w, 0)
            gradient.add_color_stop_rgb(0, *SIRI[3])
            gradient.add_color_stop_rgb(1, *(SIRI[0] if self.fraction >= 0.85 else SIRI[2]))
            c.set_source(gradient)
            self._pill(c, 0, 0, max(h, w * self.fraction), h)
            c.fill()
        return False

    @staticmethod
    def _pill(c: cairo.Context, x: float, y: float, w: float, h: float) -> None:
        r = h / 2
        c.new_sub_path()
        c.arc(x + w - r, y + r, r, -math.pi / 2, math.pi / 2)
        c.arc(x + r, y + r, r, math.pi / 2, 3 * math.pi / 2)
        c.close_path()


class Waveform(Animated):
    def __init__(self, bars: int = 4) -> None:
        super().__init__()
        self.bars = bars
        self.level = 0.0
        self.set_size_request(bars * 6, 18)

    def animating(self) -> bool:
        return True

    def paint(self, c: cairo.Context, width: int, height: int, t: float) -> None:
        c.set_line_width(3)
        c.set_line_cap(cairo.LINE_CAP_ROUND)
        for i in range(self.bars):
            wobble = 0.4 + 0.6 * abs(math.sin(t * 7 + i * 1.3))
            bar = max(3.0, (height - 4) * (0.2 + 0.8 * self.level) * wobble)
            x = 3 + i * (width - 6) / max(1, self.bars - 1)
            c.set_source_rgba(*SIRI[i % 5], 1)
            c.move_to(x, height / 2 - bar / 2)
            c.line_to(x, height / 2 + bar / 2)
            c.stroke()


class ThinkingDots(Animated):
    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(26, 12)

    def animating(self) -> bool:
        return True

    def paint(self, c: cairo.Context, width: int, height: int, t: float) -> None:
        for i in range(3):
            phase = 0.5 + 0.5 * math.sin(t * 5 - i * 0.9)
            c.set_source_rgba(*SIRI[i + 1], 0.35 + 0.65 * phase)
            c.arc(5 + i * 8, height / 2, 2.2 + phase * 0.9, 0, 2 * math.pi)
            c.fill()
