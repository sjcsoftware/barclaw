"""WebSocket client for an OpenClaw gateway (protocol v4), as an operator or a node.

Handles the challenge/connect handshake with a signed device identity, request/response
correlation, event fan-out, a tick watchdog, and fast reconnects driven by network and
resume-from-suspend notifications.
"""
from __future__ import annotations

import json
import locale
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from gi.repository import Gio, GLib, Soup

from ..support import (
    ExpectedError, Future, Observable, Task, app_version, config_dir, dig, log, number_of, observed,
    read_json, sleep, spawn, text_of, write_private,
)
from .identity import SecureStore, auth_payload_v2


class GatewayError(ExpectedError):
    def __init__(self, code: str, message: str, details: Any = None, retry_after_ms: Optional[float] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details if isinstance(details, dict) else None
        self.retry_after_ms = retry_after_ms

    @classmethod
    def from_frame(cls, frame: Any) -> "GatewayError":
        frame = frame if isinstance(frame, dict) else {}
        return cls(
            frame.get("code") if isinstance(frame.get("code"), str) else "UNKNOWN",
            text_of(frame.get("message")) or "Gateway request failed",
            frame.get("details"),
            number_of(frame.get("retryAfterMs")),
        )

    @property
    def detail_code(self) -> Optional[str]:
        value = (self.details or {}).get("code")
        return value if isinstance(value, str) else None


def not_connected() -> GatewayError:
    return GatewayError("NOT_CONNECTED", "Not connected to the gateway")


@dataclass(frozen=True)
class ConnectionState:
    # idle | connecting | connected | offline | pairing | auth_failed
    kind: str
    attempt: int = 0
    message: str = ""
    request_id: Optional[str] = None

    @property
    def is_connected(self) -> bool:
        return self.kind == "connected"


IDLE = ConnectionState("idle")


@dataclass
class ConnectionProfile:
    """Who a connection presents itself as. BarClaw runs an operator and, with visual intelligence on, a node."""

    role: str
    client_mode: str
    scopes: list[str]
    caps: list[str]
    display_name: str
    storage_key: str  # Namespaces the persisted device token, so each role keeps its own.
    commands: Optional[list[str]] = None
    permissions: Optional[dict[str, bool]] = None


def operator_profile() -> ConnectionProfile:
    return ConnectionProfile(
        role="operator", client_mode="ui",
        scopes=["operator.read", "operator.write", "operator.approvals"],
        # `tool-events` opts in to live tool progress; `chat-only-assistant-text` skips the duplicate
        # assistant-text agent stream (reply text is rendered from `chat` only).
        caps=["usage-refreshing", "exec-approvals", "tool-events", "chat-only-assistant-text"],
        display_name="BarClaw", storage_key="",
    )


def normalized_url(raw: str) -> Optional[str]:
    """Normalizes `host:18789`, `http://…`, or `wss://…` to a WebSocket URL."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("http://"):
        text = "ws://" + text[len("http://"):]
    elif text.startswith("https://"):
        text = "wss://" + text[len("https://"):]
    elif not text.startswith(("ws://", "wss://")):
        text = "ws://" + text
    try:
        parts = urlsplit(text)
        host = parts.hostname
        _ = parts.port  # Raises on an out-of-range port.
    except ValueError:
        return None
    return text if host else None


def http_url(gateway: str, path: str) -> Optional[str]:
    """Resolves a gateway-relative path (e.g. a media ticket URL) against its HTTP(S) origin."""
    if path.startswith(("http://", "https://")):
        return path
    parts = urlsplit(gateway)
    origin = urlunsplit(("https" if parts.scheme == "wss" else "http", parts.netloc, "/", "", ""))
    return urljoin(origin, path)


def describe_auth_failure(detail: str, message: str) -> str:
    return {
        "AUTH_TOKEN_MISMATCH": "Gateway token doesn't match.",
        "AUTH_TOKEN_MISSING": "Gateway requires a token.",
        "AUTH_REQUIRED": "Gateway requires a token.",
        "AUTH_SCOPE_MISMATCH": "Device not approved for these permissions. Re-pair on the gateway.",
    }.get(detail, message)


def friendly(message: str) -> str:
    lower = message.lower()
    if "could not connect" in lower or "connection refused" in lower or "cannot connect" in lower:
        return "Gateway unreachable"
    if "timed out" in lower or "timeout" in lower:
        return "Gateway timed out"
    if "offline" in lower or "network connection was lost" in lower or "closed" in lower:
        return "Connection lost"
    if "resolve" in lower or "hostname" in lower or "name or service" in lower:
        return "Gateway host not found"
    return message


def instance_id() -> str:
    """Stable per-install identifier reported to the gateway for presence display."""
    path = config_dir() / "instance.json"
    stored = read_json(path).get("instanceId")
    if isinstance(stored, str) and stored:
        return stored
    fresh = str(uuid.uuid4())
    try:
        write_private(path, json.dumps({"instanceId": fresh}).encode())
    except OSError as error:
        log.error("Couldn't save instance id: %s", error)
    return fresh


def preferred_locale() -> str:
    language = locale.getlocale()[0] or "en_US"
    return language.replace("_", "-")


class PairedFlags:
    """Whether a gateway has accepted this device before, per gateway URL and role."""

    path_name = "paired.json"

    @classmethod
    def get(cls, key: str) -> bool:
        return bool(read_json(config_dir() / cls.path_name).get(key))

    @classmethod
    def set(cls, key: str) -> None:
        path = config_dir() / cls.path_name
        data = read_json(path)
        if not data.get(key):
            data[key] = True
            try:
                write_private(path, json.dumps(data).encode())
            except OSError as error:
                log.error("Couldn't save pairing flag: %s", error)


class WebSocket:
    """A libsoup WebSocket reduced to what the client needs. Tests can swap in a fake."""

    def __init__(self, url: str, on_text: Callable[[str], None], on_closed: Callable[[str], None]) -> None:
        self.url = url
        self._on_text = on_text
        self._on_closed = on_closed
        self._session = Soup.Session()
        self._session.set_timeout(0)
        self._session.set_idle_timeout(0)
        self._cancellable = Gio.Cancellable()
        self._conn: Any = None
        self._closed = False
        self.on_activity: Callable[[], None] = lambda: None

    def open(self) -> Future:
        future = Future()
        parts = urlsplit(self.url)
        scheme = "https" if parts.scheme == "wss" else "http"
        # libsoup keeps the handshake on the HTTP(S) scheme.
        http = urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))
        message = Soup.Message.new("GET", http)
        if message is None:
            future.set_error(GatewayError("BAD_URL", "Invalid gateway URL"))
            return future

        def done(session: Soup.Session, result: Gio.AsyncResult) -> None:
            try:
                conn = session.websocket_connect_finish(result)
            except GLib.Error as error:
                future.set_error(GatewayError("CONNECT_FAILED", error.message))
                return
            conn.set_max_incoming_payload_size(64 * 1024 * 1024)
            conn.set_keepalive_interval(10)
            conn.connect("message", self._message)
            conn.connect("closed", self._closed_signal)
            conn.connect("pong", lambda *_: self.on_activity())
            self._conn = conn
            future.set_result(self)

        self._session.websocket_connect_async(message, None, None, GLib.PRIORITY_DEFAULT, self._cancellable, done)
        return future

    def _message(self, _conn: Any, kind: int, data: GLib.Bytes) -> None:
        raw = data.get_data() or b""
        self._on_text(raw.decode("utf-8", "replace"))

    def _closed_signal(self, conn: Any) -> None:
        if not self._closed:
            self._closed = True
            self._on_closed(f"closed ({conn.get_close_code()})")

    def send(self, text: str) -> None:
        if self._conn is None or self._conn.get_state() != Soup.WebsocketState.OPEN:
            raise GatewayError("DISCONNECTED", "Connection closed")
        self._conn.send_text(text)

    def close(self) -> None:
        self._closed = True
        self._cancellable.cancel()
        if self._conn is not None and self._conn.get_state() == Soup.WebsocketState.OPEN:
            self._conn.close(Soup.WebsocketCloseCode.GOING_AWAY, None)
        self._conn = None


class GatewayClient(Observable):
    PROTOCOL_VERSION = 4
    CLIENT_ID = "openclaw-linux"
    ADMIN_SCOPE = "operator.admin"

    state: ConnectionState = observed(IDLE)
    server_version: Optional[str] = observed(None)
    granted_scopes: list = observed([])
    network_available: bool = observed(True)
    last_error: Optional[str] = observed(None)
    # off | pending | granted, with the pending request id when known.
    admin_access: tuple = observed(("off", None))
    max_image_bytes: Optional[int] = observed(None)

    socket_factory: Callable[..., WebSocket] = WebSocket

    def __init__(self, profile: Optional[ConnectionProfile] = None, store: Optional[SecureStore] = None) -> None:
        super().__init__()
        self.profile = profile or operator_profile()
        self.store = store or SecureStore.shared()
        self.on_event: Optional[Callable[[str, Any], None]] = None
        self.on_connected: Optional[Callable[[], None]] = None
        self.url: Optional[str] = None
        self.token = ""
        self._wants_connection = False
        self._request_admin = False
        self._attempt_included_admin = False
        self._socket: Optional[WebSocket] = None
        self._generation = 0
        self._attempt = 0
        self._pending: dict[str, Future] = {}
        self._challenge_waiter: Optional[Future] = None
        self._buffered_challenge: Optional[tuple[str, int]] = None
        self._reconnect_task: Optional[Task] = None
        self._admin_probe: Optional[Task] = None
        self._connect_task: Optional[Task] = None
        self._last_frame_at = time.monotonic()
        self._tick_interval = 30.0
        self._watchdog = 0
        self._used_stored_token = False
        self._watch_system()

    # MARK: Public API

    def configure(self, url_string: str, token: str) -> None:
        """Applies new connection settings and (re)connects when they are usable."""
        new_url = normalized_url(url_string)
        changed = new_url != self.url or token != self.token
        self.url, self.token = new_url, token
        if new_url is None:
            self.stop()
            return
        if changed or not self.state.is_connected:
            self._wants_connection = True
            self._attempt = 0
            self._connect_now()

    def set_request_admin(self, enabled: bool) -> None:
        """Opts in or out of requesting `operator.admin`. Reconnects so the new scope set applies."""
        if enabled == self._request_admin:
            return
        self._request_admin = enabled
        self._cancel(self._admin_probe)
        self._admin_probe = None
        self.admin_access = ("off", None)
        self.reconnect_now("admin requested" if enabled else "admin dropped")

    @property
    def has_admin(self) -> bool:
        return self.ADMIN_SCOPE in self.granted_scopes

    @property
    def device_id(self) -> str:
        return self.store.identity.device_id

    @property
    def gateway_key(self) -> str:
        return self.url or ""

    @property
    def token_key(self) -> str:
        return self.profile.storage_key + self.gateway_key

    @property
    def host(self) -> Optional[str]:
        return urlsplit(self.url).hostname if self.url else None

    def update_profile(self, change: Callable[[ConnectionProfile], None]) -> None:
        """Changes what this connection advertises; reconnects if it changed."""
        before = (list(self.profile.caps), self.profile.commands, self.profile.permissions, self.profile.display_name)
        change(self.profile)
        after = (self.profile.caps, self.profile.commands, self.profile.permissions, self.profile.display_name)
        if before != after:
            self.reconnect_now("profile changed")

    def stop(self) -> None:
        self._wants_connection = False
        self._cancel(self._reconnect_task)
        self._teardown(not_connected())
        self.state = IDLE

    def reconnect_now(self, reason: str) -> None:
        if not self._wants_connection or self.url is None:
            return
        log.info("Reconnecting now (%s)", reason)
        self._attempt = 0
        self._connect_now()

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 30) -> Future:
        if not self.state.is_connected or self._socket is None:
            future = Future()
            future.set_error(not_connected())
            return future
        return self._send(method, params if params is not None else {}, self._socket, timeout)

    def http_url(self, path: str) -> Optional[str]:
        return http_url(self.url, path) if self.url else None

    # MARK: Connection lifecycle

    @staticmethod
    def _cancel(task: Optional[Task]) -> None:
        if task is not None:
            task.cancel()

    def _connect_now(self) -> None:
        self._cancel(self._reconnect_task)
        self._teardown(GatewayError("RECONNECTING", "Reconnecting"))
        self._generation += 1
        self._connect_task = spawn(self._perform_connect(self._generation))

    def _perform_connect(self, gen: int):
        if self.url is None or gen != self._generation:
            return
        self.state = ConnectionState("connecting", attempt=self._attempt)
        socket = self.socket_factory(
            self.url,
            lambda text: self._frame_arrived(text, gen),
            lambda reason: self._socket_closed(reason, gen),
        )
        socket.on_activity = self._touch
        self._socket = socket
        self._buffered_challenge = None
        self._touch()
        try:
            yield socket.open()
            if gen != self._generation:
                socket.close()
                return
            nonce, ts = yield self._wait_for_challenge(12)
            if gen != self._generation:
                return
            scopes = self._current_scopes()
            self._attempt_included_admin = self.ADMIN_SCOPE in scopes
            params = self._connect_params(nonce, ts, scopes)
            hello = yield self._send("connect", params, socket, 20)
            if gen != self._generation:
                return
            self._apply_hello(hello)
            self._attempt = 0
            self.last_error = None
            self.state = ConnectionState("connected")
            PairedFlags.set("paired:" + self.token_key)
            self._start_watchdog()
            log.info("Connected to gateway %s as %s", self.server_version or "?", self.profile.role)
            if self.on_connected:
                self.on_connected()
        except GatewayError as error:
            if gen == self._generation:
                self._handle_connect_failure(error)

    def _current_scopes(self) -> list[str]:
        """Base scopes plus admin when requested, unless an admin upgrade is still awaiting approval."""
        if not self._request_admin or self.admin_access[0] == "pending":
            return list(self.profile.scopes)
        return list(self.profile.scopes) + [self.ADMIN_SCOPE]

    def _connect_params(self, nonce: str, signed_at: int, scopes: list[str], record_auth: bool = True) -> dict:
        identity = self.store.identity
        stored = self.store.device_token(self.token_key)
        auth_token = self.token or (stored or {}).get("token") or None
        if record_auth:
            self._used_stored_token = not self.token and stored is not None

        params: dict[str, Any] = {
            "minProtocol": self.PROTOCOL_VERSION,
            "maxProtocol": self.PROTOCOL_VERSION,
            "client": {
                "id": self.CLIENT_ID,
                "displayName": self.profile.display_name,
                "version": app_version(),
                # Both are read by OpenClaw's node command policy; "Linux" selects its desktop defaults.
                "platform": "linux",
                "deviceFamily": "Linux",
                "mode": self.profile.client_mode,
                "instanceId": instance_id(),
            },
            "role": self.profile.role,
            "scopes": scopes,
            "caps": self.profile.caps,
            "locale": preferred_locale(),
            "userAgent": f"BarClaw/{app_version()}",
        }
        if self.profile.commands is not None:
            params["commands"] = self.profile.commands
        if self.profile.permissions is not None:
            params["permissions"] = self.profile.permissions
        if auth_token:
            params["auth"] = {"token": auth_token}
        payload = auth_payload_v2(
            identity.device_id, self.CLIENT_ID, self.profile.client_mode, self.profile.role,
            scopes, signed_at, auth_token, nonce,
        )
        params["device"] = {
            "id": identity.device_id,
            "publicKey": identity.public_key_b64url,
            "signature": identity.sign(payload),
            "signedAt": signed_at,
            "nonce": nonce,
        }
        return params

    def _apply_hello(self, hello: Any) -> None:
        self.server_version = dig(hello, "server", "version")
        scopes = dig(hello, "auth", "scopes")
        self.granted_scopes = [s for s in scopes if isinstance(s, str)] if isinstance(scopes, list) else []
        tick = number_of(dig(hello, "policy", "tickIntervalMs"))
        if tick and tick > 0:
            self._tick_interval = tick / 1000
        max_image = number_of(dig(hello, "policy", "attachments", "maxImageBytes"))
        self.max_image_bytes = int(max_image) if max_image else None
        if self.ADMIN_SCOPE in self.granted_scopes:
            self.admin_access = ("granted", None)
            self._cancel(self._admin_probe)
            self._admin_probe = None
        elif not self._request_admin:
            self.admin_access = ("off", None)
        elif self._attempt_included_admin:
            # Asked for admin but the gateway connected us without it: treat as awaiting approval.
            self.admin_access = ("pending", None)
            self._start_admin_probe()
        device_token = text_of(dig(hello, "auth", "deviceToken"))
        if device_token:
            self.store.set_device_token(self.token_key, {
                "token": device_token, "scopes": self.granted_scopes,
                "deviceId": self.store.identity.device_id, "updatedAtMs": int(time.time() * 1000),
            })

    def _handle_connect_failure(self, error: GatewayError) -> None:
        detail = error.detail_code or ""
        message = error.message
        self.last_error = message
        self._teardown(error)
        log.error("Connect failed: %s %s %s", error.code, detail, message)

        request_id = (error.details or {}).get("requestId")
        request_id = request_id if isinstance(request_id, str) else None
        needs_pairing = detail == "PAIRING_REQUIRED" or error.code == "NOT_PAIRED"
        if needs_pairing and self._attempt_included_admin and PairedFlags.get("paired:" + self.token_key):
            # An admin *upgrade* is pending. Keep working with the existing approval meanwhile.
            self.admin_access = ("pending", request_id)
            self._start_admin_probe()
            self._schedule_reconnect(0.1)
            return
        if needs_pairing:
            self.state = ConnectionState("pairing", request_id=request_id)
            self._schedule_reconnect(3)
            return
        if detail.startswith("AUTH_") or error.code == "UNAUTHORIZED" or detail.startswith("DEVICE_AUTH"):
            if self._used_stored_token:
                # Stale device token: forget it and retry once with whatever is configured.
                self.store.set_device_token(self.token_key, None)
                self._used_stored_token = False
                self._schedule_reconnect(0.25)
                return
            self.state = ConnectionState("auth_failed", message=describe_auth_failure(detail, message))
            return
        if detail == "OPERATOR_ACCESS_DENIED":
            self.state = ConnectionState("auth_failed", message="Gateway denied operator access. Ask the gateway admin to grant access.")
            self._schedule_reconnect(30)
            return
        self.state = ConnectionState("offline", message=friendly(message) if self.network_available else "No network")
        if error.retry_after_ms:
            self._schedule_reconnect(min(error.retry_after_ms / 1000, 30))
        else:
            self._schedule_reconnect()

    def _handle_disconnect(self, reason: str) -> None:
        if not self._wants_connection:
            return
        log.info("Disconnected: %s", reason)
        self._teardown(GatewayError("DISCONNECTED", reason))
        if self.state.kind == "auth_failed":
            return
        if self.state.kind != "pairing":
            self.state = ConnectionState("offline", message=friendly(reason) if self.network_available else "No network")
        self._schedule_reconnect()

    def _schedule_reconnect(self, explicit_delay: Optional[float] = None) -> None:
        if not self._wants_connection:
            return
        self._cancel(self._reconnect_task)
        # When the network is down the monitor triggers the reconnect (a loopback gateway doesn't need one).
        if not self.network_available and self.host not in ("127.0.0.1", "localhost", "::1"):
            return
        delay = explicit_delay if explicit_delay is not None else min(10, 0.25 * 2 ** self._attempt) + random.uniform(0, 0.2)
        self._attempt += 1

        def wait():
            yield sleep(delay)
            self._reconnect_task = None
            self._connect_now()

        self._reconnect_task = spawn(wait())

    def _teardown(self, error: Exception) -> None:
        self._generation += 1
        if self._watchdog:
            GLib.source_remove(self._watchdog)
            self._watchdog = 0
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        waiter, self._challenge_waiter = self._challenge_waiter, None
        if waiter is not None:
            waiter.set_error(error)
        pending, self._pending = self._pending, {}
        for future in pending.values():
            future.set_error(error)

    # MARK: Frames

    def _touch(self) -> None:
        self._last_frame_at = time.monotonic()

    def _socket_closed(self, reason: str, gen: int) -> None:
        if gen != self._generation:
            return
        if self.state.is_connected:
            self._handle_disconnect(reason)
        else:
            self._handle_connect_failure(GatewayError("DISCONNECTED", reason))

    def _frame_arrived(self, text: str, gen: int) -> None:
        if gen != self._generation:
            return
        self._touch()
        try:
            frame = json.loads(text)
        except ValueError:
            return
        if not isinstance(frame, dict):
            return
        kind = frame.get("type")
        if kind == "res":
            future = self._pending.pop(frame.get("id"), None)
            if future is None:
                return
            if frame.get("ok") is True:
                future.set_result(frame.get("payload"))
            else:
                future.set_error(GatewayError.from_frame(frame.get("error")))
        elif kind == "event":
            name = frame.get("event")
            if not isinstance(name, str):
                return
            payload = frame.get("payload")
            if name == "connect.challenge":
                nonce = text_of(dig(payload, "nonce"))
                ts = number_of(dig(payload, "ts"))
                if nonce is None or ts is None or ts < 0:
                    return
                waiter, self._challenge_waiter = self._challenge_waiter, None
                if waiter is not None:
                    waiter.set_result((nonce, int(ts)))
                else:
                    self._buffered_challenge = (nonce, int(ts))
                return
            if name == "tick":
                return
            if self.on_event:
                self.on_event(name, payload)

    def _wait_for_challenge(self, timeout: float) -> Future:
        future = Future()
        if self._buffered_challenge is not None:
            future.set_result(self._buffered_challenge)
            self._buffered_challenge = None
            return future
        self._challenge_waiter = future

        def expire() -> bool:
            if self._challenge_waiter is future:
                self._challenge_waiter = None
                future.set_error(GatewayError("TIMEOUT", "Gateway handshake timed out"))
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(int(timeout * 1000), expire)
        return future

    def _send(self, method: str, params: dict, socket: WebSocket, timeout: float) -> Future:
        future = Future()
        request_id = str(uuid.uuid4())
        frame = {"type": "req", "id": request_id, "method": method, "params": params}
        self._pending[request_id] = future
        try:
            socket.send(json.dumps(frame))
        except (GatewayError, GLib.Error) as error:
            self._pending.pop(request_id, None)
            future.set_error(error if isinstance(error, GatewayError) else GatewayError("SEND_FAILED", str(error)))
            return future

        def expire() -> bool:
            if self._pending.pop(request_id, None) is future:
                future.set_error(GatewayError("TIMEOUT", f"{method} timed out"))
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(int(timeout * 1000), expire)
        return future

    # MARK: Admin upgrade

    def _start_admin_probe(self) -> None:
        """While an admin upgrade awaits approval, retry on a separate short-lived connection."""
        if self._admin_probe is not None and not self._admin_probe.done:
            return

        def probe_loop():
            while True:
                yield sleep(6)
                if not self._request_admin or self.admin_access[0] != "pending":
                    return
                outcome, request_id = yield spawn(self._probe_admin())
                if outcome == "granted":
                    log.info("Admin access approved")
                    self.admin_access = ("granted", None)
                    self._admin_probe = None
                    self.reconnect_now("admin approved")
                    return
                if outcome == "pending" and request_id:
                    self.admin_access = ("pending", request_id)

        self._admin_probe = spawn(probe_loop())

    def _probe_admin(self):
        if self.url is None:
            return ("failed", None)
        frames: list[dict] = []
        waiter: list[Future] = []

        def on_text(text: str) -> None:
            try:
                frame = json.loads(text)
            except ValueError:
                return
            frames.append(frame)
            if waiter:
                waiter.pop().set_result(None)

        def next_frame(timeout: float):
            if not frames:
                future = Future()
                waiter.append(future)

                def expire() -> bool:
                    future.set_error(GatewayError("TIMEOUT", "Admin check timed out"))
                    return GLib.SOURCE_REMOVE

                source = GLib.timeout_add(int(timeout * 1000), expire)
                try:
                    yield future
                finally:
                    GLib.source_remove(source) if not future.error else None
            return frames.pop(0)

        socket = self.socket_factory(self.url, on_text, lambda _reason: None)
        try:
            yield socket.open()
            while True:
                frame = yield spawn(next_frame(10))
                if frame.get("event") == "connect.challenge":
                    nonce, ts = text_of(dig(frame, "payload", "nonce")), number_of(dig(frame, "payload", "ts"))
                    if nonce is not None and ts is not None:
                        break
            request_id = str(uuid.uuid4())
            params = self._connect_params(nonce, int(ts), self.profile.scopes + [self.ADMIN_SCOPE], record_auth=False)
            socket.send(json.dumps({"type": "req", "id": request_id, "method": "connect", "params": params}))
            while True:
                frame = yield spawn(next_frame(15))
                if frame.get("type") == "res" and frame.get("id") == request_id:
                    break
            if frame.get("ok") is True:
                scopes = dig(frame, "payload", "auth", "scopes")
                return ("granted", None) if isinstance(scopes, list) and self.ADMIN_SCOPE in scopes else ("pending", None)
            error = GatewayError.from_frame(frame.get("error"))
            if error.detail_code == "PAIRING_REQUIRED" or error.code == "NOT_PAIRED":
                return ("pending", (error.details or {}).get("requestId"))
            return ("failed", None)
        except GatewayError:
            return ("failed", None)
        finally:
            socket.close()

    # MARK: Liveness

    def _start_watchdog(self) -> None:
        if self._watchdog:
            GLib.source_remove(self._watchdog)
        self._watchdog = GLib.timeout_add_seconds(3, self._check_liveness)

    def _check_liveness(self) -> bool:
        if not self.state.is_connected:
            self._watchdog = 0
            return GLib.SOURCE_REMOVE
        if time.monotonic() - self._last_frame_at > max(self._tick_interval * 2, 20):
            self._watchdog = 0
            self._handle_disconnect("Gateway went silent")
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _watch_system(self) -> None:
        monitor = Gio.NetworkMonitor.get_default()
        self.network_available = monitor.get_network_available()
        self._network_handler = monitor.connect("network-changed", lambda _m, available: self._network_changed(available))
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            bus.signal_subscribe(
                "org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
                "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE, self._sleep_signal,
            )
            self._system_bus = bus
        except GLib.Error:
            self._system_bus = None

    def _sleep_signal(self, _bus: Any, _sender: Any, _path: Any, _iface: Any, _signal: Any, params: GLib.Variant) -> None:
        if not params.unpack()[0]:  # False means the machine has resumed.
            self.reconnect_now("wake")

    def _network_changed(self, available: bool) -> None:
        was = self.network_available
        self.network_available = available
        if not self._wants_connection:
            return
        if not available:
            if self.state.kind != "idle":
                self._teardown(GatewayError("OFFLINE", "No network"))
                if self.state.kind != "auth_failed":
                    self.state = ConnectionState("offline", message="No network")
            self._cancel(self._reconnect_task)
        elif not was:
            # Network is back: reconnect immediately instead of waiting out backoff.
            if self.state.kind != "auth_failed":
                self.reconnect_now("network restored")
        elif self.state.is_connected and time.monotonic() - self._last_frame_at > 5:
            # The interface changed (e.g. Wi-Fi switch) and the socket has gone quiet.
            self.reconnect_now("network changed")

    def simulate_connected(self, version: str) -> None:
        """Screenshot rendering only: pretend to be connected."""
        self.server_version = version
        self.state = ConnectionState("connected")
