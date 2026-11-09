"""The Settings window."""
from __future__ import annotations

from typing import Any, Callable, Optional

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

from ..bar.panel import copy_text
from ..support import ExpectedError, app_version, log, spawn
from ..vision.reader import tesseract_languages
from ..vision.snapshot import ScreenReadError
from ..voice import controller as voice_module
from ..voice.hotkey import describe_accelerator
from .store import USAGE_WINDOWS


def heading(text: str) -> Gtk.Label:
    label = Gtk.Label(xalign=0)
    label.set_markup(f"<b>{GLib.markup_escape_text(text)}</b>")
    return label


def note(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=70)
    label.get_style_context().add_class("dim-label")
    return label


class Page(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.set_border_width(18)
        self.grid_row = 0
        self.grid = Gtk.Grid(column_spacing=14, row_spacing=10)
        self.pack_start(self.grid, False, False, 0)

    def row(self, title: str, widget: Gtk.Widget) -> Gtk.Widget:
        label = Gtk.Label(label=title, xalign=0)
        self.grid.attach(label, 0, self.grid_row, 1, 1)
        widget.set_hexpand(True)
        self.grid.attach(widget, 1, self.grid_row, 1, 1)
        self.grid_row += 1
        return widget

    def wide(self, widget: Gtk.Widget) -> Gtk.Widget:
        self.grid.attach(widget, 0, self.grid_row, 2, 1)
        self.grid_row += 1
        return widget

    def check(self, title: str, key: str, settings: Any, after: Optional[Callable[[], None]] = None) -> Gtk.CheckButton:
        button = Gtk.CheckButton(label=title, active=settings.get(key))

        def toggled(_b: Gtk.CheckButton) -> None:
            settings.set(key, button.get_active())
            if after:
                after()

        button.connect("toggled", toggled)
        self.wide(button)
        return button


def command_box(lines: list) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    for line in lines:
        row = Gtk.Box(spacing=8)
        text = Gtk.Label(label=line, xalign=0, selectable=True)
        text.set_ellipsize(Pango.EllipsizeMode.END)
        text.get_style_context().add_class("monospace")
        row.pack_start(text, True, True, 0)
        copy = Gtk.Button(label="Copy")
        copy.connect("clicked", lambda _b, value=line: copy_text(value))
        row.pack_end(copy, False, False, 0)
        box.pack_start(row, False, False, 0)
    return box


class SettingsWindow:
    TABS = (("gateway", "Gateway"), ("agent", "Agent"), ("panel", "Panel"), ("voice", "Voice"), ("screen", "Screen"), ("updates", "Updates"))

    def __init__(self, app: Any) -> None:
        self.app = app
        self.window = Gtk.Window(title="BarClaw Settings")
        self.window.set_default_size(620, 520)
        self.window.set_wmclass("barclaw-settings", "BarClaw")
        self.window.connect("delete-event", lambda *_: (self.window.hide(), True)[1])
        self.window.connect("key-press-event", self._key)
        layout = Gtk.Box()
        sidebar = Gtk.StackSidebar()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=100)
        sidebar.set_stack(self.stack)
        layout.pack_start(sidebar, False, False, 0)
        layout.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        layout.pack_start(self.stack, True, True, 0)
        self.window.add(layout)
        self._build_gateway()
        self._build_agent()
        self._build_panel()
        self._build_voice()
        self._build_screen()
        self._build_updates()
        layout.show_all()
        for observable in (app.gateway, app.screen_node, app.updater, self.installer):
            observable.observe(self.refresh)
        self._recording = False

    def _key(self, _w: Gtk.Window, event: Gdk.EventKey) -> bool:
        if self._recording:
            return self._capture_shortcut(event)
        if event.keyval == Gdk.KEY_Escape:
            self.window.hide()
            return True
        return False

    def show(self, tab: Optional[str] = None) -> None:
        if tab:
            self.stack.set_visible_child_name(tab)
        self.refresh()
        self.window.present_with_time(Gtk.get_current_event_time() or Gdk.CURRENT_TIME)

    def _add(self, name: str, title: str) -> Page:
        page = Page()
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(page)
        self.stack.add_titled(scroller, name, title)
        return page

    # MARK: Gateway

    def _build_gateway(self) -> None:
        page = self._add("gateway", "Gateway")
        settings = self.app.settings
        self.url_entry = page.row("Gateway URL", Gtk.Entry(text=settings.get("gateway_url"), placeholder_text="ws://127.0.0.1:18789"))
        self.token_entry = page.row("Token", Gtk.Entry(text=settings.gateway_token, visibility=False, placeholder_text="gateway.auth.token"))
        self.session_entry = page.row("Session", Gtk.Entry(text=settings.get("session_key")))
        save = Gtk.Button(label="Save & Connect")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", lambda _b: self._save_connection())
        save.set_halign(Gtk.Align.START)
        page.wide(save)
        self.gateway_status = page.wide(note(""))
        self.admin_toggle = page.check("Admin access (needed for Compact and Reset, and to approve the node itself)", "request_admin_access", settings, self.app.apply_connection_settings)
        self.admin_status = page.wide(note(""))
        self.admin_commands = page.wide(Gtk.Box())
        self.device_label = page.wide(note(""))

    def _save_connection(self) -> None:
        settings = self.app.settings
        previous = settings.resolved_session_key
        settings.set("gateway_url", self.url_entry.get_text().strip())
        settings.gateway_token = self.token_entry.get_text().strip()
        settings.set("session_key", self.session_entry.get_text().strip() or "main")
        self.app.apply_connection_settings()
        if previous != settings.resolved_session_key:
            self.app.session.session_changed(previous)

    def _refresh_gateway(self) -> None:
        gateway = self.app.gateway
        state = gateway.state
        text = {
            "connected": f"Connected · {gateway.server_version or 'gateway'}",
            "connecting": "Connecting…",
            "offline": f"Offline: {state.message}",
            "pairing": "Waiting for approval on the gateway host",
            "auth_failed": f"Couldn't sign in: {state.message}",
            "idle": "Not connected",
        }[state.kind]
        self.gateway_status.set_text(text)
        access, request_id = gateway.admin_access
        lines: list = []
        if access == "granted":
            self.admin_status.set_text("Admin access is on.")
        elif access == "pending":
            self.admin_status.set_text("Waiting for approval. Run this on the gateway host:")
            lines = [f"openclaw devices approve {request_id or '--latest'}"]
        else:
            self.admin_status.set_text("")
        self._swap_commands("admin_commands", lines)
        self.device_label.set_text(f"Device ID: {gateway.device_id[:16]}…")

    def _swap_commands(self, attribute: str, lines: list) -> None:
        current: Gtk.Box = getattr(self, attribute)
        if getattr(current, "_lines", None) == lines:
            return
        current._lines = lines
        for child in current.get_children():
            current.remove(child)
            child.destroy()
        if lines:
            current.pack_start(command_box(lines), True, True, 0)
        current.show_all()

    # MARK: Agent

    def _build_agent(self) -> None:
        page = self._add("agent", "Agent")
        settings = self.app.settings
        name = page.row("Name", Gtk.Entry(text=settings.get("agent_name")))
        name.connect("changed", lambda e: settings.set("agent_name", e.get_text()))
        emoji = page.row("Emoji", Gtk.Entry(text=settings.get("agent_emoji"), max_length=8))
        emoji.connect("changed", lambda e: settings.set("agent_emoji", e.get_text()))
        page.check("Ask the agent to reply in Markdown and use charts (adds a short note to each message)", "inject_markdown", settings)
        page.check("Glow the icon when there's a new reply", "glow_on_reply", settings)
        page.check("Desktop notification when the agent needs your approval", "notify_needs_input", settings)
        page.check("Desktop notification for every new reply", "notify_on_reply", settings)

    # MARK: Panel

    def _build_panel(self) -> None:
        page = self._add("panel", "Panel")
        settings = self.app.settings

        def slider(title: str, key: str, low: float, high: float) -> None:
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, low, high, 0.05)
            scale.set_value(settings.get(key))
            scale.connect("format-value", lambda _s, v: f"{int(v * 100)}%")
            scale.connect("value-changed", lambda s: settings.set(key, s.get_value()))
            page.row(title, scale)

        slider("Maximum height", "max_height_fraction", 0.3, 0.95)
        slider("Maximum width", "max_width_fraction", 0.25, 0.9)
        page.check("Show Codex usage", "show_usage", settings)
        combo = page.row("Usage window", Gtk.ComboBoxText())
        for key, (title, _short) in USAGE_WINDOWS.items():
            combo.append(key, title)
        combo.set_active_id(settings.get("usage_window"))
        combo.connect("changed", lambda c: settings.set("usage_window", c.get_active_id()))
        page.check("Start BarClaw when I log in", "autostart", settings, self.app.apply_autostart)
        self.tray_status = page.wide(note(""))

    # MARK: Voice

    def _build_voice(self) -> None:
        page = self._add("voice", "Voice")
        settings = self.app.settings
        self.installer = voice_module.SpeechInstaller()
        page.check("Hold a shortcut to talk", "push_to_talk_enabled", settings, self.app.apply_hotkey)
        self.shortcut_button = Gtk.Button(label=describe_accelerator(settings.get("push_to_talk")))
        self.shortcut_button.connect("clicked", self._start_recording)
        page.row("Shortcut", self.shortcut_button)
        self.hotkey_error = page.wide(note(""))
        page.check("Send automatically when I let go", "auto_send_voice", settings)
        engine = page.row("Recognition", Gtk.ComboBoxText())
        engine.append("vosk", "Built in (Vosk, on this computer, CPU only)")
        engine.append("command", "My own command (e.g. whisper.cpp)")
        engine.set_active_id(settings.get("speech_engine"))
        engine.connect("changed", lambda c: (settings.set("speech_engine", c.get_active_id()), self.refresh()))
        language = page.row("Language", Gtk.ComboBoxText())
        for code, (title, _model) in voice_module.MODELS.items():
            language.append(code, title)
        language.set_active_id(settings.get("speech_language") if settings.get("speech_language") in voice_module.MODELS else "en-us")
        language.connect("changed", lambda c: (settings.set("speech_language", c.get_active_id()), self.refresh()))
        self.language_combo = language
        self.install_button = Gtk.Button(label="Install speech recognition")
        self.install_button.set_halign(Gtk.Align.START)
        self.install_button.connect("clicked", lambda _b: self.installer.install(settings.get("speech_language")))
        page.wide(self.install_button)
        self.install_status = page.wide(note(""))
        command = page.row("Command", Gtk.Entry(text=settings.get("speech_command"), placeholder_text="whisper-cli -m ~/ggml-base.bin -nt -f {file}"))
        command.connect("changed", lambda e: settings.set("speech_command", e.get_text()))
        self.command_entry = command
        page.wide(note("The command gets a 16 kHz WAV file ({file}) and prints the transcript to stdout. Audio is only captured while you hold the shortcut."))

    def _start_recording(self, _button: Gtk.Button) -> None:
        self._recording = True
        self.app.is_recording_shortcut = True
        self.shortcut_button.set_label("Press a shortcut…")
        self.shortcut_button.grab_focus()

    def _capture_shortcut(self, event: Gdk.EventKey) -> bool:
        keyval = Gdk.keyval_to_lower(event.keyval)
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        if keyval in (Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R, Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R, Gdk.KEY_Meta_L, Gdk.KEY_Meta_R, Gdk.KEY_ISO_Level3_Shift):
            return True
        settings = self.app.settings
        if keyval != Gdk.KEY_Escape and (modifiers or Gdk.KEY_F1 <= keyval <= Gdk.KEY_F35):
            settings.set("push_to_talk", Gtk.accelerator_name(keyval, modifiers))
        self._recording = False
        self.app.is_recording_shortcut = False
        self.shortcut_button.set_label(describe_accelerator(settings.get("push_to_talk")))
        self.refresh()
        return True

    def _refresh_voice(self) -> None:
        settings = self.app.settings
        self.hotkey_error.set_text(self.app.hotkey_error or "")
        builtin = settings.get("speech_engine") == "vosk"
        language = settings.get("speech_language")
        installed = voice_module.is_engine_installed() and voice_module.is_model_installed(language)
        self.language_combo.set_sensitive(builtin)
        self.command_entry.set_sensitive(not builtin)
        working = self.installer.state == "working"
        self.install_button.set_sensitive(builtin and not installed and not working)
        if working or self.installer.state == "failed":
            self.install_status.set_text(self.installer.detail)
        else:
            self.install_status.set_text("Installed. Speech never leaves this computer." if installed else "Downloads the Vosk engine and a small language model (about 50 MB, CPU only).")

    # MARK: Screen

    def _build_screen(self) -> None:
        page = self._add("screen", "Screen")
        settings = self.app.settings
        page.check("Visual intelligence: let BarClaw and the agent read the screen when asked", "visual_intelligence", settings, self.app.screen_settings_changed)
        page.check("Attach the screen automatically when a message refers to it", "auto_attach_screen", settings)
        self.node_status = page.wide(note(""))
        self.node_commands = page.wide(Gtk.Box())
        self.ocr_status = page.wide(note(""))
        test = Gtk.Button(label="Test Capture")
        test.set_halign(Gtk.Align.START)
        test.connect("clicked", lambda _b: spawn(self._test_capture()))
        page.wide(test)
        self.capture_status = page.wide(note(""))
        self.capture_preview = page.wide(Gtk.Image())

    def _test_capture(self):
        self.capture_status.set_text("Capturing…")
        self.window.hide()
        try:
            snapshot = yield self.app.screen_reader.test_capture()
        except ScreenReadError as error:
            self.capture_status.set_text(str(error))
            self.window.present()
            return
        self.window.present()
        loader = GdkPixbuf.PixbufLoader()
        loader.write(snapshot.image_data)
        loader.close()
        pixbuf = loader.get_pixbuf()
        scaled = pixbuf.scale_simple(420, max(1, int(pixbuf.get_height() * 420 / pixbuf.get_width())), GdkPixbuf.InterpType.BILINEAR)
        self.capture_preview.set_from_pixbuf(scaled)
        words = len(snapshot.text.split())
        self.capture_status.set_text(f"{snapshot.width}×{snapshot.height} · {snapshot.app_description or 'unknown app'} · OCR found {words} words. Nothing was sent or kept.")

    def _refresh_screen(self) -> None:
        node = self.app.screen_node
        kind, value = node.status
        text = {
            "off": "Visual intelligence is off.",
            "waiting": "Waiting for the gateway connection.",
            "connecting": "Connecting the BarClaw node…",
            "needs_device": "Waiting for approval. Turn on Admin access, or run these on the gateway host:",
            "needs_command": "One more approval: run this on the gateway host:",
            "approving": "Approving this computer…",
            "ready": "Your agent can look at your screen through BarClaw when a request needs it.",
            "failed": f"Node failed: {value}",
        }[kind]
        if node.publish_error:
            text += f" (publish problem: {node.publish_error})"
        self.node_status.set_text(text)
        self._swap_commands("node_commands", node.approval_commands)
        languages = tesseract_languages()
        self.ocr_status.set_text(f"OCR: Tesseract ({', '.join(languages)})" if languages else "OCR isn't available: install tesseract-ocr. Screenshots still work without it.")

    # MARK: Updates

    def _build_updates(self) -> None:
        page = self._add("updates", "Updates")
        page.wide(heading(f"BarClaw {app_version()}"))
        self.update_button = Gtk.Button(label="Check for Updates")
        self.update_button.set_halign(Gtk.Align.START)
        self.update_button.connect("clicked", lambda _b: self.app.updater.check_for_updates())
        page.wide(self.update_button)
        self.update_status = page.wide(note(""))

    def _refresh_updates(self) -> None:
        updater = self.app.updater
        self.update_button.set_sensitive(not updater.is_busy)
        self.update_status.set_text(updater.status_text)

    def refresh(self) -> None:
        self._refresh_gateway()
        self._refresh_voice()
        self._refresh_screen()
        self._refresh_updates()
        self.tray_status.set_text(self.app.tray_description())
