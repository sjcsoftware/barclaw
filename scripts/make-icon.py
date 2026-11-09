#!/usr/bin/env python3
"""Draws BarClaw's app icon: a neon claw reaching up to a menu bar. Usage: make-icon.py <output.png> [size]"""
import math
import sys

import cairo

SIRI = [(1.00, 0.18, 0.58), (0.66, 0.33, 0.97), (0.23, 0.51, 0.96), (0.13, 0.83, 0.93)]


def rounded(c, x, y, w, h, r):
    c.new_sub_path()
    c.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    c.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    c.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    c.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    c.close_path()


def pincer(c, flip):
    """One half of the claw, drawn around x = 0."""
    s = -1 if flip else 1
    c.move_to(s * 0.02, 0.86)
    c.curve_to(s * 0.28, 0.82, s * 0.34, 0.60, s * 0.27, 0.44)
    c.curve_to(s * 0.25, 0.38, s * 0.18, 0.30, s * 0.08, 0.27)
    c.curve_to(s * 0.15, 0.40, s * 0.15, 0.50, s * 0.09, 0.57)
    c.curve_to(s * 0.07, 0.60, s * 0.04, 0.62, s * 0.02, 0.63)
    c.close_path()


def draw(size):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    c = cairo.Context(surface)
    c.scale(size, size)
    rounded(c, 0.04, 0.04, 0.92, 0.92, 0.2)
    c.clip()
    background = cairo.LinearGradient(0, 0, 0, 1)
    background.add_color_stop_rgb(0, 0.09, 0.09, 0.13)
    background.add_color_stop_rgb(1, 0.05, 0.05, 0.07)
    c.set_source(background)
    c.paint()
    bar = cairo.LinearGradient(0, 0, 1, 0)
    for i, color in enumerate(SIRI):
        bar.add_color_stop_rgb(i / (len(SIRI) - 1), *color)
    c.set_source(bar)
    c.rectangle(0, 0, 1, 0.17)
    c.fill()
    c.translate(0.5, 0.02)
    claw = cairo.LinearGradient(0, 0.2, 0, 0.9)
    claw.add_color_stop_rgb(0, 1.0, 0.42, 0.34)
    claw.add_color_stop_rgb(1, 0.9, 0.16, 0.3)
    c.set_source(claw)
    pincer(c, False)
    c.fill()
    pincer(c, True)
    c.fill()
    surface.flush()
    return surface


if __name__ == "__main__":
    draw(int(sys.argv[2]) if len(sys.argv) > 2 else 256).write_to_png(sys.argv[1])
