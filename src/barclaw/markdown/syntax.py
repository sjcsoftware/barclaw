"""A compact tokenizer that turns code into Pango markup for fenced blocks."""
from __future__ import annotations

import re
from html import escape
from typing import Optional

_C_LIKE = """if else for while do switch case default break continue return goto struct enum union class public private protected
static const void int char float double long short unsigned signed bool true false null new delete this typedef sizeof namespace using
template typename virtual override extern volatile inline try catch throw"""
KEYWORDS = {
    "python": """def class if elif else for while try except finally with as import from return yield lambda pass break continue and or not
    in is None True False raise global nonlocal assert del async await match case self""",
    "javascript": """function const let var if else for while do switch case default break continue return class extends new delete typeof
    instanceof in of try catch finally throw this super import export from as async await yield true false null undefined void static get set""",
    "typescript": """function const let var if else for while do switch case default break continue return class extends implements interface type
    enum namespace new delete typeof instanceof in of try catch finally throw this super import export from as async await yield true false null
    undefined void static readonly public private protected abstract string number boolean any unknown never""",
    "rust": """fn let mut const static if else match for while loop break continue return struct enum impl trait pub use mod crate self Self super
    as in ref move async await dyn where type true false unsafe""",
    "go": """func package import var const type struct interface map chan if else for range switch case default break continue return go defer
    select fallthrough true false nil""",
    "swift": """func let var if else guard for while repeat switch case default break continue return class struct enum protocol extension import
    self Self super init deinit try catch throw throws async await in is as nil true false public private internal static""",
    "java": _C_LIKE + " extends implements interface import package final abstract synchronized throws instanceof byte",
    "c": _C_LIKE,
    "shell": """if then elif else fi for while until do done case esac in function return exit local export readonly source sudo echo cd""",
    "sql": """select from where insert into values update set delete create table index view drop alter add join left right inner outer on group
    by order having limit offset union all distinct as and or not null is in like between exists primary key foreign references""",
    "ruby": """def end class module if elsif else unless while until for in do return yield begin rescue ensure raise require include self nil true false and or not""",
    "php": """function class if else elseif for foreach while do switch case default break continue return new public private protected static
    echo require include use namespace try catch finally throw true false null array""",
    "css": """important media import keyframes from to""",
    "json": """true false null""",
    "yaml": """true false null yes no""",
    "lua": """function local if then elseif else end for while do repeat until return break in and or not nil true false""",
}
ALIASES = {
    "py": "python", "python3": "python", "js": "javascript", "jsx": "javascript", "mjs": "javascript", "node": "javascript",
    "ts": "typescript", "tsx": "typescript", "rs": "rust", "golang": "go", "kt": "java", "kotlin": "java", "scala": "java", "cs": "java", "csharp": "java",
    "c++": "c", "cpp": "c", "cc": "c", "h": "c", "hpp": "c", "objc": "c", "bash": "shell", "sh": "shell", "zsh": "shell", "fish": "shell", "console": "shell",
    "terminal": "shell", "psql": "sql", "postgres": "sql", "mysql": "sql", "rb": "ruby", "yml": "yaml", "scss": "css", "less": "css",
}
HASH_COMMENTS = {"python", "shell", "ruby", "yaml"}
DASH_COMMENTS = {"sql", "lua"}
COLORS = {
    "comment": "#7f8a99", "string": "#a5e3a1", "number": "#f6c177", "keyword": "#c4a7ff",
    "function": "#7cc4ff", "type": "#6fe0d8", "plain": "#e8ebf0",
}


def normalize_language(language: Optional[str]) -> Optional[str]:
    if not language:
        return None
    name = language.strip().split()[0].lower() if language.strip() else ""
    name = ALIASES.get(name, name)
    return name if name in KEYWORDS else None


def _tokenizer(language: Optional[str]) -> re.Pattern:
    comments = [r"//[^\n]*", r"/\*.*?(?:\*/|\Z)"]
    if language in HASH_COMMENTS:
        comments = [r"#[^\n]*"]
    elif language in DASH_COMMENTS:
        comments = [r"--[^\n]*"]
    elif language == "php":
        comments.append(r"#[^\n]*")
    elif language in ("json",):
        comments = []
    elif language == "css":
        comments = [r"/\*.*?(?:\*/|\Z)"]
    strings = [r'"(?:\\.|[^"\\\n])*"?', r"'(?:\\.|[^'\\\n])*'?"]
    if language in ("python",):
        strings = [r'"""[\s\S]*?(?:"""|\Z)', r"'''[\s\S]*?(?:'''|\Z)"] + strings
    if language in ("javascript", "typescript", "go", "shell"):
        strings.append(r"`(?:\\.|[^`\\])*`?")
    parts = []
    if comments:
        parts.append(f"(?P<comment>{'|'.join(comments)})")
    parts.append(f"(?P<string>{'|'.join(strings)})")
    parts.append(r"(?P<number>\b(?:0[xX][\da-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b)")
    parts.append(r"(?P<word>[A-Za-z_$][\w$]*)")
    return re.compile("|".join(parts), re.S)


_CACHE: dict = {}


def highlight(code: str, language: Optional[str]) -> str:
    """Pango markup for the code. Unknown languages still get strings, numbers, and comments."""
    name = normalize_language(language)
    if name not in _CACHE:
        words = frozenset((KEYWORDS.get(name, "") if name else "").split())
        _CACHE[name] = (_tokenizer(name), words)
    pattern, keywords = _CACHE[name]

    def span(kind: str, text: str) -> str:
        return f'<span foreground="{COLORS[kind]}">{escape(text, quote=False)}</span>'

    out, position = [], 0
    for match in pattern.finditer(code):
        out.append(escape(code[position:match.start()], quote=False))
        position = match.end()
        kind = match.lastgroup
        text = match.group()
        if kind == "word":
            following = code[match.end():match.end() + 1]
            if text in keywords:
                kind = "keyword"
            elif following == "(":
                kind = "function"
            elif text[0].isupper() and name is not None:
                kind = "type"
            else:
                out.append(escape(text, quote=False))
                continue
        out.append(span(kind, text))
    out.append(escape(code[position:], quote=False))
    return "".join(out)
