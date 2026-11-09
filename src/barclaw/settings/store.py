"""User preferences, persisted as JSON. The gateway token lives in `SecureStore`."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..gateway.identity import SecureStore
from ..support import Observable, config_dir, log, read_json, write_private

USAGE_WINDOWS = {"five_hour": ("5 hours", "5h"), "weekly": ("Weekly", "Week")}

DEFAULTS: dict[str, Any] = {
    "gateway_url": "ws://127.0.0.1:18789",
    "session_key": "main",
    "agent_emoji": "🦞",
    "agent_name": "OpenClaw",
    "show_usage": True,
    "usage_window": "five_hour",
    "max_height_fraction": 0.5,
    "max_width_fraction": 1 / 3,
    "glow_on_reply": True,
    "notify_needs_input": True,
    "notify_on_reply": False,
    "inject_markdown": True,
    "push_to_talk": "<Alt>space",
    "push_to_talk_enabled": True,
    "speech_engine": "vosk",  # vosk | command
    "speech_language": "en-us",
    "speech_command": "",
    "auto_send_voice": True,
    "request_admin_access": False,
    "visual_intelligence": True,
    "auto_attach_screen": True,
    "autostart": False,
}


class AppSettings(Observable):
    def __init__(self, path: Optional[Path] = None) -> None:
        super().__init__()
        self.path = path or config_dir() / "settings.json"
        stored = read_json(self.path)
        self._values = {key: stored.get(key, default) for key, default in DEFAULTS.items()}
        for key, default in DEFAULTS.items():
            # A hand-edited file shouldn't be able to feed a string to a slider.
            if isinstance(default, (int, float)) and not isinstance(default, bool):
                if not isinstance(self._values[key], (int, float)) or isinstance(self._values[key], bool):
                    self._values[key] = default
            elif type(self._values[key]) is not type(default):
                self._values[key] = default
        if self._values["usage_window"] not in USAGE_WINDOWS:
            self._values["usage_window"] = "five_hour"
        self._token = SecureStore.shared().gateway_token

    def get(self, key: str) -> Any:
        return self._values[key]

    def set(self, key: str, value: Any) -> None:
        if self._values.get(key) == value:
            return
        self._values[key] = value
        self._save()
        self.changed()

    @property
    def gateway_token(self) -> str:
        return self._token

    @gateway_token.setter
    def gateway_token(self, value: str) -> None:
        if value != self._token:
            self._token = value
            SecureStore.shared().gateway_token = value
            self.changed()

    @property
    def resolved_session_key(self) -> str:
        return self.get("session_key").strip() or "main"

    @property
    def resolved_emoji(self) -> str:
        return self.get("agent_emoji").strip() or "🦞"

    @property
    def resolved_name(self) -> str:
        return self.get("agent_name").strip() or "OpenClaw"

    def _save(self) -> None:
        try:
            write_private(self.path, json.dumps(self._values, indent=2, ensure_ascii=False).encode())
        except OSError as error:
            log.error("Failed to save settings: %s", error)
