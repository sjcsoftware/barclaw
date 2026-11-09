"""Conversation and run state for one OpenClaw session, driven by gateway events."""
from __future__ import annotations

import base64
import time
import urllib.request
import uuid
from typing import Any, Callable, Optional

from gi.repository import GdkPixbuf, GLib

from ..gateway.client import GatewayClient, GatewayError
from ..support import (
    CancelledError, ExpectedError, Observable, Task, as_list, clamp, dig, in_thread, log, number_of, observed,
    sleep, spawn, text_of,
)
from ..vision.context import ScreenContext
from ..vision.snapshot import ScreenReadError, ScreenSnapshot
from .models import (
    IDLE, AgentActivity, ChatItem, ImageAttachment, ModelOption, PendingApproval, PlanItem, RunProgress, ScreenShare,
    SessionSettings, SessionSummary, UsageInfo, UsageWindow, session_title, session_title_for_key, tool_title,
)

MARKDOWN_INSTRUCTION = (
    "<barclaw-format>Reply in GitHub-flavored Markdown: short headings, lists, tables for comparisons, "
    "**bold** key terms, and fenced code blocks with a language tag. When numbers are easier to grasp visually, "
    "add a chart as a fenced ```chart block containing JSON like "
    '{"type":"bar|line|area|point|pie","title":"…","x":"axis label","y":"axis label",'
    '"data":[{"x":"Mon","y":3,"series":"optional"}]}. When a picture is requested or would '
    "clearly help, use your image generation tool. Keep it skimmable.</barclaw-format>"
)
FORMAT_CLOSE = "</barclaw-format>"
HISTORY_PAGE_SIZE = 150
MAX_IMAGE_BYTES = 12 * 1024 * 1024  # OpenClaw's client guidance caps source images at 12 MiB.


def extract_text(message: Any) -> Optional[str]:
    """Display text from gateway message objects (string or content blocks)."""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return extract_text({"content": message})
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type", "text") in ("text", "output_text", "markdown"):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
        return "\n\n".join(parts) if parts else None
    return message.get("text") if isinstance(message.get("text"), str) else None


def extract_images(message: Any) -> list[ImageAttachment]:
    """Image content blocks (`type: image`) with an artifact id or inline base64 data."""
    blocks = message.get("content") if isinstance(message, dict) else message
    found = []
    for block in as_list(blocks):
        if not isinstance(block, dict) or block.get("type") not in ("image", "output_image"):
            continue
        artifact_id = text_of(block.get("artifactId"))
        inline = text_of(block.get("data")) or text_of(dig(block, "source", "data"))
        image_url = text_of(block.get("image_url"))
        if inline is None and image_url and image_url.startswith("data:") and "," in image_url:
            inline = image_url.split(",", 1)[1]
        if artifact_id is None and inline is None:
            continue
        found.append(ImageAttachment(
            id=artifact_id or inline[-48:],
            artifact_id=artifact_id,
            inline_base64=inline,
            mime_type=text_of(block.get("mimeType")) or text_of(dig(block, "source", "media_type")),
            alt=text_of(block.get("alt")) or text_of(block.get("title")),
            width=number_of(block.get("width")),
            height=number_of(block.get("height")),
        ))
    return found


def strip_instruction(text: str) -> str:
    if text.startswith("<barclaw-format>") and FORMAT_CLOSE in text:
        return text.split(FORMAT_CLOSE, 1)[1].strip()
    return text


def unique_images(images: list[ImageAttachment]) -> list[ImageAttachment]:
    seen: set[str] = set()
    return [image for image in images if not (image.id in seen or seen.add(image.id))]


def matches_session(incoming: Optional[str], current: str) -> bool:
    if incoming is None:
        return False
    incoming, current = incoming.lower(), current.lower()
    if incoming == current:
        return True
    # "main" is the alias for the default agent's main session ("agent:<id>:main").
    parts = incoming.split(":")
    if len(parts) >= 3 and parts[0] == "agent" and ":".join(parts[2:]) == current:
        return True
    cparts = current.split(":")
    return len(cparts) >= 3 and cparts[0] == "agent" and ":".join(cparts[2:]) == incoming


def is_background_session(row: dict) -> bool:
    """Sub-agent, cron, and other spawned sessions aren't conversations to pick from."""
    key = str(row.get("key") or "").lower()
    if ":subagent:" in key or ":cron:" in key or key.startswith("cron:"):
        return True
    return text_of(row.get("spawnedBy")) is not None or text_of(row.get("parentSessionKey")) is not None


def parse_usage(result: Any) -> Optional[UsageInfo]:
    providers = [p for p in as_list(dig(result, "providers")) if isinstance(p, dict)]

    def is_codex(provider: dict) -> bool:
        pid = str(provider.get("provider") or "").lower()
        name = str(provider.get("displayName") or "").lower()
        return "codex" in pid or pid == "openai" or "codex" in name or "chatgpt" in name

    candidates = [p for p in providers if is_codex(p)]
    provider = next((p for p in candidates if as_list(p.get("windows"))), candidates[0] if candidates else None)
    if provider is None:
        return None
    windows = []
    for window in as_list(provider.get("windows")):
        label, used = text_of(dig(window, "label")), number_of(dig(window, "usedPercent"))
        if label is None or used is None:
            continue
        reset = number_of(dig(window, "resetAt"))
        windows.append(UsageWindow(label, clamp(used, 0, 100), reset / 1000 if reset else None))
    return UsageInfo(text_of(provider.get("displayName")) or "Codex", text_of(provider.get("plan")), windows, text_of(provider.get("error")))


def is_missing_admin(error: BaseException) -> bool:
    if not isinstance(error, GatewayError):
        return False
    if (error.details or {}).get("missingScope") == GatewayClient.ADMIN_SCOPE:
        return True
    return (error.code == "FORBIDDEN" and "operator.admin" in error.message) or "missing scope: operator.admin" in error.message.lower()


def phase_text(phase: Optional[str], retry: Any) -> Optional[str]:
    if retry is not None:
        return "Rate limited — retrying"
    return {
        "waiting_for_state": "Waiting", "preparing_workspace": "Preparing workspace",
        "naming_worktree": "Creating worktree", "creating_worktree": "Creating worktree",
        "running_setup": "Running setup", "provisioning_environment": "Provisioning",
        "preparing_context": "Gathering context", "memory_flushing": "Saving memory",
        "starting_model": "Waking the model",
    }.get(phase or "")


def short_model_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def last_sentences(text: str, limit: int = 160) -> str:
    """Keeps commentary short enough for a single line or two."""
    trimmed = text.strip()
    return trimmed if len(trimmed) <= limit else "…" + trimmed[-(limit - 1):].strip()


def fingerprint(item: ChatItem) -> str:
    return item.text.strip() + "|" + ",".join(image.id for image in item.images)


def history_items(result: Any, page: int) -> list[ChatItem]:
    """Displayable user/assistant messages from a chat.history page."""
    restored = []
    for index, message in enumerate(as_list(dig(result, "messages"))):
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            continue
        role = message["role"]
        text = extract_text(message) or ""
        attachments = extract_images(message) if role == "assistant" else []
        if not text and not attachments:
            continue
        screen = None
        if role == "user":
            stripped = ScreenContext.strip(text)
            if stripped is not None:
                text = stripped[0]
                screen = ScreenShare("attached", app=stripped[1])
            text = strip_instruction(text)
        meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
        item_id = meta.get("id") or message.get("id") or message.get("messageId") or f"h{page}-{index}"
        item = ChatItem(id=str(item_id), role=role, text=text, images=attachments, screen=screen, run_id=meta.get("runId"))
        stamp = number_of(message.get("timestamp")) or number_of(message.get("ts"))
        if stamp:
            item.date = stamp / 1000 if stamp > 1e12 else stamp
        restored.append(item)
    return restored


def decode_image(data: bytes) -> Optional[GdkPixbuf.Pixbuf]:
    loader = GdkPixbuf.PixbufLoader()
    try:
        loader.write(data)
        loader.close()
    except GLib.Error as error:
        log.error("Couldn't decode an image: %s", error.message)
        return None
    return loader.get_pixbuf()


def fetch_image_bytes(url: str) -> bytes:
    """Downloads a gateway media URL. The ticket in the URL is scoped to this one image."""
    request = urllib.request.Request(url, headers={"Accept": "image/*"})
    with urllib.request.urlopen(request, timeout=45) as response:
        if response.status != 200:
            raise GatewayError(f"HTTP_{response.status}", f"Image download returned HTTP {response.status}")
        data = response.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise GatewayError("TOO_LARGE", "Image is larger than 12 MB")
    return data


class AgentSession(Observable):
    items: list = observed([])
    activity: AgentActivity = observed(IDLE)
    attention: Optional[str] = observed(None)  # reply | needs_input | error
    approvals: list = observed([])
    usage: Optional[UsageInfo] = observed(None)
    models: list = observed([])
    toast: Optional[str] = observed(None)
    history_loaded: bool = observed(False)
    progress: RunProgress = observed(None)
    sessions: list = observed([])
    has_older_history: bool = observed(False)
    is_loading_older: bool = observed(False)

    def __init__(self, gateway: GatewayClient, settings: Any) -> None:
        super().__init__()
        self.gateway = gateway
        self.settings = settings
        self.items = []
        self.approvals = []
        self.session_settings = SessionSettings()
        self.progress = RunProgress()
        self.images: dict[str, GdkPixbuf.Pixbuf] = {}
        self._image_loads: set[str] = set()
        self._active_run_id: Optional[str] = None
        self._finalized_runs: set[str] = set()
        self._seen_content: dict[str, str] = {}
        self._reconcile: Optional[Task] = None
        self._toast_task: Optional[Task] = None
        self._sessions_reload: Optional[Task] = None
        self._usage_retries = 0
        self._older_offset: Optional[int] = None
        self.is_user_looking: Callable[[], bool] = lambda: False
        self.on_attention: Optional[Callable[[str], None]] = None
        self.on_switch_session: Optional[Callable[[str], None]] = None
        self.on_needs_admin: Optional[Callable[[], None]] = None
        self.on_assistant_output: Optional[Callable[[], None]] = None
        self.capture_screen: Optional[Callable[[Optional[int]], Any]] = None
        gateway.on_event = self.handle_event
        gateway.on_connected = self._did_connect
        GLib.timeout_add_seconds(90, self._usage_tick)

    def _usage_tick(self) -> bool:
        self.refresh_usage()
        return GLib.SOURCE_CONTINUE

    @property
    def session_key(self) -> str:
        return self.settings.resolved_session_key

    @property
    def latest_assistant_text(self) -> Optional[str]:
        return next((item.text for item in reversed(self.items) if item.role == "assistant"), None)

    def _touch_items(self) -> None:
        self.changed()

    @staticmethod
    def _quiet(body):
        """Runs a request body and swallows gateway failures, for best-effort refreshes."""

        def runner():
            try:
                return (yield from body)
            except ExpectedError as error:
                log.debug("Background request failed: %s", error)
                return None

        return spawn(runner())

    # MARK: Attention

    def clear_attention(self) -> None:
        """Called when the user opens the panel: everything on screen now counts as seen."""
        self.attention = None
        for item in self.items:
            if item.role == "assistant" and item.run_id:
                self._seen_content[item.run_id] = fingerprint(item)

    def _item_for_run(self, run_id: str) -> Optional[ChatItem]:
        return next((i for i in reversed(self.items) if i.role == "assistant" and i.run_id == run_id), None)

    def _mark_seen(self, run_id: str) -> None:
        item = self._item_for_run(run_id)
        if item:
            self._seen_content[run_id] = fingerprint(item)

    def _has_unseen(self, run_id: str) -> bool:
        item = self._item_for_run(run_id)
        return item is not None and self._seen_content.get(run_id) != fingerprint(item)

    def _remember_final(self, run_id: str) -> None:
        self._finalized_runs.add(run_id)
        if len(self._finalized_runs) > 500:
            self._finalized_runs = {run_id}

    # MARK: Connection

    def _did_connect(self) -> None:
        self._quiet(self._subscribe())
        self.load_history()
        self.refresh_session_settings()
        self.load_models()
        self.refresh_usage()
        self.load_sessions()

    def _subscribe(self):
        yield self.gateway.request("sessions.messages.subscribe", {"key": self.session_key}, 10)

    def session_changed(self, previous_key: Optional[str] = None) -> None:
        if previous_key and self.gateway.state.is_connected:
            self._quiet(self._unsubscribe(previous_key))
        self.items = []
        self.activity = IDLE
        self._active_run_id = None
        self.history_loaded = False
        self.session_settings = SessionSettings()
        self.changed()
        if self.gateway.state.is_connected:
            self._did_connect()

    def _unsubscribe(self, key: str):
        yield self.gateway.request("sessions.messages.unsubscribe", {"key": key}, 10)

    def load_history(self) -> None:
        def body():
            try:
                result = yield self.gateway.request(
                    "chat.history", {"sessionKey": self.session_key, "limit": HISTORY_PAGE_SIZE}, 25)
            except ExpectedError as error:
                log.error("chat.history failed: %s", error)
                return
            self._apply_history(result)

        spawn(body())

    def load_older_history(self) -> None:
        """Prepends older pages, skipping pages with only tool rows so scrolling up reveals conversation."""
        if not self.has_older_history or self.is_loading_older or self._older_offset is None or not self.gateway.state.is_connected:
            return
        self.is_loading_older = True

        def body():
            offset = self._older_offset
            try:
                for _ in range(5):
                    result = yield self.gateway.request(
                        "chat.history", {"sessionKey": self.session_key, "limit": HISTORY_PAGE_SIZE, "offset": offset}, 25)
                    older = history_items(result, offset)
                    known = {item.id for item in self.items}
                    fresh = [item for item in older if item.id not in known]
                    self.items = fresh + self.items
                    self._update_paging(result)
                    if self._older_offset is None or not self.has_older_history or fresh:
                        return
                    offset = self._older_offset
            except ExpectedError as error:
                log.error("Loading older history failed: %s", error)
            finally:
                self.is_loading_older = False

        spawn(body())

    def _update_paging(self, result: Any) -> None:
        next_offset = number_of(dig(result, "nextOffset"))
        self._older_offset = int(next_offset) if next_offset is not None else None
        self.has_older_history = dig(result, "hasMore") is True and next_offset is not None

    def _apply_history(self, result: Any) -> None:
        restored = history_items(result, 0)
        self._update_paging(result)
        # Keep a locally-sent message that history doesn't contain yet.
        pending = [i for i in self.items if i.is_streaming or (i.role == "user" and i.run_id and self._active_run_id == i.run_id)]
        pending = [i for i in pending if not any(r.text == i.text and r.role == i.role for r in restored)]
        self.items = restored + pending
        run = dig(result, "inFlightRun")
        run_id = text_of(dig(run, "runId"))
        if run_id:
            self._active_run_id = run_id
            text = dig(run, "text") if isinstance(dig(run, "text"), str) else ""
            self._upsert_assistant(run_id, lambda item: setattr(item, "text", text))
            self.activity = AgentActivity("thinking") if not text else AgentActivity("streaming")
        info = dig(result, "sessionInfo")
        if info:
            self.session_settings.merge(info)
        self.history_loaded = True
        self.changed()

    # MARK: Sending

    def send(self, raw: str, share_screen: bool = False) -> None:
        text = raw.strip()
        if not text:
            return
        if not self.gateway.state.is_connected:
            self.show_toast("Not connected — message not sent")
            return
        is_command = text.startswith("/")
        capture = None if is_command or not share_screen else self.capture_screen
        local_id = str(uuid.uuid4())
        idempotency_key = str(uuid.uuid4())
        item = ChatItem(id=local_id, role="user", text=text, run_id=idempotency_key, date=time.time())
        if capture is not None:
            item.screen = ScreenShare("capturing")
        self.items = self.items + [item]
        self.progress = RunProgress(started_at=time.time())
        self.activity = AgentActivity("thinking", None if capture is None else "Reading your screen")
        self.attention = None
        inject = self.settings.get("inject_markdown") and not is_command
        formatted = f"{MARKDOWN_INSTRUCTION}\n\n{text}" if inject else text

        def body():
            message = formatted
            params: dict[str, Any] = {"sessionKey": self.session_key, "idempotencyKey": idempotency_key}
            if capture is not None:
                try:
                    snapshot = yield capture(self.gateway.max_image_bytes)
                    message = ScreenContext.block(snapshot) + "\n\n" + formatted
                    params["attachments"] = [self.attachment_for(snapshot)]
                    self._set_screen(local_id, ScreenShare("attached", app=snapshot.app_description))
                except ScreenReadError as error:
                    log.error("Screen capture for message failed: %s", error)
                    self._set_screen(local_id, ScreenShare("unavailable", reason=error.short_reason))
                if self.activity == AgentActivity("thinking", "Reading your screen"):
                    self.activity = AgentActivity("thinking")
            params["message"] = message
            try:
                ack = yield self.gateway.request("chat.send", params, 45)
            except ExpectedError as error:
                self.activity = IDLE
                self._append_error(f"Couldn't send: {error}")
                return
            run_id = text_of(dig(ack, "runId")) or idempotency_key
            self._active_run_id = run_id
            target = next((i for i in self.items if i.id == local_id), None)
            if target:
                target.run_id = run_id
                self.changed()

        spawn(body())

    def _set_screen(self, item_id: str, screen: ScreenShare) -> None:
        target = next((i for i in self.items if i.id == item_id), None)
        if target:
            target.screen = screen
            self.changed()

    @staticmethod
    def attachment_for(snapshot: ScreenSnapshot) -> dict:
        return {
            "type": "image",
            "mimeType": snapshot.mime_type,
            "fileName": f"screen.{'png' if snapshot.format == 'png' else 'jpg'}",
            "content": snapshot.base64,
            "sizeBytes": len(snapshot.image_data),
            "width": snapshot.width,
            "height": snapshot.height,
        }

    def abort(self) -> None:
        def body():
            params = {"sessionKey": self.session_key}
            if self._active_run_id:
                params["runId"] = self._active_run_id
            try:
                yield self.gateway.request("chat.abort", params, 10)
            except ExpectedError as error:
                self.show_toast(f"Couldn't stop: {error}")

        spawn(body())

    # MARK: Session settings

    def refresh_session_settings(self) -> None:
        def body():
            result = yield self.gateway.request("sessions.describe", {"key": self.session_key}, 15)
            self.session_settings.merge(dig(result, "session") or dig(result, "row") or result)
            self.changed()

        self._quiet(body())

    def load_models(self) -> None:
        def body():
            result = yield self.gateway.request("models.list", {}, 25)
            models = []
            for row in as_list(dig(result, "models")):
                model_id, provider = text_of(dig(row, "id")), text_of(dig(row, "provider"))
                if model_id and provider:
                    ref = model_id if "/" in model_id else f"{provider}/{model_id}"
                    models.append(ModelOption(ref, text_of(dig(row, "name")) or model_id, provider))
            self.models = models

        self._quiet(body())

    def set_model(self, ref: Optional[str]) -> None:
        self._patch_session({"model": ref}, f"Model → {short_model_name(ref)}" if ref else "Model reset to default")

    def set_thinking(self, level: Optional[str]) -> None:
        self._patch_session({"thinkingLevel": level}, f"Effort → {level or 'default'}")

    def _patch_session(self, fields: dict, success: str) -> None:
        def body():
            try:
                result = yield self.gateway.request("sessions.patch", {**fields, "key": self.session_key}, 25)
            except ExpectedError as error:
                self.show_toast(f"Couldn't update: {error}")
                return
            self.session_settings.merge(dig(result, "entry") or dig(result, "session") or result)
            if "model" in fields:
                self.session_settings.model = fields["model"]
                if fields["model"] is not None:
                    self.session_settings.model_provider = None
            if "thinkingLevel" in fields:
                self.session_settings.thinking_level = fields["thinkingLevel"]
            self.changed()
            self.refresh_session_settings()
            self.show_toast(success)

        spawn(body())

    def compact(self) -> None:
        if self.activity != IDLE:
            self.show_toast("Wait for the current reply to finish")
            return
        if not self._require_admin("Compact"):
            return
        self.activity = AgentActivity("compacting")

        def body():
            try:
                yield self.gateway.request("sessions.compact", {"key": self.session_key}, 180)
                self.show_toast("Context compacted")
                self.refresh_session_settings()
            except ExpectedError as error:
                if is_missing_admin(error):
                    self._admin_missing("Compact")
                else:
                    self.show_toast(f"Compaction failed: {error}")
            if self.activity.kind == "compacting":
                self.activity = IDLE

        spawn(body())

    # MARK: Sessions

    @property
    def current_session_title(self) -> str:
        row = next((s for s in self.sessions if matches_session(s.key, self.session_key)), None)
        return row.title if row else session_title_for_key(self.session_key)

    def load_sessions(self) -> None:
        if not self.gateway.state.is_connected:
            return

        def body():
            # Only long-standing params, so older gateways accept the request too.
            result = yield self.gateway.request("sessions.list", {"limit": 25, "sortBy": "updatedAt"}, 20)
            rows = []
            for row in as_list(dig(result, "sessions")):
                key = text_of(dig(row, "key"))
                if key and row.get("archived") is not True and not is_background_session(row):
                    updated = number_of(row.get("updatedAt"))
                    rows.append(SessionSummary(key, session_title(row), updated / 1000 if updated else None))
            self.sessions = rows[:15]

        self._quiet(body())

    def create_session(self) -> None:
        """Starts a brand-new session (no admin needed) and switches to it."""
        if not self.gateway.state.is_connected:
            self.show_toast("Not connected")
            return

        def body():
            try:
                result = yield self.gateway.request("sessions.create", {"idempotencyKey": str(uuid.uuid4())}, 30)
            except ExpectedError as error:
                self.show_toast(f"Couldn't start a session: {error}")
                return
            key = text_of(dig(result, "key")) or text_of(dig(result, "entry", "key"))
            if not key:
                self.show_toast("The gateway didn't return a session")
                return
            if self.on_switch_session:
                self.on_switch_session(key)
            self.show_toast("Started a new session")
            self.load_sessions()

        spawn(body())

    def switch_session(self, key: str) -> None:
        if not matches_session(key, self.session_key) and self.on_switch_session:
            self.on_switch_session(key)

    def reset_session(self) -> None:
        """Clears the current session in place (requires admin)."""
        if not self._require_admin("Reset session"):
            return

        def body():
            try:
                yield self.gateway.request("sessions.reset", {"key": self.session_key}, 30)
                self.items = []
                self.show_toast("Session cleared")
                self.refresh_session_settings()
            except ExpectedError as error:
                if is_missing_admin(error):
                    self._admin_missing("Reset session")
                else:
                    self.show_toast(f"Reset failed: {error}")

        spawn(body())

    def _require_admin(self, action: str) -> bool:
        if not self.gateway.state.is_connected or self.gateway.has_admin:
            return True
        self._admin_missing(action)
        return False

    def _admin_missing(self, action: str) -> None:
        if self.gateway.admin_access[0] == "pending":
            self.show_toast(f"{action} needs admin access — approve it on your gateway")
        else:
            self.show_toast(f"{action} needs admin access — see Settings › Gateway")
        if self.on_needs_admin:
            self.on_needs_admin()

    def resolve_approval(self, approval: PendingApproval, decision: str) -> None:
        self.approvals = [a for a in self.approvals if a.id != approval.id]
        if not self.approvals and self.attention == "needs_input":
            self.attention = None

        def body():
            try:
                yield self.gateway.request("exec.approval.resolve", {"id": approval.id, "decision": decision}, 15)
            except ExpectedError as error:
                self.show_toast(f"Approval failed: {error}")

        spawn(body())

    # MARK: Usage

    def refresh_usage(self) -> None:
        if not self.gateway.state.is_connected:
            return

        def body():
            result = yield self.gateway.request("usage.status", {}, 30)
            if dig(result, "refreshing") is True and not as_list(dig(result, "providers")):
                # Cold cache: the gateway is fetching in the background. Poll briefly.
                if self._usage_retries < 6:
                    self._usage_retries += 1
                    yield sleep(2.5)
                    self.refresh_usage()
                return
            self._usage_retries = 0
            self.usage = parse_usage(result)

        self._quiet(body())

    # MARK: Events

    def handle_event(self, name: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        if name == "chat":
            self._handle_chat(payload)
        elif name == "agent":
            self._handle_agent(payload)
        elif name == "session.message":
            # A message was committed to the transcript; generated images arrive this way.
            key = payload.get("sessionKey") or payload.get("key") or dig(payload, "session", "key")
            message = payload.get("message")
            if matches_session(key, self.session_key) and dig(message, "role") == "assistant":
                self.attach_images(message)
        elif name in ("exec.approval.requested", "plugin.approval.requested"):
            self._handle_approval_requested(payload)
        elif name in ("exec.approval.resolved", "plugin.approval.resolved"):
            approval_id = payload.get("id") or payload.get("approvalId")
            if approval_id:
                self.approvals = [a for a in self.approvals if a.id != approval_id]
                if not self.approvals and self.attention == "needs_input":
                    self.attention = None
        elif name == "sessions.changed":
            row = payload.get("session")
            if isinstance(row, dict) and matches_session(row.get("key"), self.session_key):
                self.session_settings.merge(row)
                self.changed()
            if self._sessions_reload is not None:
                self._sessions_reload.cancel()

            def reload():
                yield sleep(1)
                self.load_sessions()

            self._sessions_reload = spawn(reload())

    def _handle_chat(self, p: dict) -> None:
        if not matches_session(p.get("sessionKey"), self.session_key):
            return
        run_id = text_of(p.get("runId"))
        if not run_id:
            return
        state = p.get("state")
        if state == "status":
            if self._active_run_id is None:
                self._active_run_id = run_id
            if self.progress.started_at is None:
                self.progress.started_at = time.time()
            self.progress.phase = phase_text(p.get("phase"), p.get("retry"))
            if self.activity.kind != "streaming":
                self.activity = AgentActivity("thinking", self.progress.headline)
            self.changed()
        elif state == "delta":
            self._active_run_id = run_id
            delta = p.get("deltaText") if isinstance(p.get("deltaText"), str) else ""
            message = p.get("message")
            snapshot = extract_text(message) if message is not None else None
            snapshot_images = extract_images(message) if message is not None else []
            replace = p.get("replace") is True

            def update(item: ChatItem) -> None:
                # A supplied snapshot already includes deltaText; never append it twice.
                if snapshot is not None:
                    item.text = snapshot
                elif replace:
                    item.text = delta
                else:
                    item.text += delta
                if snapshot_images:
                    item.images = unique_images(item.images + snapshot_images)

            self._upsert_assistant(run_id, update)
            if self.is_user_looking():
                self._mark_seen(run_id)
            was_streaming = self.activity.kind == "streaming"
            self.activity = AgentActivity("streaming")
            if not was_streaming and self.on_assistant_output:
                self.on_assistant_output()
        elif state == "final":
            self._handle_final(run_id, p)
        elif state == "aborted":
            self._upsert_assistant(run_id, lambda item: setattr(item, "is_streaming", False))
            self._remove_empty(run_id)
            self._finish_run(run_id)
            self.show_toast("Stopped")
        elif state == "error":
            self._upsert_assistant(run_id, lambda item: setattr(item, "is_streaming", False))
            self._remove_empty(run_id)
            self._finish_run(run_id)
            if run_id in self._finalized_runs:
                return
            self._remember_final(run_id)
            self._append_error(text_of(p.get("errorMessage")) or "The agent hit an error.")
            self._raise_attention("error", run_id)

    def _handle_final(self, run_id: str, p: dict) -> None:
        already_final = run_id in self._finalized_runs
        self._remember_final(run_id)
        had_item = self._item_for_run(run_id) is not None
        message = p.get("message")
        display = dig(message, "openclawDisplayContent")
        final_text = None
        if message is not None:
            final_text = (extract_text(display) if display is not None else None) or extract_text(message)
        final_images = (extract_images(display) if display is not None else []) + extract_images(message) if message is not None else []
        # A second commit of the latest reply under another run id (a mirrored transcript write)
        # is not a new message: don't duplicate it or glow.
        latest = next((i for i in reversed(self.items) if i.role == "assistant"), None)
        if not had_item and not final_images and final_text is not None and latest and latest.text.strip() == final_text.strip():
            log.info("Ignoring duplicate final for run %s", run_id)
            self._finish_run(run_id)
            return

        def update(item: ChatItem) -> None:
            if final_text is not None:
                item.text = final_text
            if final_images:
                item.images = unique_images(final_images)
            item.is_streaming = False

        self._upsert_assistant(run_id, update)
        self._remove_empty(run_id)
        self._finish_run(run_id)
        visible = self._item_for_run(run_id) is not None
        if visible and not already_final:
            if self.on_assistant_output:
                self.on_assistant_output()
            if self.is_user_looking():
                self._mark_seen(run_id)
            elif self._has_unseen(run_id):
                self._raise_attention("reply", run_id)
        self.reconcile_attachments()
        self.refresh_usage()
        self.refresh_session_settings()

    def _finish_run(self, run_id: str) -> None:
        if self._active_run_id is None or self._active_run_id == run_id:
            self._active_run_id = None
            self.activity = IDLE
            self.progress = RunProgress()

    def _handle_agent(self, p: dict) -> None:
        """`agent` events: tool calls, activity items, commentary, plans, and lifecycle."""
        key = p.get("sessionKey")
        if isinstance(key, str):
            if not matches_session(key, self.session_key):
                return
        elif p.get("runId") != self._active_run_id or p.get("runId") is None:
            return  # Unscoped events: only follow the run this session is showing.
        run_id = text_of(p.get("runId"))
        if not run_id:
            return
        data = p.get("data") if isinstance(p.get("data"), dict) else {}
        phase = data.get("phase")
        stream = p.get("stream")
        if stream == "lifecycle":
            if phase == "start":
                if self._active_run_id != run_id:
                    self.progress = RunProgress(started_at=time.time())
                self._active_run_id = run_id
                if self.activity == IDLE:
                    self.activity = AgentActivity("thinking")
            elif phase in ("end", "error"):
                self.progress.finish_all()
                self.changed()
            return
        if stream == "item":
            if data.get("kind") == "preamble":
                text = text_of(data.get("progressText"))
                if text:
                    self.progress.note = last_sentences(text)
            else:
                step_id = text_of(data.get("toolCallId")) or text_of(data.get("itemId"))
                if data.get("suppressChannelProgress") is True or data.get("hideFromChannelProgress") is True or not step_id:
                    return
                status = str(data.get("status") or "").lower()
                title = text_of(data.get("title"))
                if title is None and text_of(data.get("name")):
                    title = tool_title(data["name"], data.get("args"))
                self.progress.upsert(step_id, title, phase != "end", status in ("error", "failed"))
        elif stream == "tool":
            step_id, name = text_of(data.get("toolCallId")), text_of(data.get("name"))
            if not step_id or not name:
                return
            if phase == "start":
                # Activity items usually carry a nicer title; only fill in when absent.
                exists = any(step.id == step_id for step in self.progress.steps)
                self.progress.upsert(step_id, None if exists else tool_title(name, data.get("args")), True)
            elif phase in ("result", "end"):
                self.progress.upsert(step_id, None, False, data.get("isError") is True)
        elif stream == "plan":
            if phase not in ("update", None):
                return
            plan = []
            for step in as_list(data.get("steps")):
                text = text_of(dig(step, "step")) or text_of(dig(step, "text")) or text_of(dig(step, "title"))
                if text:
                    plan.append(PlanItem(text, dig(step, "status") or "pending"))
            self.progress.plan = plan
        else:
            return
        if self.progress.started_at is None:
            self.progress.started_at = time.time()
        if self.activity.kind == "thinking":
            self.activity = AgentActivity("thinking", self.progress.headline)
        self.changed()

    def _upsert_assistant(self, run_id: str, update: Callable[[ChatItem], None]) -> None:
        item = self._item_for_run(run_id)
        if item is None:
            item = ChatItem(id=f"run-{run_id}", role="assistant", text="", run_id=run_id, is_streaming=True, date=time.time())
            update(item)
            self.items = self.items + [item]
        else:
            update(item)
            self.changed()

    def _remove_empty(self, run_id: str) -> None:
        self.items = [i for i in self.items if not (i.role == "assistant" and i.run_id == run_id and not i.text.strip() and not i.images)]

    # MARK: Images

    def load_image(self, attachment: ImageAttachment) -> None:
        """Starts loading an attachment's pixels if needed; views observe `images` through `changed`."""
        if attachment.id in self.images or attachment.id in self._image_loads:
            return
        if attachment.inline_base64:
            try:
                pixbuf = decode_image(base64.b64decode(attachment.inline_base64))
            except ValueError:
                pixbuf = None
            if pixbuf is not None:
                self.images[attachment.id] = pixbuf
                self.changed()
                return
        if not attachment.artifact_id or not self.gateway.state.is_connected:
            return
        self._image_loads.add(attachment.id)

        def body():
            try:
                # Managed images return a short-lived ticket URL; if it expires mid-fetch, ask once more.
                for attempt in range(2):
                    try:
                        result = yield self.gateway.request(
                            "artifacts.download", {"sessionKey": self.session_key, "artifactId": attachment.artifact_id}, 30)
                        inline = dig(result, "data")
                        if isinstance(inline, str):
                            pixbuf = decode_image(base64.b64decode(inline))
                            if pixbuf is not None:
                                self.images[attachment.id] = pixbuf
                                self.changed()
                                return
                        path = text_of(dig(result, "url"))
                        url = self.gateway.http_url(path) if path else None
                        if not url:
                            log.error("artifacts.download returned no bytes or URL for %s", attachment.artifact_id)
                            return
                        data = yield in_thread(fetch_image_bytes, url)
                        pixbuf = decode_image(data)
                        if pixbuf is not None:
                            self.images[attachment.id] = pixbuf
                            self.changed()
                            return
                    except (ExpectedError, OSError, ValueError) as error:
                        log.error("Image load failed (attempt %d): %s", attempt + 1, error)
            finally:
                self._image_loads.discard(attachment.id)

        spawn(body())

    def reconcile_attachments(self) -> None:
        """The image can be committed to the transcript after the run ends; check a few times."""
        if self._reconcile is not None:
            self._reconcile.cancel()

        def body():
            for delay in (0.6, 2.0, 4.0, 8.0):
                yield sleep(delay)
                try:
                    result = yield self.gateway.request("chat.history", {"sessionKey": self.session_key, "limit": 8}, 20)
                except ExpectedError:
                    continue
                attached = False
                for message in as_list(dig(result, "messages")):
                    if dig(message, "role") == "assistant" and self.attach_images(message):
                        attached = True
                latest = next((i for i in reversed(self.items) if i.role == "assistant"), None)
                if attached or (latest is not None and latest.images):
                    return

        self._reconcile = spawn(body())

    def attach_images(self, message: Any) -> bool:
        """Attaches a committed assistant message's images to its reply. True if it added any."""
        found = extract_images(message)
        if not found:
            return False
        shown = {image.id for item in self.items for image in item.images}
        if all(image.id in shown for image in found):
            return False
        run_id = dig(message, "__openclaw", "runId")
        text = (extract_text(message) or "").strip()
        assistants = [i for i in self.items if i.role == "assistant"]
        # 1) same run, 2) the live reply that contains this caption, 3) the latest reply.
        target = next((i for i in reversed(assistants) if run_id and i.run_id == run_id), None)
        if target is None and text:
            target = next((i for i in reversed(assistants) if text in i.text.strip()), None)
        if target is None and self.items and self.items[-1].role == "assistant":
            target = self.items[-1]
        if target is not None:
            target.images = unique_images(target.images + found)
            self.changed()
        else:
            item_id = dig(message, "__openclaw", "id") or str(uuid.uuid4())
            self.items = self.items + [ChatItem(id=item_id, role="assistant", text=text, run_id=run_id, images=found, date=time.time())]
        log.info("Attached %d image(s) from the transcript", len(found))
        return True

    def _handle_approval_requested(self, p: dict) -> None:
        approval_id = text_of(p.get("id")) or text_of(p.get("approvalId"))
        if not approval_id:
            return
        request = p.get("request") if isinstance(p.get("request"), dict) else p
        argv = request.get("argv")
        command = text_of(request.get("command")) or text_of(request.get("commandText"))
        if command is None and isinstance(argv, list):
            command = " ".join(a for a in argv if isinstance(a, str)) or None
        title = command or text_of(p.get("title")) or text_of(request.get("title")) or "The agent needs your approval"
        cwd = text_of(request.get("cwd"))
        detail = f"in {cwd}" if cwd else text_of(request.get("description")) or text_of(p.get("description"))
        allowed = p.get("allowedDecisions") if isinstance(p.get("allowedDecisions"), list) else request.get("allowedDecisions")
        decisions = [d for d in as_list(allowed) if isinstance(d, str)] or ["allow-once", "allow-always", "deny"]
        expires = number_of(p.get("expiresAtMs")) or number_of(request.get("expiresAtMs"))
        approval = PendingApproval(approval_id, title, detail, decisions, expires / 1000 if expires else None)
        self.approvals = [a for a in self.approvals if a.id != approval_id] + [approval]
        self._raise_attention("needs_input")

    def _raise_attention(self, reason: str, run_id: Optional[str] = None) -> None:
        if not self.settings.get("glow_on_reply") and reason != "needs_input":
            return
        if self.is_user_looking() and reason != "needs_input":
            return
        log.info("Attention raised: %s run=%s", reason, run_id or "-")
        self.attention = reason
        if self.on_attention:
            self.on_attention(reason)

    def _append_error(self, message: str) -> None:
        self.items = self.items + [ChatItem(id=str(uuid.uuid4()), role="error", text=message, date=time.time())]

    def show_toast(self, message: str) -> None:
        self.toast = message
        if self._toast_task is not None:
            self._toast_task.cancel()

        def body():
            yield sleep(2.6)
            self.toast = None

        self._toast_task = spawn(body())
