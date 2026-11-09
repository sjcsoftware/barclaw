"""Root object wiring settings, the gateway connection, the agent session, push-to-talk, and the bar UI."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from gi.repository import Gdk, Gio, GLib, Gtk

from .agent.session import AgentSession
from .bar.panel import Panel
from .bar.theme import install_css
from .bar.tray import SniTray, TrayState, XEmbedTray, make_tray, shell_host_present
from .gateway.client import GatewayClient
from .settings.store import AppSettings
from .settings.view import SettingsWindow
from .support import Future, Observable, Task, app_version, log, observed, setup_logging, sleep, spawn
from .updater import Updater
from .vision.context import refers_to_screen
from .vision.glow import ScreenEdgeGlow
from .vision.node import ScreenNode
from .vision.reader import CaptureOptions, ScreenReader
from .vision.snapshot import ScreenReadError, ScreenSnapshot
from .voice import command as voice_command
from .voice.command import VoiceCommand
from .voice.controller import VoiceController
from .voice.hotkey import describe_accelerator, make_hotkey

APP_ID = "dev.barclaw.Barclaw"
CONTROL_XML = """
<node><interface name="dev.barclaw.Control">
  <method name="Toggle"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="TalkStart"/><method name="TalkStop"/>
  <property name="Emoji" type="s" access="read"/><property name="Mode" type="s" access="read"/>
  <property name="Tooltip" type="s" access="read"/><property name="UsagePercent" type="i" access="read"/>
  <signal name="Changed"/>
</interface></node>
"""


class AppModel(Observable):
    spoken_text: Optional[str] = observed(None)

    def __init__(self) -> None:
        super().__init__()
        self.settings = AppSettings()
        self.gateway = GatewayClient()
        self.session = AgentSession(self.gateway, self.settings)
        self.voice = VoiceController(self.settings)
        self.updater = Updater()
        self.screen_reader = ScreenReader()
        self.screen_node = ScreenNode(self.gateway, self.settings, self.screen_reader)
        self.edge_glow = ScreenEdgeGlow()
        self.hotkey: Any = make_hotkey()
        self.hotkey_error: Optional[str] = None
        self._recording_shortcut = False
        self.tray = make_tray()
        self.panel = Panel(self)
        self.settings_window = SettingsWindow(self)
        self._expanded_for_voice = False
        self._bubble_task: Optional[Task] = None
        self._control: Optional[Gio.DBusConnection] = None
        self._was_hidden_for_capture: list = []
        self._applied_hotkey: Any = object()

        session = self.session
        session.is_user_looking = lambda: self.panel.is_expanded
        session.on_assistant_output = self._assistant_produced
        session.on_needs_admin = self.open_admin_settings
        session.on_switch_session = self.switch_session
        session.on_attention = self._attention_raised
        session.capture_screen = self._capture_for_message
        self.screen_node.on_agent_read = self._agent_read_screen
        self.screen_reader.on_reading = self._reading_changed
        self.screen_reader.hide_own_windows = self._hide_for_capture
        self.screen_reader.restore_own_windows = self._restore_after_capture
        self.voice.on_finished = self._handle_transcript
        self.voice.on_failed = self._voice_failed
        self.hotkey.on_press = self.push_to_talk_pressed
        self.hotkey.on_release = self.push_to_talk_released

        self.tray.on_activate = self.panel.toggle
        self.tray.on_secondary = self.toggle_listening
        self.tray.menu_items = [
            ("Open BarClaw", self.panel.expand),
            ("Chat", lambda: self.panel.expand(page="chat")),
            ("Talk", self.toggle_listening),
            (None, None),
            ("Settings…", self.open_settings),
            ("Check for Updates…", self.check_for_updates),
            (None, None),
            ("Quit", self.quit),
        ]
        self.tray.rebuild_menu()
        if isinstance(self.tray, SniTray):
            self.tray.on_gone = self._tray_host_gone
        self.quit_handler: Callable[[], None] = lambda: None

        for observable in (self.settings, self.gateway, self.session, self.voice, self.screen_node, self):
            observable.observe(self._sync)
        self.gateway.observe(self.screen_node.apply)
        self.settings.observe(self.apply_hotkey)

    def start(self) -> None:
        self.apply_connection_settings()
        self.apply_hotkey()
        self.apply_autostart()
        self._sync()
        GLib.timeout_add(2500, self._check_tray)
        if not self.settings.get("gateway_url") or not self.settings.gateway_token and "127.0.0.1" not in self.settings.get("gateway_url"):
            self.open_settings()

    def quit(self) -> None:
        self.voice.cancel()
        self.gateway.stop()
        self.screen_node.client.stop()
        self.quit_handler()

    # MARK: Tray

    def _check_tray(self) -> bool:
        if not self.tray.is_visible() and not shell_host_present():
            log.info("No system tray is visible; opening the panel directly")
            if not self.panel.is_expanded:
                self.panel.expand()
            self.session.show_toast("No tray found: see “Desktops” in the README")
        return GLib.SOURCE_REMOVE

    def _tray_host_gone(self) -> None:
        log.info("The tray host went away; waiting for it to return")

    def tray_description(self) -> str:
        if isinstance(self.tray, SniTray):
            return "Menu bar: StatusNotifierItem" + ("" if self.tray.is_visible() else " (no host yet)")
        if isinstance(self.tray, XEmbedTray):
            return "Menu bar: XEmbed tray" + ("" if self.tray.is_visible() else " (no tray found)")
        return "Menu bar: none. Open BarClaw with `barclaw --toggle`."

    def _tray_state(self) -> TrayState:
        session, gateway = self.session, self.gateway
        attention = session.attention
        if not gateway.state.is_connected:
            mode = "offline"
        elif attention == "needs_input":
            mode = "attention_input"
        elif attention == "error":
            mode = "attention_error"
        elif attention == "reply":
            mode = "attention_reply"
        elif self.voice.is_active:
            mode = "listening"
        elif session.activity.is_busy:
            mode = "thinking"
        else:
            mode = "idle"
        name = self.settings.resolved_name
        tooltip = {
            "offline": f"{name}: offline", "attention_input": f"{name} needs your approval",
            "attention_error": f"{name} hit an error", "attention_reply": f"{name} replied",
            "listening": "Listening…", "thinking": f"{name} is working…", "idle": name,
        }[mode]
        usage = None
        if self.settings.get("show_usage") and session.usage is not None:
            window = session.usage.best_window(self.settings.get("usage_window"))
            if window is not None:
                usage = window.used_percent / 100
                tooltip += f" · {window.label} {int(window.used_percent)}%"
        return TrayState(self.settings.resolved_emoji, tooltip, mode, usage)

    def _sync(self) -> None:
        state = self._tray_state()
        changed = state != self.tray.state
        self.tray.update(state)
        if self.panel.is_expanded:
            self.panel.refresh()
        else:
            self.panel.composer.refresh()
        if changed:
            self._emit_control_changed()
        if self.settings_window.window.get_visible():
            self.settings_window.refresh()

    def panel_visibility_changed(self) -> None:
        self._sync()

    # MARK: Control interface (used by the GNOME Shell extension)

    def export_control(self, connection: Gio.DBusConnection) -> None:
        info = Gio.DBusNodeInfo.new_for_xml(CONTROL_XML).interfaces[0]
        connection.register_object("/dev/barclaw/Control", info, self._control_method, self._control_property)
        self._control = connection

    def _control_method(self, _bus, _sender, _path, _iface, method: str, params: GLib.Variant, invocation: Gio.DBusMethodInvocation) -> None:
        invocation.return_value(None)
        if method == "Toggle":
            self.panel.toggle(*params.unpack())
        elif method == "TalkStart":
            self.push_to_talk_pressed()
        elif method == "TalkStop":
            self.push_to_talk_released()

    def _control_property(self, _bus, _sender, _path, _iface, name: str) -> Optional[GLib.Variant]:
        state = self.tray.state
        return {
            "Emoji": GLib.Variant("s", state.emoji), "Mode": GLib.Variant("s", state.mode),
            "Tooltip": GLib.Variant("s", state.tooltip),
            "UsagePercent": GLib.Variant("i", -1 if state.usage is None else int(state.usage * 100)),
        }.get(name)

    def _emit_control_changed(self) -> None:
        if self._control is not None:
            try:
                self._control.emit_signal(None, "/dev/barclaw/Control", "dev.barclaw.Control", "Changed", None)
            except GLib.Error:
                pass

    # MARK: Actions

    def apply_connection_settings(self) -> None:
        self.gateway.set_request_admin(self.settings.get("request_admin_access"))
        self.gateway.configure(self.settings.get("gateway_url"), self.settings.gateway_token)

    def switch_session(self, key: str) -> None:
        previous = self.settings.resolved_session_key
        self.settings.set("session_key", key)
        self.session.session_changed(previous)

    def open_settings(self, tab: Optional[str] = None) -> None:
        self.settings_window.show(tab)

    def open_admin_settings(self) -> None:
        self.open_settings("gateway")

    def check_for_updates(self) -> None:
        self.open_settings("updates")
        self.updater.check_for_updates()

    def screen_settings_changed(self) -> None:
        self.screen_node.apply()

    def open_pixbuf(self, pixbuf: Any) -> None:
        """Opens an image in the default viewer from a temporary file."""
        directory = Path(tempfile.mkdtemp(prefix="barclaw-image-"))
        path = directory / "image.png"
        pixbuf.savev(str(path), "png", [], [])
        try:
            Gtk.show_uri_on_window(None, path.as_uri(), Gdk.CURRENT_TIME)
        except GLib.Error as error:
            log.error("Couldn't open the image: %s", error.message)

    def apply_autostart(self) -> None:
        target = Path(GLib.get_user_config_dir()) / "autostart" / "dev.barclaw.Barclaw.desktop"
        if os.environ.get("BARCLAW_CONFIG_DIR"):
            return  # Test and development runs must not touch the real session.
        if self.settings.get("autostart"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"[Desktop Entry]\nType=Application\nName=BarClaw\nExec={sys.executable} -m barclaw\nX-GNOME-Autostart-enabled=true\n")
        elif target.exists():
            target.unlink()

    # MARK: Hotkey

    @property
    def is_recording_shortcut(self) -> bool:
        return self._recording_shortcut

    @is_recording_shortcut.setter
    def is_recording_shortcut(self, value: bool) -> None:
        self._recording_shortcut = value
        self.apply_hotkey()

    def apply_hotkey(self) -> None:
        combo = self.settings.get("push_to_talk") if self.settings.get("push_to_talk_enabled") and not self._recording_shortcut else None
        if combo == self._applied_hotkey:
            return
        self._applied_hotkey = combo
        ok = self.hotkey.register(combo)
        if combo and not ok:
            self.hotkey_error = f"{describe_accelerator(combo)} is already used by another app. Pick a different shortcut."
        elif not self._recording_shortcut:
            self.hotkey_error = None

    # MARK: Visual intelligence

    def should_share_screen(self, text: str, override: Optional[bool] = None) -> bool:
        if not self.settings.get("visual_intelligence") or text.strip().startswith("/"):
            return False
        if override is not None:
            return override
        return self.settings.get("auto_attach_screen") and refers_to_screen(text)

    def _capture_for_message(self, max_bytes: Optional[int]) -> Future:
        if not self.settings.get("visual_intelligence"):
            future = Future()
            future.set_error(ScreenReadError.disabled())
            return future
        return self.screen_reader.capture(CaptureOptions(max_bytes=max_bytes))

    def _reading_changed(self, monitor: Optional[Gdk.Monitor]) -> None:
        if monitor is not None:
            self.edge_glow.begin(monitor)
        else:
            self.edge_glow.end()

    def _hide_for_capture(self) -> Future:
        """BarClaw's own windows are never in the picture: hide them for the instant of the grab."""
        windows = [w for w in (self.panel.window, self.settings_window.window) if w.get_visible()]
        self._was_hidden_for_capture = windows
        future = Future()
        if not windows:
            future.set_result(None)
            return future
        self.panel.suspend_autohide(True)
        for window in windows:
            window.hide()

        def settled() -> bool:
            future.set_result(None)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(90, settled)  # Lets the display server repaint what's underneath.
        return future

    def _restore_after_capture(self) -> None:
        for window in self._was_hidden_for_capture:
            window.show()
        if self.panel.window in self._was_hidden_for_capture:
            self.panel.window.present()
        self._was_hidden_for_capture = []
        GLib.timeout_add(200, lambda: (self.panel.suspend_autohide(False), GLib.SOURCE_REMOVE)[1])

    def _agent_read_screen(self, result: Any) -> None:
        """The agent looked at the screen through the node: say so, so it never happens unnoticed."""
        name = self.settings.resolved_name
        if isinstance(result, ScreenSnapshot):
            place = f" ({result.app_name})" if result.app_name else ""
            self.session.show_toast(f"{name} looked at your screen{place}")
            self._notify(f"{name} looked at your screen{place}", "")
        elif isinstance(result, ScreenReadError) and result.code == "SCREEN_PERMISSION_DENIED":
            self.session.show_toast(f"Allow screen capture so {name} can see your screen")

    # MARK: Push-to-talk

    def toggle_listening(self) -> None:
        if self.voice.is_active:
            self.push_to_talk_released()
        else:
            self.push_to_talk_pressed()

    def push_to_talk_pressed(self) -> None:
        self._expanded_for_voice = not self.panel.is_expanded
        # Talking always happens on the home screen, with the agent front and center.
        self.panel.expand(page="home")
        self.session.clear_attention()
        self._cancel_bubble()
        self.spoken_text = None
        self.voice.begin()

    def push_to_talk_released(self) -> None:
        self.voice.end()

    def _cancel_bubble(self) -> None:
        if self._bubble_task is not None:
            self._bubble_task.cancel()
            self._bubble_task = None

    def _voice_failed(self) -> None:
        if not self._expanded_for_voice:
            return
        self._cancel_bubble()

        def tidy():
            yield sleep(3)
            if self.panel.page == "home" and not self.voice.is_active and not self.panel.pinned:
                self.panel.collapse()

        self._bubble_task = spawn(tidy())

    def _handle_transcript(self, text: str) -> None:
        self.spoken_text = text
        command = voice_command.parse(text)
        if command is not None:
            self.perform(command)
            self._clear_bubble(2.5, True)
        elif self.settings.get("auto_send_voice"):
            self.session.send(text, share_screen=self.should_share_screen(text))
            # Stay open on the home screen while the agent thinks; the reply opens chat.
        else:
            self.spoken_text = None
            self.panel.composer.set_text(text)
            self.panel.show_chat(True)

    def _clear_bubble(self, delay: float, collapse: bool) -> None:
        self._cancel_bubble()

        def body():
            yield sleep(delay)
            self.spoken_text = None
            if collapse and self._expanded_for_voice and self.panel.page == "home" and not self.voice.is_active and not self.panel.pinned:
                self.panel.collapse()

        self._bubble_task = spawn(body())

    def _assistant_produced(self) -> None:
        """Replies always land in the chat screen: switch now if open, otherwise on next open."""
        self._cancel_bubble()
        self.spoken_text = None
        if self.panel.is_expanded:
            self.panel.show_chat(False)
        else:
            self.panel.chat_pending = True

    def perform(self, command: VoiceCommand) -> None:
        session = self.session
        if command.kind == "stop":
            session.abort()
        elif command.kind == "compact":
            session.compact()
        elif command.kind == "new_session":
            session.create_session()
        elif command.kind == "set_effort":
            session.set_thinking(command.value)
        elif command.kind == "set_model":
            if command.value in ("default", "the default", "auto"):
                session.set_model(None)
            else:
                model = voice_command.match_model(command.value, session.models)
                if model is not None:
                    session.set_model(model.id)
                else:
                    session.show_toast(f"No model matching “{command.value}”")

    # MARK: Notifications

    def _attention_raised(self, reason: str) -> None:
        name = self.settings.resolved_name
        if reason == "needs_input" and self.settings.get("notify_needs_input"):
            approval = self.session.approvals[-1] if self.session.approvals else None
            self._notify(f"{name} needs your approval", approval.title if approval else "")
        elif reason == "reply" and self.settings.get("notify_on_reply"):
            self._notify(f"{name} replied", (self.session.latest_assistant_text or "")[:160])

    def _notify(self, title: str, body: str) -> None:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return
        args = GLib.Variant("(susssasa{sv}i)", ("BarClaw", 0, "dev.barclaw.Barclaw", title, body, [], {}, 5000))
        bus.call("org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications", "Notify", args, None, Gio.DBusCallFlags.NONE, 2000, None, lambda *_: None)


class BarClawApplication(Gtk.Application):
    """Single-instance, with command-line verbs forwarded to the running process."""

    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.model: Optional[AppModel] = None
        options = [
            ("toggle", "Open or close the panel"), ("chat", "Open the chat screen"),
            ("settings", "Open Settings"), ("talk-start", "Start listening (for compositors without global shortcuts)"),
            ("talk-stop", "Stop listening and send"), ("talk-toggle", "Toggle listening"), ("quit", "Quit BarClaw"),
            ("version", "Print the version"),
        ]
        for name, description in options:
            self.add_main_option(name, 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, description, None)

    def do_handle_local_options(self, options: GLib.VariantDict) -> int:
        """`--version` answers here, without starting or disturbing a running instance."""
        if options.contains("version"):
            print(f"BarClaw {app_version()}")
            return 0
        return -1

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        install_css()
        self.model = AppModel()
        self.model.quit_handler = self.quit
        connection = self.get_dbus_connection()
        if connection is not None:
            self.model.export_control(connection)
        self.hold()
        self.model.start()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict().end().unpack()
        model = self.model
        if options.get("quit"):
            model.quit()
        elif options.get("toggle"):
            model.panel.toggle()
        elif options.get("chat"):
            model.panel.expand(page="chat")
        elif options.get("settings"):
            model.open_settings()
        elif options.get("talk-start"):
            model.push_to_talk_pressed()
        elif options.get("talk-stop"):
            model.push_to_talk_released()
        elif options.get("talk-toggle"):
            model.toggle_listening()
        return 0


def main() -> int:
    setup_logging()
    GLib.set_prgname("barclaw")
    GLib.set_application_name("BarClaw")
    return BarClawApplication().run(sys.argv)
