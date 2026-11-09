"""A small, forgiving GitHub-flavored Markdown block parser.

Handles streaming input gracefully (an unterminated code fence, half a table), and turns inline
Markdown into Pango markup.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from typing import Optional, Union


@dataclass
class Heading:
    level: int
    text: str


@dataclass
class Paragraph:
    text: str


@dataclass
class Code:
    language: Optional[str]
    code: str


@dataclass
class Math:
    latex: str


@dataclass
class Quote:
    blocks: list


@dataclass
class ListItem:
    checked: Optional[bool]
    blocks: list


@dataclass
class ListBlock:
    ordered: bool
    start: int
    items: list


@dataclass
class Table:
    header: list
    alignments: list  # "leading" | "center" | "trailing"
    rows: list


@dataclass
class Rule:
    pass


Block = Union[Heading, Paragraph, Code, Math, Quote, ListBlock, Table, Rule]

_MATH_ENVIRONMENTS = ("equation", "align", "aligned", "gather", "gathered", "multline", "split", "alignat", "eqnarray", "displaymath")


def parse(text: str) -> list:
    return _LineParser(text.replace("\r\n", "\n").split("\n")).parse_blocks()


@dataclass
class _Marker:
    ordered: bool
    number: int
    indent: int
    content: str
    content_indent: int


def _leading(line: str, chars: str = " \t") -> int:
    return len(line) - len(line.lstrip(chars))


def _fence_marker(t: str) -> Optional[str]:
    for mark in ("`", "~"):
        if t.startswith(mark * 3):
            return t[: _leading(t, mark)]
    return None


def _heading(t: str) -> Optional[Heading]:
    hashes = _leading(t, "#")
    if not 1 <= hashes <= 6:
        return None
    rest = t[hashes:]
    if rest and not rest.startswith(" "):
        return None
    return Heading(hashes, rest.strip().rstrip("#").strip())


def _is_rule(t: str) -> bool:
    compact = t.replace(" ", "")
    return len(compact) >= 3 and compact[0] in "-*_" and all(c == compact[0] for c in compact)


def _math_environment(t: str) -> Optional[str]:
    if not t.startswith("\\begin{") or "}" not in t:
        return None
    name = t[7: t.index("}")]
    return name if name.replace("*", "") in _MATH_ENVIRONMENTS else None


def _starts_block(t: str) -> bool:
    return bool(_fence_marker(t) or _heading(t) or t.startswith((">", "$$", "\\[")) or _is_rule(t) or _math_environment(t))


def _list_marker(line: str) -> Optional[_Marker]:
    indent = _leading(line)
    rest = line[indent:]
    if rest[:1] in ("-", "*", "+") and rest[:1]:
        if rest[1:2] != " " or _is_rule(rest.strip()):
            return None
        return _Marker(False, 1, indent, rest[2:], indent + 2)
    digits = len(rest) - len(rest.lstrip("0123456789"))
    if not 1 <= digits <= 9:
        return None
    after = rest[digits:]
    if after[:1] not in (".", ")") or not after[:1] or after[1:2] != " ":
        return None
    return _Marker(True, int(rest[:digits]), indent, after[2:], indent + digits + 2)


def _table_cells(line: str) -> list:
    t = line.strip()
    if t.startswith("|"):
        t = t[1:]
    if t.endswith("|") and not t.endswith("\\|"):
        t = t[:-1]
    cells, current, escaped, in_code = [], "", False, False
    for ch in t:
        if escaped:
            current += ch
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            current += ch
            continue
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append(current.strip())
            current = ""
        else:
            current += ch
    cells.append(current.strip())
    return [cell.replace("\\|", "|") for cell in cells]


def _table_delimiter(line: str) -> Optional[list]:
    t = line.strip()
    if "-" not in t or ("|" not in t and ":" not in t):
        return None
    result = []
    for cell in _table_cells(t):
        c = cell.strip()
        if not c or "-" not in c or not all(ch in "-:" for ch in c):
            return None
        if c.startswith(":") and c.endswith(":"):
            result.append("center")
        elif c.endswith(":"):
            result.append("trailing")
        else:
            result.append("leading")
    return result or None


class _LineParser:
    def __init__(self, lines: list) -> None:
        self.lines = lines
        self.index = 0

    def parse_blocks(self) -> list:
        blocks: list = []
        paragraph: list = []

        def flush() -> None:
            if paragraph:
                blocks.append(Paragraph("\n".join(paragraph)))
                paragraph.clear()

        while self.index < len(self.lines):
            line = self.lines[self.index]
            trimmed = line.strip()

            if not trimmed:
                flush()
                self.index += 1
                continue

            fence = _fence_marker(trimmed)
            if fence:
                flush()
                language = trimmed[len(fence):].strip()
                self.index += 1
                code = []
                indent = _leading(line, " ")
                while self.index < len(self.lines):
                    current = self.lines[self.index]
                    stripped = current.strip()
                    if stripped.startswith(fence) and all(c == fence[0] for c in stripped):
                        self.index += 1
                        break
                    code.append(current[min(indent, _leading(current, " ")):])
                    self.index += 1
                blocks.append(Code(language or None, "\n".join(code)))
                continue

            math = self._display_math(trimmed)
            if math is not None:
                flush()
                blocks.append(Math(math[0]))
                if math[1]:
                    paragraph.append(math[1])
                continue

            heading = _heading(trimmed)
            if heading:
                flush()
                blocks.append(heading)
                self.index += 1
                continue

            # Setext underline, only directly after a paragraph line.
            if paragraph and len(trimmed) >= 2 and (set(trimmed) == {"="} or set(trimmed) == {"-"}):
                text = " ".join(paragraph)
                paragraph.clear()
                blocks.append(Heading(1 if trimmed[0] == "=" else 2, text))
                self.index += 1
                continue

            if _is_rule(trimmed):
                flush()
                blocks.append(Rule())
                self.index += 1
                continue

            if trimmed.startswith(">"):
                flush()
                quoted = []
                while self.index < len(self.lines):
                    t = self.lines[self.index].strip()
                    if not t.startswith(">"):
                        break
                    content = t[1:]
                    quoted.append(content[1:] if content.startswith(" ") else content)
                    self.index += 1
                blocks.append(Quote(_LineParser(quoted).parse_blocks()))
                continue

            alignments = _table_delimiter(self.lines[self.index + 1]) if self.index + 1 < len(self.lines) and "|" in trimmed else None
            if alignments is not None:
                flush()
                header = _table_cells(trimmed)
                self.index += 2
                rows = []
                while self.index < len(self.lines):
                    t = self.lines[self.index].strip()
                    if not t or "|" not in t:
                        break
                    cells = _table_cells(t)
                    cells += [""] * (len(header) - len(cells))
                    rows.append(cells[: len(header)])
                    self.index += 1
                aligned = (alignments + ["leading"] * len(header))[: len(header)]
                blocks.append(Table(header, aligned, rows))
                continue

            if _list_marker(line) is not None:
                flush()
                blocks.append(self._parse_list())
                continue

            paragraph.append(trimmed)
            self.index += 1
        flush()
        return blocks

    def _display_math(self, trimmed: str) -> Optional[tuple]:
        """Display math through its closing delimiter (or the end, while streaming)."""
        keep = False
        if trimmed.startswith("$$"):
            closer = "$$"
        elif trimmed.startswith("\\["):
            closer = "\\]"
        else:
            environment = _math_environment(trimmed)
            if environment is None:
                return None
            closer = f"\\end{{{environment}}}"
            keep = True
        body = trimmed if keep else trimmed[2:]
        close = body.find(closer, 1 if keep else 0)
        if close >= 0:
            # A single line with text after the closing delimiter is inline math in a paragraph.
            if body[close + len(closer):].strip():
                return None
            self.index += 1
            return (body[: close + len(closer)] if keep else body[:close]).strip(), ""
        latex = [body]
        trailing = ""
        self.index += 1
        while self.index < len(self.lines):
            line = self.lines[self.index]
            self.index += 1
            close = line.find(closer)
            if close >= 0:
                latex.append(line[: close + len(closer)] if keep else line[:close])
                trailing = line[close + len(closer):].strip()
                break
            latex.append(line)
        return "\n".join(latex).strip(), trailing

    def _parse_list(self) -> ListBlock:
        first = _list_marker(self.lines[self.index])
        items = []
        base = first.indent
        while self.index < len(self.lines):
            marker = _list_marker(self.lines[self.index])
            if marker is None or marker.indent > base + 1 or marker.ordered != first.ordered:
                break
            item_lines = [marker.content]
            self.index += 1
            # Continuation lines: indented further than the marker, or lazy paragraph lines.
            while self.index < len(self.lines):
                line = self.lines[self.index]
                t = line.strip()
                indent = _leading(line)
                if not t:
                    # Blank line: continue only if the next line is still part of this item.
                    if self.index + 1 < len(self.lines):
                        upcoming = self.lines[self.index + 1]
                        if _leading(upcoming) > base + 1 and upcoming.strip():
                            item_lines.append("")
                            self.index += 1
                            continue
                    break
                nested = _list_marker(line)
                if nested is not None and nested.indent <= base + 1:
                    break
                if indent > base:
                    item_lines.append(line[min(indent, marker.content_indent):])
                elif nested is None and not _starts_block(t):
                    item_lines.append(t)
                else:
                    break
                self.index += 1
            checked = None
            if item_lines[0].startswith("[ ] "):
                checked, item_lines[0] = False, item_lines[0][4:]
            elif item_lines[0].lower().startswith("[x] "):
                checked, item_lines[0] = True, item_lines[0][4:]
            items.append(ListItem(checked, _LineParser(item_lines).parse_blocks()))
        return ListBlock(first.ordered, first.number, items)


# MARK: Finding math in inline text


def split_math(source: str) -> list:
    """Splits inline Markdown into `("text", s)` and `("math", tex, display)` pieces."""
    if "$" not in source and "\\" not in source:
        return [("text", source)]
    chars, pieces, text, i = source, [], "", 0
    n = len(chars)

    def emit(body: str, display: bool) -> None:
        nonlocal text
        if text:
            pieces.append(("text", text))
            text = ""
        pieces.append(("math", body.strip(), display))

    def find(needle: str, start: int) -> int:
        j = start
        while j + len(needle) <= n:
            if chars.startswith(needle, j):
                return j
            j += 2 if chars[j] == "\\" else 1
        return -1

    def inline_end(open_at: int) -> int:
        # Pandoc's rule: `$` opens before a non-space and closes after a non-space and not before
        # a digit, so prices like "$5 and $10" stay text.
        if open_at + 1 >= n or chars[open_at + 1].isspace() or chars[open_at + 1] == "$":
            return -1
        j = open_at + 1
        while j < n:
            if chars[j] == "\\":
                j += 2
            elif chars[j] == "$":
                valid = not chars[j - 1].isspace() and (j + 1 >= n or not chars[j + 1].isdigit())
                return j if valid else -1
            else:
                j += 1
        return -1

    while i < n:
        c = chars[i]
        if c == "`":
            run = _leading(chars[i:], "`")
            stop = i + run
            j = stop
            while j < n:
                if chars[j] != "`":
                    j += 1
                    continue
                other = _leading(chars[j:], "`")
                if other == run:
                    stop = j + run
                    break
                j += other
            text += chars[i:stop]
            i = stop
            continue
        if c == "\\" and i + 1 < n:
            following = chars[i + 1]
            if following in "([":
                end = find("\\)" if following == "(" else "\\]", i + 2)
                if end >= 0:
                    emit(chars[i + 2:end], following == "[")
                    i = end + 2
                    continue
            text += c + following
            i += 2
            continue
        if c == "$":
            if chars.startswith("$$", i):
                end = find("$$", i + 2)
                if end > i + 2:
                    emit(chars[i + 2:end], True)
                    i = end + 2
                else:
                    text += "$$"
                    i += 2
                continue
            end = inline_end(i)
            if end >= 0:
                emit(chars[i + 1:end], False)
                i = end + 1
                continue
        text += c
        i += 1
    if text:
        pieces.append(("text", text))
    return pieces


# MARK: Inline Markdown → Pango markup

_INLINE = re.compile(
    r"(?P<code>(?<!`)(`+)(?!`)(?P<codebody>.+?)(?<!`)\2(?!`))"
    r"|(?P<image>!\[(?P<imgalt>[^\]]*)\]\((?P<imgurl>[^)\s]+)[^)]*\))"
    r"|(?P<link>\[(?P<linktext>[^\]]+)\]\((?P<linkurl>[^)\s]+)(?:\s+\"[^\"]*\")?\))"
    r"|(?P<auto>https?://[^\s<>()\[\]]+[^\s<>()\[\].,;:!?'\"])"
    r"|(?P<bold>\*\*(?=\S)(?P<boldbody>.+?)(?<=\S)\*\*|(?<![\w])__(?=\S)(?P<ubold>.+?)(?<=\S)__(?![\w]))"
    r"|(?P<strike>~~(?=\S)(?P<strikebody>.+?)(?<=\S)~~)"
    r"|(?P<em>(?<![\*\w])\*(?=[^\s\*])(?P<embody>.+?)(?<=[^\s\*])\*(?![\*\w])|(?<![\w_])_(?=[^\s_])(?P<uem>.+?)(?<=[^\s_])_(?![\w_]))",
    re.S,
)
_ESCAPABLE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~>$])")
CODE_FOREGROUND = "#ffd7a8"
LINK_FOREGROUND = "#7cc4ff"


def _plain(text: str) -> str:
    return escape(_ESCAPABLE.sub(r"\1", text), quote=False)


def inline_markup(text: str) -> str:
    """Pango markup for one run of inline Markdown (no math)."""
    out, position = [], 0
    for match in _INLINE.finditer(text):
        out.append(_plain(text[position:match.start()]))
        position = match.end()
        if match.group("code"):
            body = match.group("codebody")
            if len(body) > 1 and body.startswith(" ") and body.endswith(" "):
                body = body[1:-1]
            out.append(f'<span font_family="monospace" size="92%" foreground="{CODE_FOREGROUND}">{escape(body, quote=False)}</span>')
        elif match.group("image"):
            alt = match.group("imgalt") or "image"
            out.append(f'<a href="{escape(match.group("imgurl"), quote=True)}">{_plain(alt)}</a>')
        elif match.group("link"):
            out.append(f'<a href="{escape(match.group("linkurl"), quote=True)}">{inline_markup(match.group("linktext"))}</a>')
        elif match.group("auto"):
            url = match.group("auto")
            out.append(f'<a href="{escape(url, quote=True)}">{escape(url, quote=False)}</a>')
        elif match.group("bold"):
            out.append(f"<b>{inline_markup(match.group('boldbody') or match.group('ubold'))}</b>")
        elif match.group("strike"):
            out.append(f"<s>{inline_markup(match.group('strikebody'))}</s>")
        elif match.group("em"):
            out.append(f"<i>{inline_markup(match.group('embody') or match.group('uem'))}</i>")
    out.append(_plain(text[position:]))
    return "".join(out)
