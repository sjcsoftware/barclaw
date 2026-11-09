"""Spoken control phrases that adjust the session instead of being sent to the agent."""
from __future__ import annotations

import re
import string
from dataclasses import dataclass
from typing import Optional, Sequence

_STRIP = string.whitespace + string.punctuation


@dataclass(frozen=True)
class VoiceCommand:
    kind: str  # set_model | set_effort | compact | new_session | stop
    value: Optional[str] = None


def normalize_effort(raw: str) -> Optional[str]:
    return {
        "off": "off", "none": "off", "zero": "off", "no": "off",
        "minimal": "minimal", "minimum": "minimal", "min": "minimal",
        "low": "low", "light": "low",
        "medium": "medium", "normal": "medium", "mid": "medium", "default level": "medium",
        "high": "high", "hard": "high",
        "max": "xhigh", "maximum": "xhigh", "extra high": "xhigh", "x high": "xhigh",
        "xhigh": "xhigh", "very high": "xhigh", "ultra": "xhigh",
    }.get(raw.strip())


def parse(transcript: str) -> Optional[VoiceCommand]:
    text = transcript.lower().strip(_STRIP).replace("  ", " ")
    if not text or len(text.split(" ")) > 9:
        return None
    if text in ("stop", "cancel", "stop it", "stop generating", "abort"):
        return VoiceCommand("stop")
    if re.fullmatch(r"(please )?(compact|compress|summarize)( the| my)?( context| session| conversation| history)?( now)?", text):
        return VoiceCommand("compact")
    if re.fullmatch(r"(please )?(start a |start |begin a |begin |open a )?(new|fresh) (session|conversation|chat)", text) or text in (
        "reset session", "reset the session", "start over", "clear session", "clear the session",
    ):
        return VoiceCommand("new_session")
    match = re.fullmatch(r"(?:please )?(?:set |change |switch )?(?:the )?(?:effort|thinking|reasoning)(?: level)?(?: to)? (.+)", text)
    if match:
        level = normalize_effort(match.group(1))
        if level:
            return VoiceCommand("set_effort", level)
    for pattern in (
        r"(?:please )?(?:set|switch|change|use)(?: the)? model(?: to)? (.+)",
        r"(?:please )?(?:switch|change) to (?:model )?(.+)",
        r"(?:please )?use (.+?) model",
    ):
        match = re.fullmatch(pattern, text)
        if match:
            return VoiceCommand("set_model", match.group(1))
    return None


def _normalize(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def _spoken_numbers(text: str) -> str:
    words = {
        "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "six": "6", "seven": "7", "eight": "8", "nine": "9", "point": ".", "dot": ".",
    }
    return " ".join(words.get(word, word) for word in text.split(" ") if word)


def match_model(query: str, models: Sequence) -> Optional[object]:
    """Picks the closest model for a spoken name like `gpt five codex` or `sonnet`."""
    q = _normalize(_spoken_numbers(query))
    if not q:
        return None
    best, best_score = None, 0
    for model in models:
        score = 0
        for candidate in (_normalize(model.id), _normalize(model.name), _normalize(model.id.rsplit("/", 1)[-1])):
            if candidate == q:
                score = max(score, 100)
            elif candidate.endswith(q) or candidate.startswith(q):
                score = max(score, 80 - abs(len(candidate) - len(q)))
            elif q in candidate:
                score = max(score, 60 - abs(len(candidate) - len(q)))
        if score > 0 and score > best_score:
            best, best_score = model, score
    return best
