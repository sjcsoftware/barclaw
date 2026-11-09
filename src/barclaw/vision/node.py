"""BarClaw as an OpenClaw **node**: a second connection (same device, role `node`) offering `screen.snapshot`.

It publishes a `barclaw_screen` agent tool and a `barclaw-screen` skill, and answers the agent's
screen requests. `screen.snapshot` is on OpenClaw's default Linux command policy, and node tools and
skills are accepted by default, so nothing has to be configured on the gateway beyond a one-time
approval, which BarClaw grants itself when it has admin access and otherwise explains.
"""
from __future__ import annotations

import json
import socket
from typing import Any, Callable, Optional

from ..gateway.client import ConnectionProfile, GatewayClient, normalized_url
from ..gateway.identity import SecureStore
from ..support import ExpectedError, Observable, Task, as_list, clamp, dig, log, number_of, observed, sleep, spawn, text_of
from .context import ScreenContext
from .reader import CaptureOptions, ScreenReader
from .snapshot import ScreenReadError, ScreenSnapshot

COMMAND = "screen.snapshot"
TOOL_NAME = "barclaw_screen"
SKILL_NAME = "barclaw-screen"
PLUGIN_ID = "barclaw"

TOOL_DESCRIPTOR = {
    "pluginId": PLUGIN_ID,
    "name": TOOL_NAME,
    "command": COMMAND,
    "description": (
        "See what's on the user's Linux desktop right now. Returns a fresh screenshot of the display they're "
        "working on (BarClaw's own panel is left out), the active app and window title, and on-device OCR text. "
        'Call it whenever the user points at something on their screen ("this", "this page", "what I\'m looking '
        'at", an error, email, document, chart, or foreign-language text) or asks you to read, summarize, '
        "translate, explain, proofread, or pull something out of it. Skip it when the current message already "
        "carries a <barclaw-screen> attachment that answers the question."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "screenIndex": {
                "type": "integer", "minimum": 0,
                "description": "Display to capture: 0 is the first display. Omit to capture the display the user is working on.",
            },
            "maxWidth": {
                "type": "integer", "minimum": 320, "maximum": 3840,
                "description": "Width of the returned image in pixels (default 1600). Use 2400 or more when text is small.",
            },
        },
        "additionalProperties": False,
    },
}

SKILL_CONTENT = """---
name: barclaw-screen
description: Read, summarize, translate, or explain what's on the user's Linux desktop through BarClaw.
---

# BarClaw screen

The user runs BarClaw, a menu bar app on their Linux desktop that is paired with you as a node. It can show you their screen: a screenshot plus on-device OCR text, the active app, and the window title.

## When to look

- They point at something visible: "this", "this page", "this error", "what am I looking at", "what does it say", "is this legit?".
- They ask you to summarize, translate, explain, proofread, fact-check, or answer questions about what's on screen, or to pull out tables, dates, to-dos, links, code, or numbers from it.
- Don't look when the request doesn't need the screen.

## How

1. If the message starts with a `<barclaw-screen>` block, the screenshot is already attached (captured when they hit send). Use it and don't capture again unless you need a fresh or sharper look.
2. Otherwise call the `barclaw_screen` tool (it can appear with a node-id prefix if names collide). Pass `maxWidth: 2400` or more when text is small. Pass `screenIndex` only when they name another display.
3. Use the image for layout, visuals, and what's highlighted; use the OCR text for exact wording. OCR can garble small or stylized text, so trust the image when they disagree.
4. Focus on the main content (the article, email, document, error) and ignore panels, docks, and unrelated windows unless asked.

## Tasks

- **Summarize**: lead with a one-sentence gist, then the key points as a short list. Keep names, numbers, and dates exact.
- **Translate**: detect the source language. Translate into the language the user wrote in unless they name another. Keep the structure (headings, lists, tables), translate all of the main content rather than summarizing it, and keep untranslatable names as they are. For short UI text, show original → translation pairs.
- **Explain or answer**: answer the question directly, quoting the on-screen text you relied on.
- **Errors and code**: name the cause, then give the fix.

## Privacy

- Look only when the request needs it. Don't bring up unrelated private content you happen to see (notifications, other chats, passwords, tokens) and never repeat secrets.
- If the tool says visual intelligence is off or capture isn't allowed, tell the user to turn it on in BarClaw Settings › Screen.
"""

SKILL_DESCRIPTOR = {
    "name": SKILL_NAME,
    "description": "Read, summarize, translate, or explain what's on the user's Linux desktop through BarClaw.",
    "content": SKILL_CONTENT,
}


def node_profile(can_capture: bool) -> ConnectionProfile:
    host = socket.gethostname() or "Linux"
    return ConnectionProfile(
        role="node", client_mode="node", scopes=[], caps=["screen"], commands=[COMMAND],
        permissions={"screenRecording": can_capture},
        display_name=f"BarClaw ({host})", storage_key="node|",
    )


def own_entry(listing: Any, node_id: str) -> Optional[dict]:
    return next((n for n in as_list(dig(listing, "nodes")) if dig(n, "nodeId") == node_id), None)


def surface_from(entry: Optional[dict]) -> tuple:
    """Approval of the node's command surface: (`approved`|`pending`|`unknown`, request id)."""
    if entry is None:
        return ("unknown", None)
    if COMMAND in [c for c in as_list(entry.get("commands")) if isinstance(c, str)]:
        return ("approved", None)
    request_id = entry.get("pendingRequestId")
    if isinstance(request_id, str):
        return ("pending", request_id)
    # Connected without the command and without a visible request: still awaiting approval.
    return ("pending", None) if entry.get("connected") is True else ("unknown", None)


def pending_request(listing: Any, device_id: str, keys: list) -> Optional[str]:
    pending = as_list(dig(listing, "pending")) or as_list(dig(listing, "requests"))
    for entry in pending:
        if any(dig(entry, key) == device_id for key in keys):
            value = dig(entry, "requestId")
            return value if isinstance(value, str) else None
    return None


def options_from_params(params_json: Optional[str]) -> CaptureOptions:
    """Reads OpenClaw's `screen.snapshot` params (`screenIndex`, `maxWidth`, `quality`, `format`)."""
    options = CaptureOptions()
    if not params_json:
        return options
    try:
        params = json.loads(params_json)
    except ValueError:
        return options
    if not isinstance(params, dict):
        return options
    index = number_of(params.get("screenIndex"))
    if index is not None and index >= 0:
        options.screen_index = int(index)
    width = number_of(params.get("maxWidth"))
    if width is not None and width > 0:
        options.max_width = int(width)
    quality = number_of(params.get("quality"))
    if quality is not None and quality > 0:
        options.quality = clamp(quality / 100 if quality > 1 else quality, 0.3, 1)
    if params.get("format") == "png":
        options.format = "png"
    return options


def invoke_payload(snapshot: ScreenSnapshot) -> dict:
    """One payload satisfying OpenClaw's `screen.snapshot` result and an agent tool result."""
    encoded = snapshot.base64
    return {
        "format": snapshot.format,
        "base64": encoded,
        "width": snapshot.width,
        "height": snapshot.height,
        "screenIndex": snapshot.display_index,
        "capturedAtMs": int(snapshot.captured_at * 1000),
        "content": [
            {"type": "text", "text": ScreenContext.tool_text(snapshot)},
            {"type": "image", "data": encoded, "mimeType": snapshot.mime_type},
        ],
    }


def failure(error: ScreenReadError) -> dict:
    return {"ok": False, "error": {"code": error.code, "message": str(error)}}


class ScreenNode(Observable):
    surface: tuple = observed(("unknown", None))
    approving: bool = observed(False)
    last_agent_read: Optional[float] = observed(None)
    publish_error: Optional[str] = observed(None)

    def __init__(self, operator: GatewayClient, settings: Any, reader: ScreenReader, store: Optional[SecureStore] = None) -> None:
        super().__init__()
        self.operator = operator
        self.settings = settings
        self.reader = reader
        self.client = GatewayClient(node_profile(True), store or operator.store)
        self.client.on_event = self._handle_event
        self.client.on_connected = self._node_connected
        self.client.observe(self._state_changed)
        self.on_agent_read: Optional[Callable[[Any], None]] = None
        self._surface_task: Optional[Task] = None
        self._approved_requests: set = set()
        self._reconnected_for_approval = False
        self._last_pairing_handled: Optional[str] = None

    @property
    def status(self) -> tuple:
        """`(kind, request_id_or_message)`: off, waiting, connecting, needs_device, needs_command, approving, ready, failed."""
        if not self.settings.get("visual_intelligence"):
            return ("off", None)
        if self.approving:
            return ("approving", None)
        state = self.client.state
        if state.kind == "idle":
            return ("waiting", None)
        if state.kind in ("connecting", "offline"):
            return ("connecting" if self.operator.state.is_connected else "waiting", None)
        if state.kind == "pairing":
            return ("needs_device", state.request_id)
        if state.kind == "auth_failed":
            return ("failed", state.message)
        if self.surface[0] == "pending":
            return ("needs_command", self.surface[1])
        if self.surface[0] == "checking":
            return ("connecting", None)
        # Older gateways may not report the surface; a connected node is the best signal there.
        return ("ready", None)

    def apply(self) -> None:
        """Re-evaluates whether the node should be connected. Call when settings or the operator change."""
        if not self.settings.get("visual_intelligence") or normalized_url(self.settings.get("gateway_url")) is None or not self.operator.state.is_connected:
            if self.client.state.kind != "idle":
                self.client.stop()
            if self._surface_task is not None:
                self._surface_task.cancel()
            self.surface = ("unknown", None)
            return
        self.client.configure(self.settings.get("gateway_url"), self.settings.gateway_token)
        if self.client.state.is_connected:
            self._schedule_surface_check(0)

    @property
    def approval_commands(self) -> list:
        kind, value = self.status
        if kind == "needs_device":
            return ["openclaw devices list", f"openclaw devices approve {value or '--latest'}"]
        if kind == "needs_command":
            return [f"openclaw nodes approve {value}"] if value else ["openclaw nodes pending", "openclaw nodes approve <requestId>"]
        return []

    # MARK: Connection

    def _state_changed(self) -> None:
        self.changed()
        state = self.client.state
        if state.kind == "pairing":
            spawn(self._auto_approve_device(state.request_id))

    def _node_connected(self) -> None:
        log.info("Node connected; publishing %s and %s", TOOL_NAME, SKILL_NAME)
        if self.surface[0] not in ("approved", "pending"):
            self.surface = ("checking", None)
        spawn(self._publish())
        self._schedule_surface_check(0.5)

    def _publish(self):
        """Publishes the agent tool and skill. Both are replaced wholesale and vanish on disconnect."""
        problems = []
        try:
            yield self.client.request("node.pluginTools.update", {"tools": [TOOL_DESCRIPTOR]}, 15)
        except ExpectedError as error:
            problems.append(f"tool: {error}")
        try:
            yield self.client.request("node.skills.update", {"skills": [SKILL_DESCRIPTOR]}, 15)
        except ExpectedError as error:
            problems.append(f"skill: {error}")
        self.publish_error = "; ".join(problems) or None
        if self.publish_error:
            log.error("Node publication failed: %s", self.publish_error)

    # MARK: Approval

    def _schedule_surface_check(self, delay: float) -> None:
        if self._surface_task is not None:
            self._surface_task.cancel()

        def body():
            if delay > 0:
                yield sleep(delay)
            while self.client.state.is_connected:
                pending = yield spawn(self._check_surface())
                if not pending:
                    return
                yield sleep(5)

        self._surface_task = spawn(body())

    def _check_surface(self):
        if not self.operator.state.is_connected:
            return True
        try:
            listing = yield self.operator.request("node.list", {}, 15)
        except ExpectedError:
            self.surface = ("unknown", None)
            return False
        kind, request_id = surface_from(own_entry(listing, self.client.device_id))
        if kind == "approved":
            if self.surface[0] == "pending":
                log.info("Node command surface approved")
                yield spawn(self._publish())  # Descriptors are filtered to the approved surface; publish again.
            self.surface = ("approved", None)
            self._reconnected_for_approval = False
            return False
        if kind == "pending":
            if request_id is None and self.surface[0] == "pending" and self.surface[1] is not None and not self._reconnected_for_approval:
                # The request was resolved but the command hasn't appeared: reconnect to pick it up.
                self._reconnected_for_approval = True
                self.client.reconnect_now("node approval changed")
            self.surface = ("pending", request_id)
            yield spawn(self._auto_approve_surface(request_id))
            return True
        self.surface = ("unknown", None)
        return False

    def _auto_approve_device(self, request_id: Optional[str]):
        """Approves this device's node role with the operator's admin access, if it has it."""
        if not self.operator.has_admin or self.approving:
            return
        self.approving = True
        try:
            target = request_id
            if target is None:
                listing = yield self.operator.request("device.pair.list", {}, 15)
                target = pending_request(listing, self.client.device_id, ["deviceId"])
            if target is None or target in self._approved_requests:
                return
            self._approved_requests.add(target)
            yield self.operator.request("device.pair.approve", {"requestId": target}, 15)
            log.info("Approved this machine's node role (%s)", target)
            self.client.reconnect_now("node device approved")
        except ExpectedError as error:
            log.error("Couldn't approve node role automatically: %s", error)
        finally:
            self.approving = False

    def _auto_approve_surface(self, request_id: Optional[str]):
        """Approves `screen.snapshot` for this node with the operator's admin access, if it has it."""
        if not self.operator.has_admin or self.approving:
            return
        self.approving = True
        try:
            target = request_id
            if target is None:
                listing = yield self.operator.request("node.pair.list", {}, 15)
                target = pending_request(listing, self.client.device_id, ["nodeId", "deviceId"])
            if target is None or target in self._approved_requests:
                return
            self._approved_requests.add(target)
            yield self.operator.request("node.pair.approve", {"requestId": target}, 15)
            log.info("Approved node screen access (%s)", target)
            self._reconnected_for_approval = True
            self.client.reconnect_now("node surface approved")
        except ExpectedError as error:
            log.error("Couldn't approve node screen access automatically: %s", error)
        finally:
            self.approving = False

    # MARK: Invokes

    def _handle_event(self, name: str, payload: Any) -> None:
        if name != "node.invoke.request":
            return
        request_id = text_of(dig(payload, "id"))
        if request_id is None:
            return
        spawn(self._answer(
            request_id, text_of(dig(payload, "nodeId")) or self.client.device_id,
            dig(payload, "command") or "", dig(payload, "paramsJSON"),
        ))

    def _answer(self, request_id: str, node_id: str, command: str, params_json: Optional[str]):
        result: dict = {"id": request_id, "nodeId": node_id}
        if command != COMMAND:
            result.update(ok=False, error={"code": "UNSUPPORTED_COMMAND", "message": f"BarClaw only supports {COMMAND}."})
        elif not self.settings.get("visual_intelligence"):
            error = ScreenReadError.disabled()
            result.update(failure(error))
            if self.on_agent_read:
                self.on_agent_read(error)
        else:
            options = options_from_params(params_json)
            options.ocr_timeout = 10  # A tool call can wait a little longer than a reply the user is watching.
            try:
                snapshot = yield self.reader.capture(options)
                result.update(ok=True, payload=invoke_payload(snapshot))
                import time

                self.last_agent_read = time.time()
                self.surface = ("approved", None)
                if self.on_agent_read:
                    self.on_agent_read(snapshot)
            except ScreenReadError as error:
                result.update(failure(error))
                if self.on_agent_read:
                    self.on_agent_read(error)
                if error.code == "SCREEN_PERMISSION_DENIED":
                    self.client.update_profile(lambda p: setattr(p, "permissions", {"screenRecording": False}))
        try:
            yield self.client.request("node.invoke.result", result, 30)
        except ExpectedError as error:
            log.error("Couldn't return screen.snapshot result: %s", error)
