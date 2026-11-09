"""The connection state machine and event handling, against a scripted in-memory socket."""
import base64
import hashlib
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from gi.repository import GLib

import barclaw
from barclaw.agent.session import AgentSession
from barclaw.agent.models import IDLE
from barclaw.gateway import client as C
from barclaw.gateway.identity import DeviceIdentity, SecureStore, auth_payload_v2
from barclaw.settings.store import AppSettings
from barclaw.support import Future, in_thread, run_until, sleep, spawn


class FakeSocket:
    """Stands in for libsoup: records frames and lets a test play the gateway."""

    instances: list = []
    respond = None  # (socket, frame) -> None

    def __init__(self, url, on_text, on_closed):
        self.url, self.on_text, self.on_closed = url, on_text, on_closed
        self.sent = []
        self.closed = False
        self.on_activity = lambda: None
        FakeSocket.instances.append(self)

    def open(self):
        future = Future()
        future.set_result(self)
        GLib.idle_add(self._greet)
        return future

    def _greet(self):
        self.event("connect.challenge", {"nonce": "n-1", "ts": 1234})
        return False

    def event(self, name, payload):
        if not self.closed:
            self.on_text(json.dumps({"type": "event", "event": name, "payload": payload}))

    def reply(self, frame, payload=None, error=None):
        self.on_text(json.dumps({"type": "res", "id": frame["id"], "ok": error is None, "payload": payload, "error": error}))

    def send(self, text):
        frame = json.loads(text)
        self.sent.append(frame)
        if FakeSocket.respond:
            FakeSocket.respond(self, frame)

    def close(self):
        self.closed = True


HELLO = {"server": {"version": "test"}, "auth": {"scopes": ["operator.read"], "deviceToken": "dt"}, "policy": {"tickIntervalMs": 15000, "attachments": {"maxImageBytes": 1000}}}


@pytest.fixture
def gateway(monkeypatch):
    FakeSocket.instances = []
    FakeSocket.respond = None
    monkeypatch.setattr(C.GatewayClient, "socket_factory", FakeSocket)
    client = C.GatewayClient(store=SecureStore())
    yield client
    client.stop()


def auto_hello(socket, frame):
    if frame["method"] == "connect":
        socket.reply(frame, HELLO)


def wait(predicate):
    assert run_until(predicate, 3)


# MARK: Handshake


def test_connect_signs_the_challenge(gateway):
    FakeSocket.respond = auto_hello
    gateway.configure("127.0.0.1:18789", "secret")
    wait(lambda: gateway.state.is_connected)
    assert FakeSocket.instances[0].url == "ws://127.0.0.1:18789"
    params = FakeSocket.instances[0].sent[0]["params"]
    assert params["client"]["id"] == "openclaw-linux" and params["client"]["platform"] == "linux" and params["client"]["deviceFamily"] == "Linux"
    assert params["auth"] == {"token": "secret"} and params["role"] == "operator"
    assert params["minProtocol"] == params["maxProtocol"] == 4
    device = params["device"]
    public = base64.urlsafe_b64decode(device["publicKey"] + "==")
    assert hashlib.sha256(public).hexdigest() == device["id"]
    payload = auth_payload_v2(device["id"], "openclaw-linux", "ui", "operator", params["scopes"], 1234, "secret", "n-1")
    Ed25519PublicKey.from_public_bytes(public).verify(base64.urlsafe_b64decode(device["signature"] + "=="), payload.encode())
    assert gateway.server_version == "test" and gateway.max_image_bytes == 1000


def test_device_token_is_stored_and_reused_without_a_configured_token(gateway):
    FakeSocket.respond = auto_hello
    gateway.configure("ws://h:1", "")
    wait(lambda: gateway.state.is_connected)
    assert "auth" not in FakeSocket.instances[0].sent[0]["params"]
    assert gateway.store.device_token("ws://h:1")["token"] == "dt"
    gateway.reconnect_now("test")
    wait(lambda: len(FakeSocket.instances) == 2 and FakeSocket.instances[1].sent)
    assert FakeSocket.instances[1].sent[0]["params"]["auth"] == {"token": "dt"}


def test_identity_persists_with_private_permissions(tmp_path):
    store = SecureStore(tmp_path)
    first = store.identity
    assert SecureStore(tmp_path).identity == first
    assert (tmp_path / "credentials.json").stat().st_mode & 0o777 == 0o600


def test_pairing_required_shows_request_and_retries(gateway):
    def respond(socket, frame):
        socket.reply(frame, error={"code": "NOT_PAIRED", "message": "pairing required", "details": {"code": "PAIRING_REQUIRED", "requestId": "req-1"}})

    FakeSocket.respond = respond
    gateway.configure("ws://h:1", "t")
    wait(lambda: gateway.state.kind == "pairing")
    assert gateway.state.request_id == "req-1"


@pytest.mark.parametrize("detail,expected", [("AUTH_TOKEN_MISMATCH", "Gateway token doesn't match."), ("AUTH_REQUIRED", "Gateway requires a token.")])
def test_auth_failure_stops_and_explains(gateway, detail, expected):
    def respond(socket, frame):
        socket.reply(frame, error={"code": "X", "message": "no", "details": {"code": detail}})

    FakeSocket.respond = respond
    gateway.configure("ws://h:1", "t")
    wait(lambda: gateway.state.kind == "auth_failed")
    assert gateway.state.message == expected
    count = len(FakeSocket.instances)
    assert not run_until(lambda: len(FakeSocket.instances) > count, 0.5)


def test_stale_device_token_is_dropped_and_retried(gateway):
    gateway.store.set_device_token("ws://h:1", {"token": "old", "deviceId": gateway.device_id})

    def respond(socket, frame):
        if socket is FakeSocket.instances[0]:
            socket.reply(frame, error={"code": "X", "message": "no", "details": {"code": "AUTH_TOKEN_MISMATCH"}})
        else:
            socket.reply(frame, HELLO)

    FakeSocket.respond = respond
    gateway.configure("ws://h:1", "")
    wait(lambda: gateway.state.is_connected)
    assert FakeSocket.instances[0].sent[0]["params"]["auth"] == {"token": "old"}
    assert "auth" not in FakeSocket.instances[1].sent[0]["params"]


def test_disconnect_reconnects_automatically(gateway):
    FakeSocket.respond = auto_hello
    gateway.configure("ws://h:1", "t")
    wait(lambda: gateway.state.is_connected)
    FakeSocket.instances[0].on_closed("closed (1006)")
    assert gateway.state.kind == "offline"
    wait(lambda: gateway.state.is_connected and len(FakeSocket.instances) == 2)


def test_pending_requests_fail_when_the_socket_drops(gateway):
    FakeSocket.respond = auto_hello
    gateway.configure("ws://h:1", "t")
    wait(lambda: gateway.state.is_connected)
    future = gateway.request("slow.call")
    FakeSocket.instances[0].on_closed("closed")
    assert future.done and isinstance(future.error, C.GatewayError)


def test_request_when_offline_fails_immediately(gateway):
    future = gateway.request("x")
    assert future.done and future.error.code == "NOT_CONNECTED"


def test_admin_upgrade_keeps_working_while_pending(gateway):
    C.PairedFlags.set("paired:ws://h:1")

    def respond(socket, frame):
        if "operator.admin" in frame["params"]["scopes"]:
            socket.reply(frame, error={"code": "NOT_PAIRED", "message": "p", "details": {"code": "PAIRING_REQUIRED", "requestId": "adm"}})
        else:
            socket.reply(frame, HELLO)

    FakeSocket.respond = respond
    gateway.set_request_admin(True)
    gateway.configure("ws://h:1", "t")
    wait(lambda: gateway.state.is_connected)
    assert gateway.admin_access == ("pending", "adm") and not gateway.has_admin


@pytest.mark.parametrize("raw,expected", [
    ("host:18789", "ws://host:18789"), ("http://h", "ws://h"), ("https://h/p", "wss://h/p"), ("wss://h", "wss://h"), ("", None), ("ws://", None), ("h:99999", None),
])
def test_url_normalization(raw, expected):
    assert C.normalized_url(raw) == expected


def test_http_url_resolution():
    assert C.http_url("wss://h:1/x", "/media/a?t=1") == "https://h:1/media/a?t=1"
    assert C.http_url("ws://h", "https://cdn/x") == "https://cdn/x"


# MARK: Session


@pytest.fixture
def session(gateway):
    FakeSocket.respond = lambda socket, frame: auto_hello(socket, frame) if frame["method"] == "connect" else socket.reply(frame, {"runId": "r1"} if frame["method"] == "chat.send" else {})
    agent = AgentSession(gateway, AppSettings())
    gateway.configure("ws://h:1", "t")
    wait(lambda: gateway.state.is_connected)
    return agent


def chat(session, **fields):
    session.handle_event("chat", {"sessionKey": "agent:main:main", **fields})


def sent(method):
    return [f for f in FakeSocket.instances[-1].sent if f["method"] == method]


def test_send_injects_the_format_note(session):
    session.send("hello")
    wait(lambda: sent("chat.send"))
    message = sent("chat.send")[0]["params"]["message"]
    assert message.startswith("<barclaw-format>") and message.endswith("hello")
    assert session.items[-1].role == "user" and session.activity.kind == "thinking"


def test_slash_commands_go_verbatim(session):
    session.send("/status")
    wait(lambda: sent("chat.send"))
    assert sent("chat.send")[0]["params"]["message"] == "/status"


def test_streaming_then_final(session):
    chat(session, state="delta", runId="r1", deltaText="Hel")
    chat(session, state="delta", runId="r1", deltaText="lo")
    assert session.items[-1].text == "Hello" and session.items[-1].is_streaming and session.activity.kind == "streaming"
    chat(session, state="final", runId="r1", message={"role": "assistant", "content": [{"type": "text", "text": "Hello!"}]})
    assert session.items[-1].text == "Hello!" and not session.items[-1].is_streaming and session.activity == IDLE
    assert session.attention == "reply"


def test_snapshot_never_appends_delta_twice(session):
    chat(session, state="delta", runId="r1", deltaText="b", message={"content": [{"type": "text", "text": "ab"}]})
    assert session.items[-1].text == "ab"


def test_no_glow_while_user_is_looking(session):
    session.is_user_looking = lambda: True
    chat(session, state="delta", runId="r1", deltaText="x")
    chat(session, state="final", runId="r1", message={"content": [{"type": "text", "text": "x"}]})
    assert session.attention is None


def test_repeated_final_does_not_glow_again(session):
    final = {"content": [{"type": "text", "text": "x"}]}
    chat(session, state="final", runId="r1", message=final)
    session.clear_attention()
    chat(session, state="final", runId="r1", message=final)
    assert session.attention is None


def test_mirrored_final_is_not_duplicated(session):
    final = {"content": [{"type": "text", "text": "same"}]}
    chat(session, state="final", runId="r1", message=final)
    chat(session, state="final", runId="r1-mirror", message=final)
    assert sum(1 for i in session.items if i.role == "assistant") == 1


def test_silent_run_is_invisible(session):
    chat(session, state="final", runId="hb", message={"content": []})
    assert not session.items and session.attention is None


def test_error_raises_attention(session):
    chat(session, state="error", runId="r1", errorMessage="boom")
    assert session.items[-1].role == "error" and session.items[-1].text == "boom" and session.attention == "error"


def test_events_for_other_sessions_are_ignored(session):
    session.handle_event("chat", {"sessionKey": "agent:main:other", "state": "delta", "runId": "r", "deltaText": "x"})
    assert not session.items


def test_approval_flow(session):
    session.handle_event("exec.approval.requested", {"id": "a1", "request": {"command": "rm x", "cwd": "/tmp"}})
    approval = session.approvals[0]
    assert approval.title == "rm x" and approval.detail == "in /tmp" and session.attention == "needs_input"
    assert approval.decisions == ["allow-once", "allow-always", "deny"]
    session.resolve_approval(approval, "deny")
    wait(lambda: sent("exec.approval.resolve"))
    assert not session.approvals and session.attention is None
    assert sent("exec.approval.resolve")[0]["params"] == {"id": "a1", "decision": "deny"}


def test_tool_progress_headline(session):
    session.send("x")
    session.handle_event("agent", {"sessionKey": "main", "runId": "r9", "stream": "tool", "data": {"phase": "start", "name": "exec", "toolCallId": "t", "args": {"command": "ls"}}})
    assert session.progress.headline == "Running `ls`" and session.activity.phase == "Running `ls`"
    session.handle_event("agent", {"sessionKey": "main", "runId": "r9", "stream": "tool", "data": {"phase": "result", "name": "exec", "toolCallId": "t"}})
    assert session.progress.active_step is None


def test_compact_needs_admin(session):
    opened = []
    session.on_needs_admin = lambda: opened.append(1)
    session.compact()
    assert opened and "admin" in session.toast and session.activity == IDLE


def test_late_images_attach_to_the_right_reply(session):
    chat(session, state="final", runId="r1", message={"content": [{"type": "text", "text": "caption"}]})
    assert session.attach_images({"role": "assistant", "content": [{"type": "text", "text": "caption"}, {"type": "image", "artifactId": "a"}]})
    assert [i.id for i in session.items[-1].images] == ["a"]
    assert not session.attach_images({"role": "assistant", "content": [{"type": "image", "artifactId": "a"}]})


def test_history_keeps_a_message_still_in_flight(session):
    session.send("pending message")
    wait(lambda: sent("chat.send"))
    session._apply_history({"messages": [{"role": "assistant", "content": "older"}]})
    assert [i.text for i in session.items] == ["older", "pending message"]


# MARK: Tasks


def test_task_returns_and_propagates():
    def inner():
        yield sleep(0.01)
        return 7

    def outer():
        return (yield spawn(inner())) + 1

    task = spawn(outer())
    assert run_until(lambda: task.done) and task.result == 8


def test_cancel_stops_a_sleeping_task():
    ran = []

    def body():
        yield sleep(0.05)
        ran.append(True)

    task = spawn(body())
    task.cancel()
    run_until(lambda: False, 0.1)
    assert not ran


def test_in_thread_delivers_errors():
    def boom():
        raise ValueError("x")

    future = in_thread(boom)
    assert run_until(lambda: future.done) and isinstance(future.error, ValueError)
