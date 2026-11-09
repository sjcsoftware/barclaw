"""Renders LaTeX math with a small TeX-style box layout on Cairo and Pango.

Coordinates here are y-up relative to a baseline origin (matching TeX), and `MathBox.paint`
converts to Cairo's y-down at the moment of drawing.
"""
from __future__ import annotations

import math as _math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Optional

import cairo
from gi.repository import Pango, PangoCairo

from .mathparser import (
    BIN, CLOSE, DISPLAY, INNER, OP, OPEN, ORD, PUNCT, REL, SCRIPT, SCRIPTSCRIPT, TEXT, MathParser, fraction_of, script_of,
)

MATH_FAMILY = "STIX Two Math, Latin Modern Math, TeX Gyre Termes Math, DejaVu Math TeX Gyre, Noto Sans Math, DejaVu Serif, serif"
TEXT_FAMILY = "STIX Two Text, Latin Modern Roman, TeX Gyre Termes, DejaVu Serif, serif"
UI_FAMILY = "sans-serif"
INTEGRALS = "∫∬∭∮∯"

Draw = Callable[[cairo.Context, float, float], None]


def _nothing(_ctx: cairo.Context, _x: float, _y: float) -> None:
    pass


@dataclass
class MathBox:
    """A TeX box: a width, a height above the baseline, a depth below it, and how to draw it."""

    width: float = 0.0
    ascent: float = 0.0
    descent: float = 0.0
    draw: Draw = field(default=_nothing)

    def raised(self, dy: float) -> "MathBox":
        draw = self.draw
        return MathBox(self.width, self.ascent + dy, self.descent - dy, lambda c, x, y: draw(c, x, y + dy))

    @classmethod
    def row(cls, boxes: list) -> "MathBox":
        offsets, x = [], 0.0
        for box in boxes:
            offsets.append(x)
            x += box.width

        def draw(c: cairo.Context, px: float, py: float) -> None:
            for box, dx in zip(boxes, offsets):
                box.draw(c, px + dx, py)

        return cls(x, max((b.ascent for b in boxes), default=0.0), max((b.descent for b in boxes), default=0.0), draw)


def kern(width: float) -> MathBox:
    return MathBox(width=width)


# TeX's inter-atom spacing table, in mu (1/18 em). Negative entries apply only outside scripts.
_SPACING = [
    # ord op bin rel open close punct inner
    [0, 3, -4, -5, 0, 0, 0, -3],
    [3, 3, 0, -5, 0, 0, 0, -3],
    [-4, -4, 0, 0, -4, 0, 0, -4],
    [-5, -5, 0, 0, -5, 0, 0, -5],
    [0, 0, 0, 0, 0, 0, 0, 0],
    [0, 3, -4, -5, 0, 0, 0, -3],
    [-3, -3, 0, -3, -3, -3, -3, -3],
    [-3, 3, -4, -5, -3, 0, -3, -3],
]

_measure_surface = cairo.ImageSurface(cairo.FORMAT_A8, 1, 1)
_measure_context = cairo.Context(_measure_surface)


class _Typesetter:
    def __init__(self, size: float) -> None:
        self.size = size

    def font_size(self, style: int) -> float:
        if style <= TEXT:
            return self.size
        if style == SCRIPT:
            return max(self.size * 0.71, 6.5)
        return max(self.size * 0.55, 5.5)

    @staticmethod
    def axis(s: float) -> float:
        return s * 0.25

    @staticmethod
    def rule(s: float) -> float:
        return max(s * 0.066, 0.6)

    # MARK: Lists and spacing

    def layout(self, nodes: list, style: int) -> MathBox:
        classes = [self._class_of(n) for n in nodes]
        # A binary operator with nothing to bind on one side acts as an ordinary symbol (TeX rule 5/6).
        previous = None
        for k, cls in enumerate(classes):
            if cls is None:
                continue
            if cls == BIN and (previous is None or classes[previous] in (BIN, OP, REL, OPEN, PUNCT)):
                classes[k] = ORD
            if cls in (REL, CLOSE, PUNCT) and previous is not None and classes[previous] == BIN:
                classes[previous] = ORD
            previous = k
        if previous is not None and classes[previous] == BIN:
            classes[previous] = ORD

        boxes, last = [], None
        for node, cls in zip(nodes, classes):
            if cls is not None:
                if last is not None:
                    boxes.append(kern(self._spacing(last, cls, style)))
                last = cls
            boxes.append(self.box(node, style))
        return MathBox.row(boxes)

    @classmethod
    def _class_of(cls, node: tuple) -> Optional[int]:
        kind = node[0]
        if kind in ("symbol", "group"):
            return node[2]
        if kind == "delimiter":
            return node[3]
        if kind == "stack":
            return node[4]
        if kind in ("large_op", "function"):
            return OP
        if kind == "scripts":
            inner = cls._class_of(node[1])
            return ORD if inner is None else inner
        if kind in ("fraction", "fenced"):
            return INNER
        if kind == "space":
            return None
        return ORD

    def _spacing(self, left: int, right: int, style: int) -> float:
        mu = _SPACING[left][right]
        if mu < 0 and style >= SCRIPT:
            return 0.0
        return abs(mu) * self.font_size(style) / 18

    # MARK: Atoms

    def box(self, node: tuple, style: int) -> MathBox:
        s = self.font_size(style)
        kind = node[0]
        if kind == "symbol":
            return self.glyphs(node[1], MATH_FAMILY, s)
        if kind == "text":
            text, bold, italic = node[1:]
            # Math fonts have no Hangul/CJK; use the UI font for those.
            if any(ord(c) >= 0x1100 and not 0x2000 <= ord(c) <= 0x2BFF for c in text):
                return self.glyphs(text, UI_FAMILY, s * 0.92, bold=bold)
            return self.glyphs(text, TEXT_FAMILY, s, bold=bold, italic=italic)
        if kind == "function":
            return self.glyphs(node[1], TEXT_FAMILY, s)
        if kind == "large_op":
            glyph = node[1]
            integral = glyph in INTEGRALS
            scale = (1.75 if integral else 1.45) if style == DISPLAY else (1.2 if integral else 1.05)
            op = self.glyphs(glyph, MATH_FAMILY, s * scale)
            return op.raised(self.axis(s) - (op.ascent - op.descent) / 2)
        if kind == "group":
            return self.layout(node[1], style)
        if kind == "scripts":
            return self.scripts(node[1], node[2], node[3], style)
        if kind == "fraction":
            return self.fraction(node[1], node[2], node[3], node[4] if node[4] is not None else style)
        if kind == "radical":
            return self.radical(node[1], node[2], style)
        if kind == "fenced":
            return self.fence(self.layout(node[2], style), node[1], node[3], style)
        if kind == "delimiter":
            return self.delimiter(node[1], node[2] * s, style)
        if kind == "accent":
            return self.accent(node[1], node[2], style)
        if kind == "stack":
            base, over, under = node[1], node[2], node[3]
            script = script_of(style)
            return self.limits(self.layout(base, style), self.layout(over, script) if over else None, self.layout(under, script) if under else None, s)
        if kind == "boxed":
            return self.boxed(self.layout(node[1], style), s)
        if kind == "array":
            return self.array(node[1], node[2], node[3], style)
        if kind == "space":
            return kern(node[1] * s)
        if kind == "phantom":
            box = self.layout(node[1], style)
            return MathBox(box.width, box.ascent, box.descent)
        if kind == "styled":
            return self.layout(node[2], node[1])
        return MathBox()

    def glyphs(self, text: str, family: str, size: float, bold: bool = False, italic: bool = False) -> MathBox:
        layout = PangoCairo.create_layout(_measure_context)
        description = Pango.FontDescription()
        description.set_family(family)
        description.set_absolute_size(size * Pango.SCALE)
        if bold:
            description.set_weight(Pango.Weight.BOLD)
        if italic:
            description.set_style(Pango.Style.ITALIC)
        layout.set_font_description(description)
        layout.set_text(text, -1)
        ink, logical = layout.get_pixel_extents()
        baseline = layout.get_baseline() / Pango.SCALE
        empty = ink.width == 0 or ink.height == 0
        ascent = 0.0 if empty else max(0.0, baseline - ink.y)
        descent = 0.0 if empty else max(0.0, ink.y + ink.height - baseline)

        def draw(c: cairo.Context, x: float, y: float) -> None:
            c.move_to(x, -y - baseline)
            PangoCairo.update_layout(c, layout)
            PangoCairo.show_layout(c, layout)

        return MathBox(logical.width, ascent, descent, draw)

    @staticmethod
    def _is_glyph(node: tuple) -> bool:
        return node[0] in ("symbol", "function", "text")

    # MARK: Scripts and limits

    def scripts(self, base_node: tuple, sub: Optional[list], sup: Optional[list], style: int) -> MathBox:
        s = self.font_size(style)
        base = self.box(base_node, style)
        script = script_of(style)
        sup_box = self.layout(sup, script) if sup is not None else None
        sub_box = self.layout(sub, script) if sub is not None else None
        if self._uses_limits(base_node, style):
            return self.limits(base, sup_box, sub_box, s)

        glyph = self._is_glyph(base_node)
        script_size = self.font_size(script)
        sup_shift = sub_shift = 0.0
        if sup_box:
            sup_shift = max(0.41 * s if style == DISPLAY else 0.36 * s, 0 if glyph else base.ascent - 0.39 * script_size, sup_box.descent + 0.11 * s)
        if sub_box:
            sub_shift = max(0.17 * s, 0 if glyph else base.descent + 0.05 * script_size, sub_box.ascent - 0.38 * s)
        if sup_box and sub_box:
            sub_shift = max(sub_shift, 0.25 * s)
            gap = (sup_shift - sup_box.descent) - (sub_box.ascent - sub_shift)
            if gap < 4 * self.rule(s):
                sub_shift += 4 * self.rule(s) - gap
        # Integrals tuck the subscript under their slant.
        sub_kern = -0.2 * s if base_node[0] == "large_op" and base_node[1] in INTEGRALS else 0.0
        script_width = max(sup_box.width if sup_box else 0, (sub_box.width if sub_box else 0) + sub_kern)
        width = base.width + script_width + 0.05 * s
        ascent = max(base.ascent, sup_shift + sup_box.ascent if sup_box else 0)
        descent = max(base.descent, sub_shift + sub_box.descent if sub_box else 0)

        def draw(c: cairo.Context, x: float, y: float) -> None:
            base.draw(c, x, y)
            if sup_box:
                sup_box.draw(c, x + base.width, y + sup_shift)
            if sub_box:
                sub_box.draw(c, x + base.width + sub_kern, y - sub_shift)

        return MathBox(width, ascent, descent, draw)

    @staticmethod
    def _uses_limits(node: tuple, style: int) -> bool:
        if node[0] in ("large_op", "function"):
            return node[2] if node[2] is not None else style == DISPLAY
        return node[0] == "accent" and node[2] in ("overbrace", "underbrace")

    @staticmethod
    def limits(base: MathBox, sup: Optional[MathBox], sub: Optional[MathBox], s: float) -> MathBox:
        gap = 0.14 * s
        width = max(base.width, sup.width if sup else 0, sub.width if sub else 0)
        sup_baseline = base.ascent + gap + (sup.descent if sup else 0)
        sub_baseline = base.descent + gap + (sub.ascent if sub else 0)
        ascent = sup_baseline + sup.ascent if sup else base.ascent
        descent = sub_baseline + sub.descent if sub else base.descent

        def draw(c: cairo.Context, x: float, y: float) -> None:
            base.draw(c, x + (width - base.width) / 2, y)
            if sup:
                sup.draw(c, x + (width - sup.width) / 2, y + sup_baseline)
            if sub:
                sub.draw(c, x + (width - sub.width) / 2, y - sub_baseline)

        return MathBox(width, ascent, descent, draw)

    # MARK: Fractions and radicals

    def fraction(self, num_nodes: list, den_nodes: list, has_rule: bool, style: int) -> MathBox:
        s = self.font_size(style)
        inner = fraction_of(style)
        num, den = self.layout(num_nodes, inner), self.layout(den_nodes, inner)
        display = style == DISPLAY
        thickness = self.rule(s) if has_rule else 0.0
        gap = 3 * self.rule(s) if display else self.rule(s) * 1.2
        a = self.axis(s)
        num_shift = max(0.68 * s if display else 0.4 * s, a + thickness / 2 + gap + num.descent)
        den_shift = max(0.69 * s if display else 0.34 * s, den.ascent + gap + thickness / 2 - a)
        pad = 0.12 * s
        width = max(num.width, den.width) + 2 * pad

        def draw(c: cairo.Context, x: float, y: float) -> None:
            num.draw(c, x + (width - num.width) / 2, y + num_shift)
            den.draw(c, x + (width - den.width) / 2, y - den_shift)
            if has_rule:
                c.rectangle(x + pad / 2, -(y + a + thickness / 2), width - pad, thickness)
                c.fill()

        return MathBox(width, num_shift + num.ascent, den_shift + den.descent, draw)

    def radical(self, body_nodes: list, index_nodes: Optional[list], style: int) -> MathBox:
        s = self.font_size(style)
        body = self.layout(body_nodes, style)
        t = self.rule(s)
        gap = t + 0.12 * s if style == DISPLAY else t * 1.25 + 0.06 * s
        top = max(body.ascent, 0.68 * s) + gap
        bottom = max(body.descent, 0.08 * s) + 0.06 * s
        height = top + bottom
        sign_width = min(0.62 * s, 0.48 * s + height * 0.05)
        index = self.layout(index_nodes, SCRIPTSCRIPT) if index_nodes else None
        index_raise = -bottom + height * 0.58
        lead = max(0.0, index.width - sign_width * 0.45) if index else 0.0
        width = lead + sign_width + body.width + 0.1 * s
        ascent = top + t
        if index:
            ascent = max(ascent, index_raise + index.descent + index.ascent)

        def draw(c: cairo.Context, x: float, y: float) -> None:
            x0, yb, yt = x + lead, y - bottom, y + top
            if index:
                index.draw(c, x0 + sign_width * 0.45 - index.width, y + index_raise + index.descent)
            hook = (x0 + sign_width * 0.2, yb + height * 0.48)
            foot = (x0 + sign_width * 0.5, yb)
            c.save()
            c.set_line_cap(cairo.LINE_CAP_ROUND)
            c.set_line_join(cairo.LINE_JOIN_ROUND)
            c.set_line_width(t)
            c.move_to(x0, -(yb + height * 0.42))
            c.line_to(hook[0], -hook[1])
            c.stroke()
            c.set_line_width(t * 1.9)
            c.move_to(hook[0], -hook[1])
            c.line_to(foot[0], -foot[1])
            c.stroke()
            c.set_line_width(t)
            c.set_line_cap(cairo.LINE_CAP_BUTT)
            c.move_to(foot[0], -foot[1])
            c.line_to(x0 + sign_width, -yt)
            c.line_to(x0 + sign_width + body.width + 0.1 * s, -yt)
            c.stroke()
            c.restore()
            body.draw(c, x0 + sign_width + 0.04 * s, y)

        return MathBox(width, ascent, bottom, draw)

    # MARK: Delimiters

    def fence(self, body: MathBox, left: str, right: str, style: int) -> MathBox:
        s = self.font_size(style)
        half = max(body.ascent - self.axis(s), body.descent + self.axis(s))
        height = max(2 * half * 0.901, 2 * half - 0.5 * s)
        return MathBox.row([self.delimiter(left, height, style), body, self.delimiter(right, height, style)])

    def delimiter(self, d: str, height: float, style: int) -> MathBox:
        s = self.font_size(style)
        if not d or d == ".":
            return kern(0.12 * s)
        glyph = self.glyphs(d, MATH_FAMILY, s)
        if height <= (glyph.ascent + glyph.descent) * 1.05:
            return glyph
        t = self.rule(s) * (1.1 + min(height / s, 4) * 0.12)
        if d in "()":
            width = 0.3 * s + height * 0.04
        elif d in "{}":
            width = 0.36 * s + height * 0.03
        elif d in "⟨⟩":
            width = 0.3 * s + height * 0.05
        elif d in "|‖":
            width = 0.26 * s
        else:
            width = 0.3 * s
        pad = 0.06 * s
        a = self.axis(s)

        def draw(c: cairo.Context, x: float, y: float) -> None:
            self._draw_delimiter(d, c, x + pad, y + a - height / 2, width, height, t, glyph)

        return MathBox(width + 2 * pad, a + height / 2, height / 2 - a, draw)

    @staticmethod
    def _draw_delimiter(d: str, c: cairo.Context, rx: float, ry: float, w: float, h: float, t: float, glyph: MathBox) -> None:
        flip = d in ")]}⟩⌋⌉"

        def X(f: float) -> float:
            return rx + w - f * w if flip else rx + f * w

        def quad(x0: float, y0: float, cx: float, cy: float, x1: float, y1: float) -> None:
            # Cairo only has cubics; raise the degree of the quadratic.
            c.curve_to(x0 + 2 / 3 * (cx - x0), -(y0 + 2 / 3 * (cy - y0)), x1 + 2 / 3 * (cx - x1), -(y1 + 2 / 3 * (cy - y1)), x1, -y1)

        top, bottom, mid, max_y = ry + h - t / 2, ry + t / 2, ry + h / 2, ry + h
        c.save()
        c.set_line_width(t)
        c.set_line_cap(cairo.LINE_CAP_BUTT)
        c.set_line_join(cairo.LINE_JOIN_MITER)
        if d in "()":
            # A filled crescent: thick in the middle, thin at the tips.
            end, outer, inner = X(0.95), X(0.05), X(0.05 + 1.7 * t / w)
            c.move_to(end, -max_y)
            quad(end, max_y, 2 * outer - end, mid, end, ry)
            quad(end, ry, 2 * inner - end, mid, end, max_y)
            c.close_path()
            c.fill()
        elif d in "[]⌊⌋⌈⌉":
            stem = X(0.25)
            has_top, has_bottom = d in "[]⌈⌉", d in "[]⌊⌋"
            c.move_to(X(0.9) if has_top else stem, -top)
            if has_top:
                c.line_to(stem, -top)
            c.line_to(stem, -bottom)
            if has_bottom:
                c.line_to(X(0.9), -bottom)
            c.stroke()
        elif d in "{}":
            tip, stem, point = X(0.85), X(0.5), X(0.08)
            radius = min(w * 0.4, h * 0.12)
            c.set_line_cap(cairo.LINE_CAP_ROUND)
            c.move_to(tip, -top)
            quad(tip, top, stem, top, stem, top - radius)
            c.line_to(stem, -(mid + radius))
            quad(stem, mid + radius, stem, mid, point, mid)
            quad(point, mid, stem, mid, stem, mid - radius)
            c.line_to(stem, -(bottom + radius))
            quad(stem, bottom + radius, stem, bottom, tip, bottom)
            c.stroke()
        elif d == "|":
            c.move_to(rx + w / 2, -max_y)
            c.line_to(rx + w / 2, -ry)
            c.stroke()
        elif d == "‖":
            for f in (0.3, 0.7):
                c.move_to(rx + f * w, -max_y)
                c.line_to(rx + f * w, -ry)
            c.stroke()
        elif d in "⟨⟩":
            c.set_line_join(cairo.LINE_JOIN_ROUND)
            c.move_to(X(0.9), -top)
            c.line_to(X(0.1), -mid)
            c.line_to(X(0.9), -bottom)
            c.stroke()
        elif d in "/\\":
            c.move_to(rx if d == "/" else rx + w, -ry)
            c.line_to(rx + w if d == "/" else rx, -max_y)
            c.stroke()
        else:
            # Anything else: stretch the glyph to the height.
            natural = max(glyph.ascent + glyph.descent, 1)
            c.translate(rx + w / 2 - glyph.width / 2, -ry)
            c.scale(1, h / natural)
            glyph.draw(c, 0, glyph.descent)
        c.restore()

    # MARK: Accents, boxes, arrays

    def accent(self, body_nodes: list, kind: str, style: int) -> MathBox:
        s = self.font_size(style)
        t = self.rule(s)
        body = self.layout(body_nodes, style)
        single = len(body_nodes) == 1 and self._is_glyph(body_nodes[0])
        skew = 0.06 * s if single else 0.0  # Italic letters lean right, so accents sit a little right.

        if kind in ("underline", "underbrace"):
            h = t if kind == "underline" else 0.28 * s
            gap = 0.1 * s

            def draw_under(c: cairo.Context, x: float, y: float) -> None:
                body.draw(c, x, y)
                ry = y - body.descent - gap - h
                if kind == "underline":
                    c.rectangle(x, -(ry + h), body.width, h)
                    c.fill()
                else:
                    self._draw_brace(c, x, ry, body.width, h, False, t * 1.2)

            return MathBox(body.width, body.ascent, body.descent + gap + h, draw_under)

        h = {
            "bar": t, "overline": t, "hat": 0.17 * s, "check": 0.17 * s, "widehat": 0.24 * s, "tilde": 0.14 * s,
            "widetilde": 0.14 * s, "vec": 0.22 * s, "right_arrow": 0.22 * s, "left_arrow": 0.22 * s,
            "left_right_arrow": 0.22 * s, "dot": 0.11 * s, "ddot": 0.11 * s, "overbrace": 0.28 * s,
        }.get(kind, 0.16 * s)
        gap = 0.1 * s
        top = max(body.ascent, 0.47 * s) + gap
        if kind in ("hat", "check", "tilde", "dot", "ddot", "breve", "acute", "grave", "ring"):
            accent_width = min(body.width, 0.42 * s)
        elif kind == "vec":
            accent_width = max(body.width * 0.9, 0.42 * s)
        elif kind == "bar":
            accent_width = max(body.width * 0.9, 0.35 * s)
        else:
            accent_width = body.width

        def draw(c: cairo.Context, x: float, y: float) -> None:
            body.draw(c, x, y)
            rx = x + (body.width - accent_width) / 2 + (0 if kind == "overline" else skew)
            self._draw_accent(kind, c, rx, y + top, accent_width, h, s, t)

        return MathBox(body.width, top + h + 0.03 * s, body.descent, draw)

    def _draw_accent(self, kind: str, c: cairo.Context, rx: float, ry: float, w: float, h: float, s: float, t: float) -> None:
        c.save()
        c.set_line_width(t * 1.1)
        c.set_line_cap(cairo.LINE_CAP_ROUND)
        c.set_line_join(cairo.LINE_JOIN_ROUND)
        max_x, max_y, mid_x, mid_y = rx + w, ry + h, rx + w / 2, ry + h / 2
        if kind in ("bar", "overline"):
            c.rectangle(rx, -max_y, w, h)
            c.fill()
        elif kind in ("hat", "widehat", "check"):
            up = kind != "check"
            c.move_to(rx, -(ry if up else max_y))
            c.line_to(mid_x, -(max_y if up else ry))
            c.line_to(max_x, -(ry if up else max_y))
            c.stroke()
        elif kind in ("tilde", "widetilde"):
            c.move_to(rx, -(ry + h * 0.25))
            c.curve_to(rx + w * 0.35, -(max_y + h * 0.4), rx + w * 0.65, -(ry - h * 0.4), max_x, -(ry + h * 0.75))
            c.stroke()
        elif kind in ("vec", "right_arrow", "left_arrow", "left_right_arrow"):
            head = min(h * 0.6, w * 0.4)
            c.set_line_width(t)
            c.move_to(rx, -mid_y)
            c.line_to(max_x, -mid_y)
            if kind != "left_arrow":
                c.move_to(max_x - head, -(mid_y + head * 0.8))
                c.line_to(max_x, -mid_y)
                c.line_to(max_x - head, -(mid_y - head * 0.8))
            if kind in ("left_arrow", "left_right_arrow"):
                c.move_to(rx + head, -(mid_y + head * 0.8))
                c.line_to(rx, -mid_y)
                c.line_to(rx + head, -(mid_y - head * 0.8))
            c.stroke()
        elif kind in ("dot", "ddot"):
            radius = 0.055 * s
            for cx in ([mid_x] if kind == "dot" else [mid_x - 0.11 * s, mid_x + 0.11 * s]):
                c.arc(cx, -mid_y, radius, 0, 2 * _math.pi)
                c.fill()
        elif kind == "breve":
            c.move_to(rx, -max_y)
            c.curve_to(rx + w / 3, -(ry - h * 0.6), max_x - w / 3, -(ry - h * 0.6), max_x, -max_y)
            c.stroke()
        elif kind in ("acute", "grave"):
            lean = kind == "acute"
            c.move_to(rx + w * (0.35 if lean else 0.65), -ry)
            c.line_to(rx + w * (0.75 if lean else 0.25), -max_y)
            c.stroke()
        elif kind == "ring":
            c.arc(mid_x, -mid_y, min(w, h) / 2, 0, 2 * _math.pi)
            c.stroke()
        elif kind == "overbrace":
            self._draw_brace(c, rx, ry, w, h, True, t * 1.2)
        c.restore()

    @staticmethod
    def _draw_brace(c: cairo.Context, rx: float, ry: float, w: float, h: float, up: bool, t: float) -> None:
        edge, point, mid = (ry, ry + h, ry + h / 2) if up else (ry + h, ry, ry + h / 2)
        radius = min(h, w / 4)
        max_x, mid_x = rx + w, rx + w / 2
        c.save()
        c.set_line_width(t)
        c.set_line_cap(cairo.LINE_CAP_ROUND)
        c.move_to(rx, -edge)
        c.curve_to(rx, -mid, rx, -mid, rx + radius, -mid)
        c.line_to(mid_x - radius, -mid)
        c.curve_to(mid_x, -mid, mid_x, -mid, mid_x, -point)
        c.curve_to(mid_x, -mid, mid_x, -mid, mid_x + radius, -mid)
        c.line_to(max_x - radius, -mid)
        c.curve_to(max_x, -mid, max_x, -mid, max_x, -edge)
        c.stroke()
        c.restore()

    def boxed(self, body: MathBox, s: float) -> MathBox:
        pad, t = 0.28 * s, self.rule(s)

        def draw(c: cairo.Context, x: float, y: float) -> None:
            body.draw(c, x + pad, y)
            c.save()
            c.set_line_width(t)
            c.rectangle(x + t / 2, -(y + body.ascent + pad - t / 2), body.width + 2 * pad - t, body.ascent + body.descent + 2 * pad - t)
            c.stroke()
            c.restore()

        return MathBox(body.width + 2 * pad, body.ascent + pad, body.descent + pad, draw)

    def array(self, rows: list, kind: str, spec: Optional[list], style: int) -> MathBox:
        s = self.font_size(style)
        cell_style = style if kind in ("aligned", "gathered") else (TEXT if style == DISPLAY else style)
        columns = max((len(row) for row in rows), default=0)
        cells = []
        for row in rows:
            built = []
            for c, nodes in enumerate(row):
                # In aligned columns `&=` should space the relation as if something preceded it.
                lead = [("group", [], ORD)] if kind == "aligned" and c % 2 else []
                built.append(self.layout(lead + nodes, cell_style))
            cells.append(built)
        widths = [0.0] * columns
        for row in cells:
            for c, cell in enumerate(row):
                widths[c] = max(widths[c], cell.width)

        def alignment(c: int) -> float:  # 0 leading, 0.5 center, 1 trailing
            if kind in ("matrix", "gathered"):
                return 0.5
            if kind == "cases":
                return 0.0
            if kind == "aligned":
                return 1.0 if c % 2 == 0 else 0.0
            if spec is None or c >= len(spec):
                return 0.5
            return {"l": 0.0, "r": 1.0}.get(spec[c], 0.5)

        def gap_after(c: int) -> float:
            if kind == "aligned":
                return 0.0 if c % 2 == 0 else 1.5 * s
            return 1.0 * s if kind in ("cases", "gathered") else 0.8 * s

        ascents = [max([b.ascent for b in row] + [0.72 * s]) for row in cells]
        descents = [max([b.descent for b in row] + [0.3 * s]) for row in cells]
        row_gap = (0.4 if kind in ("aligned", "gathered") else 0.25) * s
        height = sum(ascents) + sum(descents) + row_gap * max(len(rows) - 1, 0)
        width = sum(widths) + sum(gap_after(c) for c in range(max(columns - 1, 0)))
        top = self.axis(s) + height / 2

        def draw(c: cairo.Context, x: float, y: float) -> None:
            cursor_y = y + top
            for r, row in enumerate(cells):
                cursor_y -= ascents[r]
                cursor_x = x
                for col in range(columns):
                    if col < len(row):
                        row[col].draw(c, cursor_x + (widths[col] - row[col].width) * alignment(col), cursor_y)
                    cursor_x += widths[col] + (gap_after(col) if col < columns - 1 else 0)
                cursor_y -= descents[r] + row_gap

        return MathBox(width, top, height - top, draw)


@dataclass
class RenderedMath:
    """A typeset expression: paint at `(x, baseline)` on a Cairo context with the color already set."""

    width: float
    ascent: float
    descent: float
    box: MathBox

    def paint(self, c: cairo.Context, x: float, baseline: float) -> None:
        c.save()
        c.translate(x, baseline)
        self.box.draw(c, 0, 0)
        c.restore()


@lru_cache(maxsize=400)
def render_math(latex: str, size: float, display: bool) -> RenderedMath:
    box = _Typesetter(size).layout(MathParser.parse(latex), DISPLAY if display else TEXT)
    pad_x, pad_y = size * 0.08, size * 0.1
    inner = box

    def draw(c: cairo.Context, x: float, y: float) -> None:
        inner.draw(c, x + pad_x, y)

    return RenderedMath(max(1.0, box.width + 2 * pad_x), box.ascent + pad_y, box.descent + pad_y, MathBox(box.width, box.ascent, box.descent, draw))
