"""A forgiving parser for the LaTeX math people (and models) actually write.

Unknown commands render as their name; unbalanced input never fails. Nodes are small tuples whose
first element names the kind, so the typesetter can dispatch on it.
"""
from __future__ import annotations

from typing import Optional

# Math classes (TeX's atom types).
ORD, OP, BIN, REL, OPEN, CLOSE, PUNCT, INNER = range(8)
# Math styles.
DISPLAY, TEXT, SCRIPT, SCRIPTSCRIPT = range(4)


def script_of(style: int) -> int:
    return SCRIPT if style <= TEXT else SCRIPTSCRIPT


def fraction_of(style: int) -> int:
    return {DISPLAY: TEXT, TEXT: SCRIPT}.get(style, SCRIPTSCRIPT)


CHARACTERS = {
    "+": ("+", BIN), "-": ("−", BIN), "*": ("∗", BIN), "/": ("/", ORD),
    "=": ("=", REL), "<": ("<", REL), ">": (">", REL), ":": (":", REL),
    ",": (",", PUNCT), ";": (";", PUNCT), ".": (".", ORD), "!": ("!", CLOSE), "?": ("?", CLOSE),
    "(": ("(", OPEN), "[": ("[", OPEN), ")": (")", CLOSE), "]": ("]", CLOSE), "|": ("|", ORD),
    "@": ("@", ORD), '"': ('"', ORD), "#": ("#", ORD), "%": ("%", ORD),
    "−": ("−", BIN), "·": ("⋅", BIN), "×": ("×", BIN), "÷": ("÷", BIN), "±": ("±", BIN),
}


def unicode_class(c: str) -> int:
    if c in "≤≥≠≈≡→←↔⇒⇐⇔∈∉∋⊂⊆⊃⊇∼≃≅∝≪≫⟶⟵⟹⟸⟺↦∣∥⊥⊢⊨≺≻":
        return REL
    if c in "∓⋅∘∙∩∪∧∨⊕⊗⊖⊙∖⊔⊓†‡⋆":
        return BIN
    if c in "⟨⌊⌈":
        return OPEN
    if c in "⟩⌋⌉":
        return CLOSE
    return ORD


GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "varkappa": "ϰ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "omicron": "ο", "pi": "π", "varpi": "ϖ",
    "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ",
    "phi": "ϕ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ",
    "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}


def _table(cls: int, text: str) -> dict:
    return {name: (glyph, cls) for name, glyph in (entry.split("=") for entry in text.split())}


COMMANDS = {
    **_table(REL, "le=≤ leq=≤ ge=≥ geq=≥ leqslant=⩽ geqslant=⩾ ne=≠ neq=≠ approx=≈ equiv=≡ sim=∼ simeq=≃ cong=≅ "
                  "propto=∝ doteq=≐ coloneqq=≔ triangleq=≜ asymp=≍ in=∈ notin=∉ ni=∋ subset=⊂ subseteq=⊆ supset=⊃ "
                  "supseteq=⊇ subsetneq=⊊ supsetneq=⊋ to=→ rightarrow=→ leftarrow=← gets=← leftrightarrow=↔ "
                  "Rightarrow=⇒ Leftarrow=⇐ Leftrightarrow=⇔ iff=⟺ implies=⟹ impliedby=⟸ mapsto=↦ longrightarrow=⟶ "
                  "longleftarrow=⟵ Longrightarrow=⟹ Longleftarrow=⟸ longleftrightarrow=⟷ Longleftrightarrow=⟺ "
                  "longmapsto=⟼ uparrow=↑ downarrow=↓ Uparrow=⇑ Downarrow=⇓ updownarrow=↕ nearrow=↗ searrow=↘ "
                  "hookrightarrow=↪ rightleftharpoons=⇌ parallel=∥ perp=⊥ mid=∣ nmid=∤ ll=≪ gg=≫ prec=≺ succ=≻ "
                  "preceq=⪯ succeq=⪰ models=⊨ vdash=⊢ dashv=⊣ therefore=∴ because=∵ lessgtr=≶ lesssim=≲ gtrsim=≳"),
    **_table(BIN, "pm=± mp=∓ times=× div=÷ cdot=⋅ ast=∗ star=⋆ circ=∘ bullet=∙ cap=∩ cup=∪ wedge=∧ land=∧ vee=∨ lor=∨ "
                  "oplus=⊕ otimes=⊗ ominus=⊖ odot=⊙ oslash=⊘ setminus=∖ sqcup=⊔ sqcap=⊓ uplus=⊎ dagger=† ddagger=‡ "
                  "amalg=⨿ wr=≀ diamond=⋄ triangleleft=◁ triangleright=▷"),
    **_table(ORD, "neg=¬ lnot=¬ infty=∞ partial=∂ nabla=∇ forall=∀ exists=∃ nexists=∄ emptyset=∅ varnothing=∅ angle=∠ "
                  "measuredangle=∡ triangle=△ hbar=ℏ hslash=ℏ ell=ℓ Re=ℜ Im=ℑ aleph=ℵ wp=℘ prime=′ degree=° top=⊤ bot=⊥ "
                  "square=□ Box=□ blacksquare=■ checkmark=✓ clubsuit=♣ diamondsuit=♢ heartsuit=♡ spadesuit=♠ flat=♭ "
                  "natural=♮ sharp=♯ S=§ P=¶ vdots=⋮ vert=| Vert=‖ backslash=\\ R=ℝ N=ℕ Z=ℤ Q=ℚ C=ℂ imath=ı jmath=ȷ"),
    **_table(INNER, "dots=… ldots=… cdots=⋯ dotsc=… dotsb=⋯ ddots=⋱"),
    **_table(OPEN, "lfloor=⌊ lceil=⌈ langle=⟨ lvert=| lVert=‖ lbrace={ lbrack=["),
    **_table(CLOSE, "rfloor=⌋ rceil=⌉ rangle=⟩ rvert=| rVert=‖ rbrace=} rbrack=]"),
    "colon": (":", PUNCT), "{": ("{", OPEN), "}": ("}", CLOSE), "|": ("‖", ORD),
    "#": ("#", ORD), "%": ("%", ORD), "&": ("&", ORD), "_": ("_", ORD), "$": ("$", ORD),
}

DELIMITERS = {
    "{": "{", "}": "}", "|": "‖", "langle": "⟨", "rangle": "⟩", "lvert": "|", "rvert": "|", "vert": "|",
    "lVert": "‖", "rVert": "‖", "Vert": "‖", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
    "lbrace": "{", "rbrace": "}", "lbrack": "[", "rbrack": "]", "backslash": "\\",
}

LARGE_OPERATORS = {
    "sum": ("∑", None), "prod": ("∏", None), "coprod": ("∐", None), "bigcup": ("⋃", None), "bigcap": ("⋂", None),
    "bigvee": ("⋁", None), "bigwedge": ("⋀", None), "bigoplus": ("⨁", None), "bigotimes": ("⨂", None),
    "bigodot": ("⨀", None), "biguplus": ("⨄", None), "bigsqcup": ("⨆", None),
    "int": ("∫", False), "iint": ("∬", False), "iiint": ("∭", False), "oint": ("∮", False), "oiint": ("∯", False),
}

FUNCTIONS: dict = {
    **{name: (name, False) for name in ("sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh coth log ln lg exp dim ker deg arg hom").split()},
    **{name: (name, None) for name in "lim max min sup inf det gcd Pr".split()},
    "limsup": ("lim sup", None), "liminf": ("lim inf", None), "argmax": ("arg max", None), "argmin": ("arg min", None),
}

ACCENTS = {
    "hat": "hat", "widehat": "widehat", "check": "check", "tilde": "tilde", "widetilde": "widetilde",
    "bar": "bar", "overline": "overline", "underline": "underline", "vec": "vec",
    "overrightarrow": "right_arrow", "overleftarrow": "left_arrow", "overleftrightarrow": "left_right_arrow",
    "dot": "dot", "ddot": "ddot", "breve": "breve", "acute": "acute", "grave": "grave", "mathring": "ring",
    "overbrace": "overbrace", "underbrace": "underbrace",
}

SPACES = {
    ",": 3 / 18, ":": 4 / 18, ">": 4 / 18, ";": 5 / 18, "!": -3 / 18, " ": 0.33,
    "thinspace": 3 / 18, "medspace": 4 / 18, "thickspace": 5 / 18, "negthinspace": -3 / 18,
    "negmedspace": -4 / 18, "negthickspace": -5 / 18, "enspace": 0.5, "quad": 1, "qquad": 2,
}

STYLES = {"displaystyle": DISPLAY, "textstyle": TEXT, "scriptstyle": SCRIPT, "scriptscriptstyle": SCRIPTSCRIPT}

NEGATED = {
    "=": "≠", "∈": "∉", "⊂": "⊄", "⊆": "⊈", "⊃": "⊅", "⊇": "⊉", "≡": "≢", "<": "≮", ">": "≯",
    "≤": "≰", "≥": "≱", "∼": "≁", "≈": "≉", "≅": "≇", "∣": "∤", "∥": "∦", "∃": "∄", "→": "↛",
}

FONT_COMMANDS = {
    "mathrm": "roman", "mathup": "roman", "rm": "roman", "mathit": "italic", "mathnormal": "italic",
    "mathbf": "bold", "bf": "bold", "boldsymbol": "bold_italic", "bm": "bold_italic", "pmb": "bold_italic",
    "mathbb": "blackboard", "Bbb": "blackboard", "mathcal": "script", "mathscr": "script", "cal": "script",
    "mathfrak": "fraktur", "mathsf": "sans_serif", "mathtt": "monospace",
}

# Base code points of the Unicode math alphabets: (upper, lower, digit, greek).
_ALPHABETS = {
    "italic": (0x1D434, 0x1D44E, None, 0x1D6FC),
    "bold": (0x1D400, 0x1D41A, 0x1D7CE, 0x1D6C2),
    "bold_italic": (0x1D468, 0x1D482, 0x1D7CE, 0x1D736),
    "blackboard": (0x1D538, 0x1D552, 0x1D7D8, None),
    "script": (0x1D49C, 0x1D4B6, None, None),
    "fraktur": (0x1D504, 0x1D51E, None, None),
    "sans_serif": (0x1D5A0, 0x1D5BA, 0x1D7E2, None),
    "monospace": (0x1D670, 0x1D68A, 0x1D7F6, None),
}
# Letters that live in the Letterlike Symbols block instead of the math alphabets.
_EXCEPTIONS = {
    "italic": {"h": 0x210E},
    "blackboard": {"C": 0x2102, "H": 0x210D, "N": 0x2115, "P": 0x2119, "Q": 0x211A, "R": 0x211D, "Z": 0x2124},
    "script": {"B": 0x212C, "E": 0x2130, "F": 0x2131, "H": 0x210B, "I": 0x2110, "L": 0x2112, "M": 0x2133, "R": 0x211B, "e": 0x212F, "g": 0x210A, "o": 0x2134},
    "fraktur": {"C": 0x212D, "H": 0x210C, "I": 0x2111, "R": 0x211C, "Z": 0x2128},
}
_ITALIC_VARIANTS = {0x3F5: 0x1D716, 0x3D1: 0x1D717, 0x3F0: 0x1D718, 0x3D5: 0x1D719, 0x3F1: 0x1D71A, 0x3D6: 0x1D71B}


def map_alphabet(text: str, font: str) -> str:
    """Maps letters to the Unicode math alphabets (𝑥, 𝐱, ℝ, 𝒜…) that math fonts are designed around."""
    if font == "roman":
        return text
    bases = _ALPHABETS[font]
    out = []
    for c in text:
        v = ord(c)
        special = _EXCEPTIONS.get(font, {}).get(c)
        if special is not None:
            out.append(chr(special))
        elif 0x41 <= v <= 0x5A:
            out.append(chr(bases[0] + v - 0x41))
        elif 0x61 <= v <= 0x7A:
            out.append(chr(bases[1] + v - 0x61))
        elif 0x30 <= v <= 0x39 and bases[2] is not None:
            out.append(chr(bases[2] + v - 0x30))
        elif 0x3B1 <= v <= 0x3C9 and bases[3] is not None:
            out.append(chr(bases[3] + v - 0x3B1))
        elif font == "italic" and v in _ITALIC_VARIANTS:
            out.append(chr(_ITALIC_VARIANTS[v]))
        else:
            out.append(c)
    return "".join(out)


def _is_ascii_letter(c: str) -> bool:
    return c.isascii() and c.isalpha()


class MathParser:
    def __init__(self, source: str) -> None:
        self.chars = source
        self.i = 0
        self.font = "italic"

    @classmethod
    def parse(cls, latex: str) -> list:
        parser = cls(latex)
        rows = parser._rows(None)
        if len(rows) == 1 and len(rows[0]) == 1:
            return rows[0][0]
        kind = "aligned" if any(len(row) > 1 for row in rows) else "gathered"
        return [("array", rows, kind, None)]

    def _rows(self, environment: Optional[str]) -> list:
        rows: list = []
        row: list = []
        cell: list = []
        n = len(self.chars)
        while True:
            cell += self._list()
            if self.i >= n:
                break
            if self.chars[self.i] == "&":
                self.i += 1
                row.append(cell)
                cell = []
                continue
            name = self._peek_command()
            if name in ("\\", "cr"):
                self._skip_command()
                self._optional_bracket()
                row.append(cell)
                rows.append(row)
                row, cell = [], []
                continue
            if name == "end":
                self._skip_command()
                self._raw_argument()
                if environment is not None:
                    break
                continue
            self._skip_token()  # A stray `}` or `\right` at this level: drop it and keep going.
        row.append(cell)
        rows.append(row)
        if len(rows) > 1 and len(rows[-1]) == 1 and not rows[-1][0]:
            rows.pop()
        return rows

    def _list(self) -> list:
        """Atoms until the end of input or a `}`, `&`, `\\\\`, `\\end` or `\\right` (not consumed)."""
        out: list = []
        n = len(self.chars)
        while self.i < n:
            c = self.chars[self.i]
            if c.isspace():
                self.i += 1
                continue
            if c in "}&":
                return out
            if c == "\\" and self._peek_command() in ("\\", "cr", "end", "right"):
                return out
            if c in "^_":
                self.i += 1
                self._attach_script(out, self._argument(), c == "^")
                continue
            if c == "'":
                count = 0
                while self.i + count < n and self.chars[self.i + count] == "'":
                    count += 1
                self.i += count
                out.append(("symbol", "′″‴"[min(count, 3) - 1], ORD))
                continue
            self._item(out, single=False)
        return out

    def _group_body(self) -> list:
        out: list = []
        while True:
            out += self._list()
            if self.i >= len(self.chars):
                break
            if self.chars[self.i] == "}":
                self.i += 1
                break
            self._skip_token()
        return out

    def _argument(self) -> list:
        self._skip_spaces()
        if self.i >= len(self.chars):
            return []
        c = self.chars[self.i]
        if c == "{":
            self.i += 1
            return self._group_body()
        if c in "}&" or (c == "\\" and self._peek_command() in ("\\", "cr", "end", "right")):
            return []
        out: list = []
        self._item(out, single=True)
        return out

    @staticmethod
    def _attach_script(out: list, script: list, superscript: bool) -> None:
        last = out.pop() if out else ("group", [], ORD)
        if last[0] == "scripts" and (last[3] is None if superscript else last[2] is None):
            _, base, sub, sup = last
            out.append(("scripts", base, sub if superscript else script, script if superscript else sup))
        elif last[0] == "symbol" and last[1] in "′″‴" and superscript:
            # f'^2: keep the prime and the exponent together in the superscript.
            out.append(("scripts", ("group", [], ORD), None, [last] + script))
        else:
            out.append(("scripts", last, None if superscript else script, script if superscript else None))

    def _item(self, out: list, single: bool) -> None:
        c = self.chars[self.i]
        if c == "{":
            self.i += 1
            body = self._group_body()
            out.append(body[0] if len(body) == 1 else ("group", body, ORD))
        elif c == "\\":
            self.i += 1
            self._command(self._read_command(), out)
        elif c in "0123456789":
            digits = c
            self.i += 1
            n = len(self.chars)
            if not single:
                while self.i < n and self.chars[self.i].isascii() and (
                    self.chars[self.i].isdigit() or (self.chars[self.i] == "." and self.i + 1 < n and self.chars[self.i + 1].isdigit())
                ):
                    digits += self.chars[self.i]
                    self.i += 1
            out.append(("symbol", map_alphabet(digits, self.font), ORD))
        elif c == "~":
            self.i += 1
            out.append(("space", 0.33))
        else:
            self.i += 1
            if _is_ascii_letter(c):
                out.append(("symbol", map_alphabet(c, self.font), ORD))
            elif c in CHARACTERS:
                out.append(("symbol", *CHARACTERS[c]))
            elif 0x391 <= ord(c) <= 0x3FF:
                out.append(("symbol", map_alphabet(c, self.font), ORD))
            elif c.isalpha():
                # Hangul, CJK and other prose typed straight into math: set it as text.
                run = c
                n = len(self.chars)
                while self.i < n and not self.chars[self.i].isascii() and self.chars[self.i].isalnum():
                    run += self.chars[self.i]
                    self.i += 1
                out.append(("text", run, False, False))
            else:
                out.append(("symbol", c, unicode_class(c)))

    def _command(self, name: str, out: list) -> None:
        if name in COMMANDS:
            out.append(("symbol", *COMMANDS[name]))
            return
        if name in GREEK:
            # LaTeX sets lowercase Greek italic and capitals upright.
            letter = GREEK[name]
            upright = letter.isupper() and not name.startswith("var")
            out.append(("symbol", letter if upright else map_alphabet(letter, self.font), ORD))
            return
        if name in LARGE_OPERATORS:
            out.append(("large_op", *LARGE_OPERATORS[name]))
            return
        if name in FUNCTIONS:
            out.append(("function", *FUNCTIONS[name]))
            return
        if name in FONT_COMMANDS:
            saved, self.font = self.font, FONT_COMMANDS[name]
            body = self._argument()
            self.font = saved
            out.append(body[0] if len(body) == 1 else ("group", body, ORD))
            return
        if name in ACCENTS:
            out.append(("accent", self._argument(), ACCENTS[name]))
            return
        if name in SPACES:
            out.append(("space", SPACES[name]))
            return
        if name in STYLES:
            out.append(("styled", STYLES[name], self._list()))
            return

        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            num, den = self._argument(), self._argument()
            style = DISPLAY if name in ("dfrac", "cfrac") else TEXT if name == "tfrac" else None
            out.append(("fraction", num, den, True, style))
        elif name in ("binom", "dbinom", "tbinom"):
            top, bottom = self._argument(), self._argument()
            style = DISPLAY if name == "dbinom" else TEXT if name == "tbinom" else None
            out.append(("fenced", "(", [("fraction", top, bottom, False, style)], ")"))
        elif name == "sqrt":
            index = self._optional_bracket()
            out.append(("radical", self._argument(), MathParser.parse(index) if index is not None else None))
        elif name == "left":
            opener = self._delimiter()
            body = self._list()
            closer = "."
            if self._peek_command() == "right":
                self._skip_command()
                closer = self._delimiter()
            out.append(("fenced", opener, body, closer))
        elif name == "right":
            self._delimiter()
        elif name == "middle":
            d = self._delimiter()
            if d != ".":
                out.append(("symbol", d, ORD))
        elif name in ("big", "Big", "bigg", "Bigg", "bigl", "Bigl", "biggl", "Biggl", "bigr", "Bigr", "biggr", "Biggr", "bigm", "Bigm", "biggm", "Biggm"):
            scale = 3.0 if name.startswith("Bigg") else 2.4 if name.startswith("bigg") else 1.8 if name.startswith("Big") else 1.2
            cls = OPEN if name.endswith("l") else CLOSE if name.endswith("r") else REL if name.endswith("m") else ORD
            d = self._delimiter()
            if d != ".":
                out.append(("delimiter", d, scale, cls))
        elif name in ("text", "textrm", "textnormal", "textup", "textsf", "texttt", "textmd", "mbox", "hbox", "fbox", "textbf", "textit", "emph", "textsl"):
            nodes = self._text_nodes(self._raw_argument(), name == "textbf", name in ("textit", "emph", "textsl"))
            out.append(("boxed", nodes) if name == "fbox" else nodes[0] if len(nodes) == 1 else ("group", nodes, ORD))
        elif name == "operatorname":
            limits = self.i < len(self.chars) and self.chars[self.i] == "*"
            if limits:
                self.i += 1
            out.append(("function", self._plain_text(self._raw_argument()), None if limits else False))
        elif name in ("boxed", "framebox"):
            self._optional_bracket()
            out.append(("boxed", self._argument()))
        elif name in ("overset", "stackrel", "underset"):
            annotation, base = self._argument(), self._argument()
            cls = REL if name == "stackrel" else (base[0][2] if len(base) == 1 and base[0][0] == "symbol" else ORD)
            out.append(("stack", base, None, annotation, cls) if name == "underset" else ("stack", base, annotation, None, cls))
        elif name in ("xrightarrow", "xleftarrow", "xRightarrow", "xLeftarrow", "xleftrightarrow"):
            bracket = self._optional_bracket()
            under = MathParser.parse(bracket) if bracket is not None else None
            over = self._argument()
            arrow = {"xrightarrow": "⟶", "xleftarrow": "⟵", "xRightarrow": "⟹", "xLeftarrow": "⟸"}.get(name, "⟷")
            out.append(("stack", [("symbol", arrow, REL)], over, under, REL))
        elif name == "substack":
            out.append(("group", MathParser.parse(self._raw_argument()), ORD))
        elif name in ("phantom", "hphantom", "vphantom"):
            out.append(("phantom", self._argument()))
        elif name == "color":
            self._raw_argument()
        elif name in ("textcolor", "mathcolor", "colorbox"):
            self._raw_argument()
            body = self._argument()
            out.append(body[0] if len(body) == 1 else ("group", body, ORD))
        elif name in ("hspace", "mspace", "hskip", "kern", "mkern", "mskip"):
            if self.i < len(self.chars) and self.chars[self.i] == "*":
                self.i += 1
            self._skip_spaces()
            braced = self.i < len(self.chars) and self.chars[self.i] == "{"
            out.append(("space", self._ems(self._raw_argument() if braced else self._dimension())))
        elif name in ("limits", "nolimits"):
            if out and out[-1][0] in ("large_op", "function"):
                kind, glyph, _ = out.pop()
                out.append((kind, glyph, name == "limits"))
        elif name == "not":
            following: list = []
            self._skip_spaces()
            if self.i < len(self.chars):
                self._item(following, single=True)
            if len(following) == 1 and following[0][0] == "symbol":
                _, s, cls = following[0]
                out.append(("symbol", NEGATED.get(s, s + "̸"), cls))
            else:
                out.extend(following)
        elif name == "mod":
            out.extend([("space", 0.5), ("function", "mod", False), ("space", 0.2)])
        elif name == "bmod":
            out.append(("group", [("function", "mod", False)], BIN))
        elif name == "pmod":
            arg = self._argument()
            out.extend([("space", 0.5), ("symbol", "(", OPEN), ("function", "mod", False), ("space", 0.2), *arg, ("symbol", ")", CLOSE)])
        elif name == "begin":
            out.extend(self._environment(self._raw_argument()))
        elif name in ("tag", "label", "eqref", "ref"):
            self._raw_argument()
        elif name in ("nonumber", "notag", "displaylimits", "relax", "nobreak", "allowbreak", "centering", ""):
            pass
        else:
            out.append(("function", name, False))

    def _environment(self, name: str) -> list:
        bare = name.replace("*", "")
        if bare in ("alignat", "alignedat"):
            self._raw_argument()
        spec = [c for c in self._raw_argument() if c in "lcr"] if bare == "array" else None
        rows = self._rows(name)

        def matrix(left: str, right: str) -> list:
            return [("fenced", left, [("array", rows, "matrix", None)], right)]

        if bare in ("matrix", "smallmatrix"):
            return [("array", rows, "matrix", None)]
        fences = {"pmatrix": ("(", ")"), "bmatrix": ("[", "]"), "Bmatrix": ("{", "}"), "vmatrix": ("|", "|"), "Vmatrix": ("‖", "‖")}
        if bare in fences:
            return matrix(*fences[bare])
        if bare in ("cases", "dcases"):
            return [("fenced", "{", [("array", rows, "cases", None)], ".")]
        if bare == "rcases":
            return [("fenced", ".", [("array", rows, "cases", None)], "}")]
        if bare == "array":
            return [("array", rows, "array", spec or [])]
        if bare in ("aligned", "align", "alignat", "alignedat", "split", "eqnarray", "flalign"):
            return [("array", rows, "aligned", None)]
        if len(rows) == 1 and len(rows[0]) == 1:
            return rows[0][0]
        return [("array", rows, "gathered", None)]

    def _text_nodes(self, raw: str, bold: bool, italic: bool) -> list:
        """`\\text{…}` content, with any `$…$` inside it set as math."""
        nodes = []
        for k, part in enumerate(raw.split("$")):
            if not part:
                continue
            if k % 2 == 0:
                nodes.append(("text", self._plain_text(part), bold, italic))
            else:
                nodes.append(("group", MathParser.parse(part), ORD))
        return nodes or [("group", [], ORD)]

    @staticmethod
    def _plain_text(raw: str) -> str:
        out, escaped = "", False
        for c in raw:
            if escaped:
                out += c
                escaped = False
            elif c == "\\":
                escaped = True
            elif c not in "{}":
                out += " " if c == "~" else c
        return out

    @staticmethod
    def _ems(raw: str) -> float:
        """A TeX dimension (`1em`, `3mu`, `5pt`) in ems."""
        t = raw.strip()
        end = 0
        while end < len(t) and (t[end].isdigit() or t[end] in ".-"):
            end += 1
        try:
            number = float(t[:end])
        except ValueError:
            number = 0.5
        if t.endswith("mu"):
            return number / 18
        if t.endswith("pt"):
            return number / 10
        if t.endswith("ex"):
            return number * 0.45
        return number

    # MARK: Low-level reading

    def _skip_spaces(self) -> None:
        while self.i < len(self.chars) and self.chars[self.i].isspace():
            self.i += 1

    def _peek_command(self) -> str:
        if self.i >= len(self.chars) or self.chars[self.i] != "\\":
            return ""
        j = self.i + 1
        if j >= len(self.chars):
            return ""
        if not _is_ascii_letter(self.chars[j]):
            return self.chars[j]
        start = j
        while j < len(self.chars) and _is_ascii_letter(self.chars[j]):
            j += 1
        return self.chars[start:j]

    def _skip_command(self) -> None:
        self.i += 1
        self._read_command()

    def _read_command(self) -> str:
        if self.i >= len(self.chars):
            return ""
        if not _is_ascii_letter(self.chars[self.i]):
            self.i += 1
            return self.chars[self.i - 1]
        start = self.i
        while self.i < len(self.chars) and _is_ascii_letter(self.chars[self.i]):
            self.i += 1
        return self.chars[start:self.i]

    def _skip_token(self) -> None:
        if self.i < len(self.chars):
            if self.chars[self.i] == "\\":
                self._skip_command()
            else:
                self.i += 1

    def _raw_argument(self) -> str:
        """A `{…}` argument as raw text (or a single character / command)."""
        self._skip_spaces()
        n = len(self.chars)
        if self.i >= n:
            return ""
        if self.chars[self.i] == "\\":
            start = self.i
            self._skip_command()
            return self.chars[start:self.i]
        if self.chars[self.i] != "{":
            self.i += 1
            return self.chars[self.i - 1]
        self.i += 1
        depth, out = 1, ""
        while self.i < n:
            c = self.chars[self.i]
            if c == "\\" and self.i + 1 < n:
                out += c + self.chars[self.i + 1]
                self.i += 2
                continue
            self.i += 1
            if c == "{":
                depth += 1
            if c == "}":
                depth -= 1
                if depth == 0:
                    break
            out += c
        return out

    def _optional_bracket(self) -> Optional[str]:
        start = self.i
        self._skip_spaces()
        if self.i >= len(self.chars) or self.chars[self.i] != "[":
            self.i = start
            return None
        self.i += 1
        depth, out = 0, ""
        while self.i < len(self.chars):
            c = self.chars[self.i]
            self.i += 1
            if c == "{":
                depth += 1
            if c == "}":
                depth -= 1
            if c == "]" and depth == 0:
                break
            out += c
        return out

    def _dimension(self) -> str:
        out = ""
        n = len(self.chars)
        while self.i < n and (self.chars[self.i].isdigit() or self.chars[self.i] in ".-"):
            out += self.chars[self.i]
            self.i += 1
        while self.i < n and _is_ascii_letter(self.chars[self.i]) and len(out) < 12:
            out += self.chars[self.i]
            self.i += 1
            if out.endswith(("em", "mu", "pt", "ex")):
                break
        return out

    def _delimiter(self) -> str:
        """The delimiter after `\\left`, `\\right`, `\\big`…; `.` means none."""
        self._skip_spaces()
        if self.i >= len(self.chars):
            return "."
        if self.chars[self.i] == "\\":
            self.i += 1
            name = self._read_command()
            return DELIMITERS.get(name) or (COMMANDS[name][0] if name in COMMANDS else ".")
        c = self.chars[self.i]
        self.i += 1
        return {"<": "⟨", ">": "⟩"}.get(c, c)
