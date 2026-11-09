"""Charts the agent describes as JSON inside a ```chart fence, drawn with Cairo.

    {"type": "bar|line|area|point|pie", "title": "…", "x": "label", "y": "label", "unit": "$",
     "data": [{"x": "Mon", "y": 3, "series": "optional"}]}
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Optional

import cairo
from gi.repository import Gtk, Pango, PangoCairo

from ..support import number_of, text_of

COLORS = [(0.55, 0.42, 1.0), (0.25, 0.75, 1.0), (1.0, 0.36, 0.62), (0.3, 0.88, 0.62), (1.0, 0.7, 0.25), (0.95, 0.95, 1.0)]
KINDS = ("bar", "line", "area", "point", "pie")


@dataclass
class Point:
    index: int
    x: str
    x_number: Optional[float]
    y: float
    series: Optional[str]


@dataclass
class ChartSpec:
    kind: str
    title: Optional[str]
    x_label: Optional[str]
    y_label: Optional[str]
    unit: Optional[str]
    points: list

    @property
    def has_series(self) -> bool:
        return any(p.series is not None for p in self.points)

    @property
    def series_names(self) -> list:
        names: list = []
        for point in self.points:
            name = point.series or "—"
            if name not in names:
                names.append(name)
        return names

    @property
    def categories(self) -> list:
        names: list = []
        for point in self.points:
            if point.x not in names:
                names.append(point.x)
        return names


def format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6g}"


def parse_chart(source: str) -> Optional[ChartSpec]:
    try:
        payload: Any = json.loads(source)
    except ValueError:
        return None
    if isinstance(payload, list):
        payload = {"data": payload}
    if not isinstance(payload, dict):
        return None
    rows = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("points")
    points = []
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        x_value = next((row[k] for k in ("x", "label", "name", "category") if k in row), None)
        y = number_of(next((row[k] for k in ("y", "value", "count") if k in row), None))
        if y is None:
            continue
        is_string = isinstance(x_value, str)
        x_number = None if is_string else number_of(x_value)
        x_text = x_value if is_string else (format_number(x_number) if x_number is not None else str(index + 1))
        points.append(Point(index, x_text, x_number, y, text_of(row.get("series")) or text_of(row.get("group"))))
    if not points:
        return None
    kind = str(payload.get("type") or payload.get("kind") or "bar").lower()
    return ChartSpec(
        kind if kind in KINDS else "bar",
        text_of(payload.get("title")),
        text_of(payload.get("x")) or text_of(payload.get("xLabel")),
        text_of(payload.get("y")) or text_of(payload.get("yLabel")),
        text_of(payload.get("unit")),
        points,
    )


def compact_number(value: float) -> str:
    magnitude = abs(value)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if magnitude >= limit:
            text = f"{value / limit:.1f}".rstrip("0").rstrip(".")
            return text + suffix
    return format_number(round(value, 2))


def nice_ticks(low: float, high: float, count: int = 4) -> list:
    if high <= low:
        high = low + 1
    raw = (high - low) / count
    power = 10 ** math.floor(math.log10(raw))
    step = next((m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw), 10 * power)
    start = math.floor(low / step) * step
    ticks, value = [], start
    while value < high + step * 0.999:
        ticks.append(round(value, 10))
        value += step
    return ticks


class ChartWidget(Gtk.DrawingArea):
    def __init__(self, spec: ChartSpec, size: float = 13.0) -> None:
        super().__init__()
        self.spec = spec
        self.text_size = size
        self.set_size_request(240, 212 if spec.kind == "pie" else 192)
        self.set_hexpand(True)
        self.connect("draw", self._on_draw)
        self.set_tooltip_text(spec.title or "Chart")

    def _on_draw(self, _widget: Gtk.Widget, c: cairo.Context) -> bool:
        allocation = self.get_allocation()
        paint_chart(c, self.spec, allocation.width, allocation.height, self.text_size)
        return False


def _text(c: cairo.Context, text: str, x: float, y: float, size: float, color: tuple, anchor: float = 0.0, bold: bool = False, max_width: Optional[float] = None) -> float:
    layout = PangoCairo.create_layout(c)
    description = Pango.FontDescription()
    description.set_family("sans-serif")
    description.set_absolute_size(size * Pango.SCALE)
    if bold:
        description.set_weight(Pango.Weight.SEMIBOLD)
    layout.set_font_description(description)
    layout.set_text(text, -1)
    if max_width is not None:
        layout.set_width(int(max_width * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
    width, height = layout.get_pixel_size()
    c.set_source_rgba(*color)
    c.move_to(x - width * anchor, y - height / 2)
    PangoCairo.show_layout(c, layout)
    return width


def _rounded_rect(c: cairo.Context, x: float, y: float, w: float, h: float, r: float) -> None:
    r = min(r, w / 2, h / 2)
    c.new_sub_path()
    c.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    c.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    c.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    c.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    c.close_path()


def _catmull_rom(c: cairo.Context, points: list) -> None:
    for i in range(len(points) - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1, p2 = points[i], points[i + 1]
        p3 = points[i + 2] if i + 2 < len(points) else p2
        c.curve_to(
            p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6,
            p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6,
            p2[0], p2[1],
        )


def paint_chart(c: cairo.Context, spec: ChartSpec, width: float, height: float, size: float = 13.0) -> None:
    label = size * 0.76
    muted = (1, 1, 1, 0.55)
    top = 0.0
    if spec.title:
        _text(c, spec.title, 0, size * 0.75, size, (1, 1, 1, 0.92), bold=True, max_width=width)
        top = size * 1.9

    if spec.kind == "pie":
        _paint_pie(c, spec, width, height, top, label)
        return

    series = spec.series_names if spec.has_series else [spec.y_label or "y"]
    legend_height = 0.0
    if spec.has_series:
        legend_height = label * 2.0
        x = 0.0
        for k, name in enumerate(series):
            color = COLORS[k % len(COLORS)]
            c.set_source_rgba(*color, 1)
            c.arc(x + 4, height - legend_height / 2, 4, 0, 2 * math.pi)
            c.fill()
            x += 12 + _text(c, name, x + 12, height - legend_height / 2, label, muted) + 14

    values = [p.y for p in spec.points]
    low = min(0.0, min(values)) if spec.kind in ("bar", "area") else min(values)
    high = max(max(values), 0.0 if spec.kind in ("bar", "area") else min(values))
    if low == high:
        low, high = low - 1, high + 1
    ticks = nice_ticks(low, high)
    y_min, y_max = min(ticks), max(ticks)
    prefix = spec.unit or ""

    measure = max(_measure(c, f"{prefix}{compact_number(t)}", label) for t in ticks)
    left = measure + 8
    right = 6.0
    bottom = height - legend_height - label * 1.8 - (label * 1.2 if spec.x_label else 0)
    plot_top = top + 6
    plot_h = max(20.0, bottom - plot_top)
    plot_w = max(20.0, width - left - right)

    def Y(value: float) -> float:
        return plot_top + plot_h * (1 - (value - y_min) / (y_max - y_min))

    for tick in ticks:
        c.set_source_rgba(1, 1, 1, 0.08)
        c.set_line_width(1)
        c.move_to(left, Y(tick) + 0.5)
        c.line_to(left + plot_w, Y(tick) + 0.5)
        c.stroke()
        _text(c, f"{prefix}{compact_number(tick)}", left - 6, Y(tick), label, muted, anchor=1.0)

    categories = spec.categories
    band = plot_w / max(len(categories), 1)

    def category_x(index: int) -> float:
        return left + band * (index + 0.5)

    stride = max(1, math.ceil(len(categories) * (label * 3.4) / plot_w))
    for index, name in enumerate(categories):
        if index % stride == 0:
            text = name if len(name) <= 10 else name[:9] + "…"
            _text(c, text, category_x(index), bottom + label * 0.95, label, muted, anchor=0.5)
    if spec.x_label:
        _text(c, spec.x_label, left + plot_w, bottom + label * 2.25, label, muted, anchor=1.0)

    by_series: dict = {}
    for point in spec.points:
        by_series.setdefault(point.series or "—" if spec.has_series else series[0], []).append(point)

    if spec.kind == "bar":
        groups = max(len(by_series), 1)
        bar_width = min(band * 0.72 / groups, 46.0)
        zero = Y(max(y_min, 0.0))
        for k, name in enumerate(by_series):
            for point in by_series[name]:
                index = categories.index(point.x)
                color = COLORS[(k if spec.has_series else index) % len(COLORS)]
                x = category_x(index) - groups * bar_width / 2 + k * bar_width
                y = min(Y(point.y), zero)
                h = max(abs(Y(point.y) - zero), 1)
                c.set_source_rgba(*color, 1)
                _rounded_rect(c, x + 1, y, bar_width - 2, h, 3)
                c.fill()
        return

    for k, name in enumerate(by_series):
        color = COLORS[k % len(COLORS)]
        coords = [(category_x(categories.index(p.x)), Y(p.y)) for p in by_series[name]]
        if spec.kind == "point":
            c.set_source_rgba(*color, 1)
            for x, y in coords:
                c.arc(x, y, 3.5, 0, 2 * math.pi)
                c.fill()
            continue
        if spec.kind == "area" and len(coords) > 1:
            c.move_to(*coords[0])
            _catmull_rom(c, coords)
            c.line_to(coords[-1][0], Y(max(y_min, 0.0)))
            c.line_to(coords[0][0], Y(max(y_min, 0.0)))
            c.close_path()
            c.set_source_rgba(*color, 0.62)
            c.fill()
        c.set_source_rgba(*color, 1)
        c.set_line_width(2)
        c.set_line_join(cairo.LINE_JOIN_ROUND)
        c.move_to(*coords[0])
        _catmull_rom(c, coords)
        c.stroke()
        if spec.kind == "line":
            for x, y in coords:
                c.arc(x, y, 2.8, 0, 2 * math.pi)
                c.fill()


def _measure(c: cairo.Context, text: str, size: float) -> float:
    layout = PangoCairo.create_layout(c)
    description = Pango.FontDescription()
    description.set_family("sans-serif")
    description.set_absolute_size(size * Pango.SCALE)
    layout.set_font_description(description)
    layout.set_text(text, -1)
    return layout.get_pixel_size()[0]


def _paint_pie(c: cairo.Context, spec: ChartSpec, width: float, height: float, top: float, label: float) -> None:
    total = sum(max(p.y, 0) for p in spec.points) or 1.0
    legend_width = min(width * 0.42, 170.0)
    radius = min((width - legend_width - 12) / 2, (height - top - 4) / 2)
    cx, cy = radius + 2, top + (height - top) / 2
    angle = -math.pi / 2
    inset = 1.5 / max(radius, 1)
    for index, point in enumerate(spec.points):
        sweep = max(point.y, 0) / total * 2 * math.pi
        c.set_source_rgba(*COLORS[index % len(COLORS)], 1)
        c.arc(cx, cy, radius, angle + inset, angle + sweep - inset)
        c.arc_negative(cx, cy, radius * 0.55, angle + sweep - inset * 1.8, angle + inset * 1.8)
        c.close_path()
        c.fill()
        angle += sweep
    rows = spec.points[: max(1, int((height - top) / (label * 1.7)))]
    y = cy - (len(rows) - 1) * label * 0.85
    x = cx + radius + 16
    for index, point in enumerate(rows):
        c.set_source_rgba(*COLORS[index % len(COLORS)], 1)
        c.arc(x + 4, y, 4, 0, 2 * math.pi)
        c.fill()
        percent = point.y / total * 100
        _text(c, f"{point.x} · {percent:.0f}%", x + 14, y, label, (1, 1, 1, 0.75), max_width=width - x - 16)
        y += label * 1.7
