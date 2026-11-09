"""Plain data the session publishes to the UI."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..support import clamp, number_of, text_of

DEFAULT_THINKING_OPTIONS = ["off", "minimal", "low", "medium", "high", "xhigh"]


@dataclass
class ScreenShare:
    """A screenshot shared with a user message. `state` is capturing | attached | unavailable."""

    state: str
    app: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class ImageAttachment:
    """An image the agent produced, referenced by artifact id or inlined."""

    id: str
    artifact_id: Optional[str] = None
    inline_base64: Optional[str] = None
    mime_type: Optional[str] = None
    alt: Optional[str] = None
    width: Optional[float] = None
    height: Optional[float] = None


@dataclass
class ChatItem:
    id: str
    role: str  # user | assistant | notice | error
    text: str
    date: float = 0.0
    run_id: Optional[str] = None
    is_streaming: bool = False
    images: list[ImageAttachment] = field(default_factory=list)
    screen: Optional[ScreenShare] = None


@dataclass(frozen=True)
class AgentActivity:
    kind: str = "idle"  # idle | thinking | streaming | compacting
    phase: Optional[str] = None

    @property
    def is_busy(self) -> bool:
        return self.kind != "idle"


IDLE = AgentActivity()


@dataclass
class PendingApproval:
    id: str
    title: str
    detail: Optional[str]
    decisions: list[str]
    expires_at: Optional[float]


@dataclass
class UsageWindow:
    label: str
    used_percent: float
    reset_at: Optional[float] = None

    @property
    def choice(self) -> Optional[str]:
        """Which user-facing bucket this window represents: `five_hour` or `weekly`."""
        label = self.label.lower()
        if "week" in label or label == "7d":
            return "weekly"
        if label.endswith("h") and label[:-1].isdigit() and int(label[:-1]) < 24:
            return "five_hour"
        return None


@dataclass
class UsageInfo:
    provider_name: str
    plan: Optional[str]
    windows: list[UsageWindow]
    error: Optional[str] = None

    def window(self, choice: str) -> Optional[UsageWindow]:
        return next((w for w in self.windows if w.choice == choice), None)

    def best_window(self, choice: str) -> Optional[UsageWindow]:
        return self.window(choice) or (self.windows[0] if self.windows else None)


@dataclass(frozen=True)
class ModelOption:
    id: str  # "provider/model"
    name: str
    provider: str


@dataclass
class SessionSettings:
    """The session's effective settings as reported by the gateway."""

    model: Optional[str] = None
    model_provider: Optional[str] = None
    thinking_level: Optional[str] = None
    thinking_options: list[str] = field(default_factory=lambda: list(DEFAULT_THINKING_OPTIONS))
    total_tokens: Optional[float] = None
    context_tokens: Optional[float] = None

    @property
    def model_ref(self) -> Optional[str]:
        if not self.model:
            return None
        if "/" in self.model or not self.model_provider:
            return self.model
        return f"{self.model_provider}/{self.model}"

    @property
    def context_fraction(self) -> Optional[float]:
        if self.total_tokens is None or not self.context_tokens:
            return None
        return clamp(self.total_tokens / self.context_tokens, 0, 1)

    def merge(self, row: Any) -> None:
        if not isinstance(row, dict):
            return
        if text_of(row.get("model")):
            self.model = text_of(row["model"])
        if text_of(row.get("modelProvider")):
            self.model_provider = text_of(row["modelProvider"])
        if "thinkingLevel" in row:
            self.thinking_level = text_of(row["thinkingLevel"])
        options = row.get("thinkingOptions")
        if isinstance(options, list):
            cleaned = [t for t in map(text_of, options) if t]
            if cleaned:
                self.thinking_options = cleaned
        for key, attr in (("totalTokens", "total_tokens"), ("contextTokens", "context_tokens")):
            value = number_of(row.get(key))
            if value is not None:
                setattr(self, attr, value)


@dataclass
class SessionSummary:
    key: str
    title: str
    updated_at: Optional[float] = None


def session_title_for_key(key: str) -> str:
    """`agent:main:main` → `Main`; `agent:main:abc123` → `abc123`."""
    last = key.split(":")[-1] if key else key
    if last.lower() == "main" or key.lower() == "main":
        return "Main"
    return last or "Session"


def session_title(row: dict) -> str:
    for key in ("label", "displayName", "derivedTitle", "autoLabel"):
        explicit = text_of(row.get(key))
        if explicit:
            return explicit
    return session_title_for_key(row.get("key") if isinstance(row.get("key"), str) else "")


@dataclass
class Step:
    id: str
    title: str
    is_active: bool
    is_error: bool = False


@dataclass
class PlanItem:
    text: str
    status: str  # pending | in_progress | completed


@dataclass
class RunProgress:
    """Live view of what the agent is doing: tool steps, its latest commentary, and any plan."""

    started_at: Optional[float] = None
    steps: list[Step] = field(default_factory=list)
    note: Optional[str] = None
    plan: list[PlanItem] = field(default_factory=list)
    phase: Optional[str] = None

    @property
    def active_step(self) -> Optional[Step]:
        return next((s for s in reversed(self.steps) if s.is_active), None)

    @property
    def is_empty(self) -> bool:
        return not self.steps and self.note is None and not self.plan

    @property
    def headline(self) -> Optional[str]:
        """One line for headers and bubbles: what's happening right now."""
        if self.active_step:
            return self.active_step.title
        current = next((p for p in self.plan if p.status == "in_progress"), None)
        if current:
            return current.text
        if self.note:
            return self.note
        if self.steps:
            return f"Done: {self.steps[-1].title}"
        return self.phase

    def upsert(self, step_id: str, title: Optional[str], active: bool, error: bool = False) -> None:
        for step in self.steps:
            if step.id == step_id:
                if title:
                    step.title = title
                step.is_active = active
                step.is_error = step.is_error or error
                break
        else:
            if title:
                self.steps.append(Step(step_id, title, active, error))
        del self.steps[:-30]

    def finish_all(self) -> None:
        for step in self.steps:
            step.is_active = False


def tool_title(name: str, args: Any) -> str:
    """Fallback labels when the gateway sends a raw tool name without a title."""
    lower = name.lower()

    def arg(*keys: str) -> Optional[str]:
        if isinstance(args, dict):
            for key in keys:
                value = text_of(args.get(key))
                if value:
                    return value
        return None

    def clip(text: str, limit: int = 48) -> str:
        line = text.split("\n", 1)[0]
        return line[: limit - 1] + "…" if len(line) > limit else line

    if "exec" in lower or lower == "bash" or "shell" in lower or "command" in lower:
        value = arg("command", "cmd", "input")
        return f"Running `{clip(value)}`" if value else "Running a command"
    if "search" in lower:
        value = arg("query", "q")
        return f"Searching “{clip(value, 40)}”" if value else "Searching the web"
    if "fetch" in lower or "browse" in lower or "web" in lower:
        value = arg("url")
        return f"Reading {clip(value, 44)}" if value else "Browsing"
    if "read" in lower:
        value = arg("path", "file")
        return f"Reading {clip(value, 44)}" if value else "Reading files"
    if "write" in lower or "edit" in lower or "patch" in lower:
        value = arg("path", "file")
        return f"Editing {clip(value, 44)}" if value else "Editing files"
    if "image" in lower:
        return "Generating an image"
    if "memory" in lower:
        return "Checking memory"
    return "Using " + name.replace("_", " ").replace(".", " ")
