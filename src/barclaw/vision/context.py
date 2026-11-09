"""Deciding when a message is about the screen, and the text block that travels with a screenshot."""
from __future__ import annotations

import datetime
import re
from typing import Optional

from .snapshot import ScreenSnapshot

_SCREEN_PHRASES = (
    "on my screen", "on the screen", "on screen", "my screen", "the screen", "this screen",
    "my display", "my monitor", "screenshot of my", "what am i looking at", "what i'm looking at",
    "what im looking at", "what i am looking at", "what's in front of me", "what is in front of me",
    "화면", "스크린", "画面", "スクリーン", "屏幕", "螢幕",
)
_NOUNS = (
    "page", "tab", "window", "site", "website", "webpage", "article", "document", "doc", "pdf", "email",
    "mail", "error", "warning", "alert", "dialog", "popup", "pop-up", "code", "function", "chart",
    "graph", "table", "slide", "image", "picture", "photo", "diagram", "equation", "formula",
    "paragraph", "sentence", "text", "word", "form", "spreadsheet", "sheet", "tweet", "post",
    "thread", "repo", "issue", "pull request", "menu", "button", "setting", "settings",
    "video", "receipt", "invoice", "contract", "recipe", "schedule", "calendar", "map",
    "problem", "exercise", "terminal output", "log", "logs", "stack trace", "message on",
)
_VERBS = (
    "summarize", "summarise", "sum up", "tl;dr", "tldr", "translate", "explain", "read",
    "describe", "proofread", "check", "review", "fix", "solve", "analyze", "analyse",
    "interpret", "transcribe", "extract", "simplify", "rewrite", "reply to", "respond to",
    "what's", "whats", "what is", "who is", "who's", "where is", "what does", "what do",
)
_CJK_POINTERS = (
    "이거", "이것", "이건", "이게", "이걸", "여기", "이 페이지", "이 글", "이 문서", "이 에러", "이 오류",
    "이 메일", "이 코드", "이 문장", "이 표", "이 그래프", "これ", "この", "ここ", "这个", "這個", "这里", "這裡", "这些",
)
_CJK_TASKS = (
    "번역", "요약", "설명", "읽어", "뭐", "무슨", "알려", "해석", "정리", "풀어", "고쳐", "봐줘", "확인",
    "翻訳", "要約", "説明", "何", "読んで", "翻译", "翻譯", "总结", "總結", "解释", "解釋", "什么", "什麼",
)
_CJK_SHORT_TASKS = ("번역", "요약", "翻訳", "要約", "翻译", "翻譯", "总结", "總結")


def _contains_phrase(text: str, phrase: str) -> bool:
    """Whole-word match for Latin phrases (so `this app` doesn't match `this apple`)."""
    start = 0
    while True:
        index = text.find(phrase, start)
        if index < 0:
            return False
        end = index + len(phrase)
        before_ok = index == 0 or not text[index - 1].isalpha()
        after_ok = end == len(text) or not text[end].isalpha()
        if before_ok and after_ok:
            return True
        start = end


def _mentions_screen(text: str) -> bool:
    return any(phrase in text for phrase in _SCREEN_PHRASES)


def _deictic_request(text: str) -> bool:
    for determiner in ("this", "that", "these", "those"):
        if any(_contains_phrase(text, f"{determiner} {noun}") for noun in _NOUNS):
            return True
    # Verb + "this/these/here" (not "that"/"it", which usually point back at the chat).
    for verb in _VERBS:
        if any(_contains_phrase(text, f"{verb} {target}") for target in ("this", "these", "here")):
            return True
    return any(_contains_phrase(text, p) for p in ("what's this", "whats this", "what is this", "what are these", "what's going on here"))


def _bare_task(text: str) -> bool:
    if len(text) > 48:
        return False
    if any(_contains_phrase(text, r) for r in ("you", "your", "our", "conversation", "chat", "above", "previous", "earlier", "last answer")):
        return False
    return any(t in text for t in ("translate", "translation", "summarize", "summarise", "summary", "tl;dr", "tldr", "sum up"))


def _cjk_request(text: str) -> bool:
    if any(p in text for p in _CJK_POINTERS) and any(t in text for t in _CJK_TASKS):
        return True
    return len(text) <= 20 and any(t in text for t in _CJK_SHORT_TASKS) and not any(w in text for w in ("너", "우리", "대화", "위에", "앞에"))


def _has_inline_payload(text: str) -> bool:
    """Long messages, quoted text, code, or a colon followed by content carry their own material."""
    if len(text) > 280 or "```" in text or text.count("\n") >= 2:
        return True
    colon = text.find(":")
    if colon >= 0 and len(text[colon + 1:].strip(" \t")) > 24:
        return True
    quoted = sum(text.count(q) for q in "\"“”「」'")
    return quoted >= 2 and len(text) > 30


def refers_to_screen(raw: str) -> bool:
    """Deliberately conservative: a false positive leaks a screenshot the user didn't intend to share."""
    text = raw.strip().lower()
    if not text or text.startswith("/"):
        return False
    if _has_inline_payload(text):
        return _mentions_screen(text)
    return _mentions_screen(text) or _deictic_request(text) or _bare_task(text) or _cjk_request(text)


class ScreenContext:
    """Builds and strips the `<barclaw-screen>` block that rides along with a shared screenshot."""

    OPEN = "<barclaw-screen"
    CLOSE = "</barclaw-screen>"

    @classmethod
    def block(cls, snapshot: ScreenSnapshot, ocr_limit: int = 4000) -> str:
        app = snapshot.app_description or "unknown"
        lines = [
            f'{cls.OPEN} app="{cls._escape(app)}">',
            "The user's Linux desktop, captured by BarClaw when they sent this message, is attached as an image"
            f" ({snapshot.width}×{snapshot.height}, display {snapshot.display_index + 1} of {snapshot.display_count})."
            f" Active app: {app}. BarClaw's own panel is not in the picture.",
            'Use the image for layout and visuals and the OCR text for exact wording. Words like "this" or'
            ' "here" most likely mean what\'s on screen; if the message is clearly about the conversation'
            " instead, ignore the screenshot.",
        ]
        ocr = cls._truncated(snapshot.text, ocr_limit)
        if ocr:
            lines.append(f"<ocr>\n{ocr}\n</ocr>")
        lines.append(cls.CLOSE)
        return "\n".join(lines)

    @classmethod
    def tool_text(cls, snapshot: ScreenSnapshot) -> str:
        """The text block returned with a `screen.snapshot` that the agent asked for itself."""
        stamp = datetime.datetime.fromtimestamp(snapshot.captured_at).astimezone().isoformat(timespec="seconds")
        lines = [
            f"BarClaw screen capture at {stamp}.",
            f"Display {snapshot.display_index + 1} of {snapshot.display_count} ({snapshot.display_name}), image {snapshot.width}×{snapshot.height}.",
            f"Active app: {snapshot.app_description or 'unknown'}. BarClaw's own panel is excluded.",
        ]
        if snapshot.text:
            lines.append(f"On-device OCR text (top to bottom; may contain recognition errors):\n{snapshot.text}")
        else:
            lines.append("On-device OCR found no text.")
        return "\n".join(lines)

    @classmethod
    def strip(cls, text: str) -> Optional[tuple[str, Optional[str]]]:
        """Removes a leading screen block. Returns the remaining text and the app it named."""
        if not text.startswith(cls.OPEN) or cls.CLOSE not in text:
            return None
        header = text[: text.index(">")] if ">" in text else ""
        match = re.search(r'app="([^"]*)"', header)
        app = cls._unescape(match.group(1)) if match else None
        return text.split(cls.CLOSE, 1)[1].strip(), None if app == "unknown" else app

    @staticmethod
    def _truncated(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[:limit] + "\n[…truncated]"

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("&", "&amp;").replace('"', "&quot;").replace("\n", " ")

    @staticmethod
    def _unescape(value: str) -> str:
        return value.replace("&quot;", '"').replace("&amp;", "&")
