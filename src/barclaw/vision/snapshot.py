"""One look at the user's screen, and the ways taking one can fail."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Optional


@dataclass
class ScreenSnapshot:
    image_data: bytes
    format: str  # "jpeg" or "png", as OpenClaw's `screen.snapshot` contract spells it
    width: int
    height: int
    display_index: int
    display_count: int
    display_name: str
    app_name: Optional[str]
    window_title: Optional[str]
    text: str  # On-device OCR, top to bottom. Empty when nothing was recognized.
    captured_at: float

    @property
    def mime_type(self) -> str:
        return "image/png" if self.format == "png" else "image/jpeg"

    @property
    def base64(self) -> str:
        return base64.b64encode(self.image_data).decode()

    @property
    def app_description(self) -> Optional[str]:
        """`Firefox — Page title`, or whichever half is known."""
        app, title = self.app_name or None, self.window_title or None
        if app and title and app != title:
            return f"{app} — {title}"
        return app or title


class ScreenReadError(Exception):
    def __init__(self, code: str, message: str, short_reason: str) -> None:
        super().__init__(message)
        self.code = code  # Reported in `node.invoke.result`.
        self.short_reason = short_reason

    @classmethod
    def disabled(cls) -> "ScreenReadError":
        return cls("SCREEN_DISABLED", "Visual intelligence is turned off in BarClaw Settings › Screen.", "Visual intelligence is off")

    @classmethod
    def denied(cls, detail: str = "") -> "ScreenReadError":
        message = "BarClaw isn't allowed to capture the screen."
        if detail:
            message += f" {detail}"
        return cls("SCREEN_PERMISSION_DENIED", message, "Screen capture was refused")

    @classmethod
    def no_display(cls) -> "ScreenReadError":
        return cls("SCREEN_NO_DISPLAY", "BarClaw couldn't find a display to capture.", "No display found")

    @classmethod
    def failed(cls, message: str) -> "ScreenReadError":
        return cls("SCREEN_CAPTURE_FAILED", f"BarClaw couldn't capture the screen: {message}", "Couldn't capture the screen")
