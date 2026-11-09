"""Turns Markdown blocks into GTK widgets on the panel's dark surface."""
from __future__ import annotations

from typing import Callable, Optional

import cairo
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo

from . import parser as md
from .charts import ChartWidget, parse_chart
from .mathrender import RenderedMath, render_math
from .syntax import highlight

BASE_SIZE = 13.0
HEADING_SCALE = {1: 1.45, 2: 1.28, 3: 1.14, 4: 1.05, 5: 1.0, 6: 0.95}


def copy_text(text: str) -> None:
    Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)


def open_uri(uri: str) -> bool:
    if uri.startswith(("http://", "https://", "mailto:")):
        try:
            Gtk.show_uri_on_window(None, uri, Gdk.CURRENT_TIME)
        except GLib.Error:
            pass
    return True  # Handled: never let a label follow unexpected schemes.


def pango_size(pixels: float) -> int:
    """Pango markup sizes are points × 1024; panel sizes are CSS pixels (96 per inch)."""
    return int(pixels * 0.75 * Pango.SCALE)


def make_label(markup: str, css: Optional[str] = None, selectable: bool = True) -> Gtk.Label:
    label = Gtk.Label(xalign=0, yalign=0)
    label.set_line_wrap(True)
    label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_max_width_chars(1)  # Width comes from the allocation, not the text's natural length.
    label.set_hexpand(True)
    label.set_halign(Gtk.Align.FILL)
    label.set_selectable(selectable)
    label.set_can_focus(False)
    label.set_markup(markup)
    label.connect("activate-link", lambda _l, uri: open_uri(uri))
    if css:
        label.get_style_context().add_class(css)
    return label


def scaled(markup: str, scale: float, bold: bool = False) -> str:
    weight = ' weight="bold"' if bold else ""
    return f'<span size="{pango_size(BASE_SIZE * scale)}"{weight}>{markup}</span>'


class InlineFlow(Gtk.DrawingArea):
    """A paragraph with inline math: wraps styled word runs and typeset formulas together."""

    def __init__(self, pieces: list, size: float, css_class: str = "md-text") -> None:
        super().__init__()
        self.pieces = pieces
        self.size = size
        self.set_hexpand(True)
        self.get_style_context().add_class(css_class)
        self._cached_width = -1
        self._cached_lines: list = []
        self._cached_height = 0
        self.connect("draw", self._on_draw)

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_get_preferred_width(self) -> tuple:
        return 80, 320

    def do_get_preferred_height(self) -> tuple:
        height = self._measure(320)
        return height, height

    def do_get_preferred_height_for_width(self, width: int) -> tuple:
        height = self._measure(width)
        return height, height

    def _words(self) -> list:
        context = self.get_pango_context()
        font = Pango.FontDescription()
        font.set_family("sans-serif")
        font.set_absolute_size(self.size * Pango.SCALE)
        words = []
        for piece in self.pieces:
            if piece[0] == "math":
                rendered: RenderedMath = render_math(piece[1], self.size * 1.1, False)
                words.append(("math", rendered, rendered.width, rendered.ascent, rendered.descent))
                continue
            markup = md.inline_markup(piece[1])
            for chunk in _split_markup_words(markup):
                layout = Pango.Layout(context)
                layout.set_font_description(font)
                layout.set_markup(chunk, -1)
                width, height = layout.get_pixel_size()
                baseline = layout.get_baseline() / Pango.SCALE
                words.append(("text", layout, width, baseline, height - baseline))
        return words

    def _measure(self, width: int) -> int:
        if width == self._cached_width:
            return self._cached_height
        lines: list = [[]]
        x = 0.0
        for word in self._words():
            if x + word[2] > width and lines[-1]:
                lines.append([])
                x = 0.0
            lines[-1].append((x, word))
            x += word[2]
        height = sum(max([w[3] for _, w in line] + [self.size]) + max([w[4] for _, w in line] + [self.size * 0.3]) + 2 for line in lines)
        self._cached_width, self._cached_lines, self._cached_height = width, lines, int(height + 1)
        return self._cached_height

    def _on_draw(self, _widget: Gtk.Widget, c: cairo.Context) -> bool:
        self._measure(self.get_allocation().width)
        color = self.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        c.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        y = 0.0
        for line in self._cached_lines:
            ascent = max([w[3] for _, w in line] + [self.size])
            descent = max([w[4] for _, w in line] + [self.size * 0.3])
            baseline = y + ascent
            for x, word in line:
                if word[0] == "math":
                    word[1].paint(c, x, baseline)
                else:
                    c.move_to(x, baseline - word[3])
                    PangoCairo.update_layout(c, word[1])
                    PangoCairo.show_layout(c, word[1])
            y += ascent + descent + 2
        return False


def _split_markup_words(markup: str) -> list:
    """Splits Pango markup into word-sized chunks, each re-wrapped in the tags open around it."""
    chunks, open_tags, current = [], [], ""
    index = 0

    def closing() -> str:
        return "".join(f"</{tag.split()[0].strip('<>/')}>" for tag in reversed(open_tags))

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append("".join(open_tags) + current + closing())
            current = ""

    while index < len(markup):
        ch = markup[index]
        if ch == "<":
            end = markup.index(">", index)
            tag = markup[index: end + 1]
            flush()
            if tag.startswith("</"):
                if open_tags:
                    open_tags.pop()
            else:
                # Bare Pango has no links; inside a flow they're colored text.
                open_tags.append(f'<span foreground="{md.LINK_FOREGROUND}">' if tag.startswith("<a") else tag)
            index = end + 1
            continue
        if ch == "&":
            end = markup.index(";", index)
            current += markup[index: end + 1]
            index = end + 1
            continue
        current += ch
        index += 1
        if ch.isspace() or ord(ch) >= 0x2E80:
            flush()
    flush()
    return chunks


class MathBlock(Gtk.DrawingArea):
    """Display math, horizontally centered; scrolls with its parent when wider than the panel."""

    def __init__(self, latex: str, size: float) -> None:
        super().__init__()
        self.rendered = render_math(latex, size * 1.2, True)
        self.get_style_context().add_class("md-text")
        self.set_size_request(int(self.rendered.width + 1), int(self.rendered.ascent + self.rendered.descent + 2))
        self.set_tooltip_text(latex)
        self.connect("draw", self._on_draw)

    def _on_draw(self, _widget: Gtk.Widget, c: cairo.Context) -> bool:
        color = self.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        c.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        allocation = self.get_allocation()
        self.rendered.paint(c, max(0, (allocation.width - self.rendered.width) / 2), self.rendered.ascent + 1)
        return False


def inline_widget(text: str, size: float, bold: bool = False, css: Optional[str] = None) -> Gtk.Widget:
    pieces = md.split_math(text)
    if any(piece[0] == "math" for piece in pieces):
        flow = InlineFlow(pieces, size)
        if css:
            flow.get_style_context().add_class(css)
        return flow
    return make_label(scaled(md.inline_markup(text), size / BASE_SIZE, bold), css or "md-text")


def code_widget(language: Optional[str], code: str, size: float) -> Gtk.Widget:
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    outer.get_style_context().add_class("md-code")
    header = Gtk.Box(spacing=6)
    header.get_style_context().add_class("md-code-header")
    title = Gtk.Label(label=language or "code", xalign=0)
    title.get_style_context().add_class("dim")
    header.pack_start(title, True, True, 0)
    copy = Gtk.Button(label="Copy", relief=Gtk.ReliefStyle.NONE, can_focus=False)
    copy.get_style_context().add_class("chip-flat")

    def clicked(_button: Gtk.Button) -> None:
        copy_text(code)
        copy.set_label("Copied")
        GLib.timeout_add(1400, lambda: (copy.set_label("Copy"), GLib.SOURCE_REMOVE)[1])

    copy.connect("clicked", clicked)
    header.pack_end(copy, False, False, 0)
    outer.pack_start(header, False, False, 0)
    label = Gtk.Label(xalign=0, yalign=0)
    label.set_markup(f'<span font_family="monospace" size="{pango_size(size)}">{highlight(code, language)}</span>')
    label.set_selectable(True)
    label.set_can_focus(False)
    label.get_style_context().add_class("md-code-body")
    scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.AUTOMATIC, vscrollbar_policy=Gtk.PolicyType.NEVER)
    scroller.set_propagate_natural_height(True)
    scroller.set_overlay_scrolling(True)
    scroller.add(label)
    outer.pack_start(scroller, False, False, 0)
    return outer


def table_widget(table: md.Table, size: float) -> Gtk.Widget:
    grid = Gtk.Grid(column_spacing=0, row_spacing=0)
    grid.get_style_context().add_class("md-table")
    grid.set_column_homogeneous(False)
    aligns = {"leading": 0.0, "center": 0.5, "trailing": 1.0}

    def cell(text: str, row: int, column: int, header: bool) -> None:
        pieces = md.split_math(text)
        markup = "".join(md.inline_markup(p[1]) if p[0] == "text" else md.inline_markup(f"`{p[1]}`") for p in pieces)
        label = Gtk.Label(xalign=aligns[table.alignments[column]], yalign=0.5)
        label.set_markup(scaled(markup, (size - 0.5) / BASE_SIZE, header))
        label.set_line_wrap(True)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_max_width_chars(36)
        label.set_selectable(True)
        label.set_can_focus(False)
        label.connect("activate-link", lambda _l, uri: open_uri(uri))
        label.get_style_context().add_class("md-th" if header else "md-td")
        label.set_hexpand(True)
        label.set_halign(Gtk.Align.FILL)
        grid.attach(label, column, row, 1, 1)

    for column, text in enumerate(table.header):
        cell(text, 0, column, True)
    for r, row in enumerate(table.rows):
        for column, text in enumerate(row):
            cell(text, r + 1, column, False)
    # Cells wrap to the panel's width; a scroller here would claim the height of its narrowest layout.
    return grid


def block_widget(block, size: float, streaming: bool) -> Gtk.Widget:
    if isinstance(block, md.Heading):
        return inline_widget(block.text, size * HEADING_SCALE[block.level], bold=True)
    if isinstance(block, md.Paragraph):
        return inline_widget(block.text.replace("\n", " "), size)
    if isinstance(block, md.Code):
        if (block.language or "").lower() == "chart":
            spec = parse_chart(block.code)
            if spec is not None:
                card = Gtk.Box()
                card.get_style_context().add_class("md-card")
                card.pack_start(ChartWidget(spec, size), True, True, 0)
                return card
            if streaming:
                pending = make_label(scaled("Drawing chart…", 0.92), "dim", selectable=False)
                pending.get_style_context().add_class("md-card")
                return pending
        return code_widget(block.language, block.code, size - 1.5)
    if isinstance(block, md.Math):
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.AUTOMATIC, vscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_propagate_natural_height(True)
        scroller.add(MathBlock(block.latex, size))
        return scroller
    if isinstance(block, md.Quote):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=int(size * 0.5))
        box.get_style_context().add_class("md-quote")
        for inner in block.blocks:
            box.pack_start(block_widget(inner, size, streaming), False, False, 0)
        return box
    if isinstance(block, md.ListBlock):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=int(size * 0.3))
        for index, item in enumerate(block.items):
            row = Gtk.Box(spacing=6)
            marker = "☑" if item.checked else "☐" if item.checked is False else f"{block.start + index}." if block.ordered else "•"
            bullet = Gtk.Label(label=marker, xalign=1, yalign=0)
            bullet.get_style_context().add_class("dim")
            bullet.set_size_request(int(size * 1.4), -1)
            row.pack_start(bullet, False, False, 0)
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=int(size * 0.3))
            for inner in item.blocks:
                content.pack_start(block_widget(inner, size, streaming), False, False, 0)
            row.pack_start(content, True, True, 0)
            outer.pack_start(row, False, False, 0)
        return outer
    if isinstance(block, md.Table):
        return table_widget(block, size)
    separator = Gtk.Separator()
    separator.get_style_context().add_class("md-rule")
    return separator


class MarkdownView(Gtk.Box):
    """Keeps widgets for unchanged blocks across updates, so streaming replies don't flicker."""

    def __init__(self, size: float = BASE_SIZE) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=int(size * 0.7))
        self.size = size
        self._text: Optional[str] = None
        self._blocks: list = []
        self._widgets: list = []

    def update(self, text: str, streaming: bool = False) -> None:
        if text == self._text:
            return
        self._text = text
        blocks = md.parse(text)
        common = 0
        while common < len(blocks) and common < len(self._blocks) and blocks[common] == self._blocks[common]:
            common += 1
        for widget in self._widgets[common:]:
            self.remove(widget)
            widget.destroy()
        self._widgets = self._widgets[:common]
        for block in blocks[common:]:
            widget = block_widget(block, self.size, streaming)
            widget.show_all()
            self.pack_start(widget, False, False, 0)
            self._widgets.append(widget)
        self._blocks = blocks
