"""The panel that drops down from the menu bar icon: the home screen and the chat screen."""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

import cairo
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

from ..agent.models import ChatItem, PendingApproval
from ..agent.session import short_model_name
from ..markdown.view import MarkdownView, copy_text, make_label, open_uri
from ..settings.store import USAGE_WINDOWS
from ..support import log
from ..vision.context import refers_to_screen
from ..vision.windows import session_type
from ..voice.hotkey import describe_accelerator
from .widgets import AgentStage, ContextRing, ThinkingDots, UsageBar, Waveform

try:
    import gi

    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import GtkLayerShell
except (ValueError, ImportError):
    GtkLayerShell = None

HOME_WIDTH = 600
CHAT_WIDTH = 620
BAR_GAP = 6


def label(text: str, css: Optional[str] = None, xalign: float = 0.0, ellipsize: bool = False) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=xalign)
    if css:
        widget.get_style_context().add_class(css)
    if ellipsize:
        widget.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
    return widget


def icon_button(glyph: str, css: str, tooltip: str, on_click: Callable[[], None]) -> Gtk.Button:
    button = Gtk.Button(label=glyph, can_focus=False, tooltip_text=tooltip)
    button.get_style_context().add_class(css)
    button.connect("clicked", lambda _b: on_click())
    return button


def chip(text: str, on_click: Callable[[], None]) -> Gtk.Button:
    button = Gtk.Button(label=text, can_focus=False)
    button.get_style_context().add_class("chip")
    button.connect("clicked", lambda _b: on_click())
    return button


def clear(box: Gtk.Container) -> None:
    for child in box.get_children():
        box.remove(child)
        child.destroy()


def approval_title(decision: str) -> str:
    return {"allow-once": "Allow once", "allow-always": "Always allow", "deny": "Deny"}.get(decision, decision.replace("-", " ").title())


def wants_wide_layout(text: Optional[str]) -> bool:
    """Long or structured replies get the wide layout."""
    return bool(text) and (len(text) > 650 or "```" in text or "\n|" in text or text.startswith("|"))


class PillAction(Gtk.Button):
    """A pill button used around the agent on the home screen."""

    def __init__(self, glyph: str, title: str, on_click: Callable[[], None], menu: bool = False, prominent: bool = False, trailing: Optional[Gtk.Widget] = None) -> None:
        super().__init__(can_focus=False)
        self.get_style_context().add_class("pill")
        if prominent:
            self.get_style_context().add_class("prominent")
        self.set_size_request(156, 30)
        row = Gtk.Box(spacing=7)
        row.pack_start(label(glyph), False, False, 0)
        self.title = label(title, ellipsize=True)
        row.pack_start(self.title, True, True, 0)
        if trailing is not None:
            row.pack_end(trailing, False, False, 0)
        self.badge = label("", "badge")
        self.badge.set_valign(Gtk.Align.CENTER)
        self.badge.set_no_show_all(True)
        row.pack_end(self.badge, False, False, 0)
        if menu:
            row.pack_end(label("⌄", "faint"), False, False, 0)
        self.add(row)
        self.connect("clicked", lambda _b: on_click())


class ChatRow(Gtk.Box):
    """One message. Assistant text goes through MarkdownView so only changed blocks are rebuilt."""

    def __init__(self, item: ChatItem, app: Any) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.item_id = item.id
        self.app = app
        self.markdown: Optional[MarkdownView] = None
        self.user_label: Optional[Gtk.Label] = None
        self.images_box: Optional[Gtk.Box] = None
        self.screen_chip: Optional[Gtk.Label] = None
        self.copy_row: Optional[Gtk.Box] = None
        self._shown_images: dict = {}
        self._text = item.text
        if item.role == "user":
            self.screen_chip = label("", "chip-screen", xalign=1)
            self.screen_chip.set_halign(Gtk.Align.END)
            self.screen_chip.set_no_show_all(True)
            self.pack_start(self.screen_chip, False, False, 0)
            outer = Gtk.Box()
            self.user_label = make_label("", "msg-user")
            self.user_label.set_hexpand(False)
            self.user_label.set_halign(Gtk.Align.END)
            self.user_label.set_max_width_chars(60)
            outer.pack_end(self.user_label, False, False, 0)
            self.pack_start(outer, False, False, 0)
        elif item.role == "assistant":
            self.markdown = MarkdownView()
            self.pack_start(self.markdown, False, False, 0)
            self.images_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            self.pack_start(self.images_box, False, False, 0)
            self.copy_row = Gtk.Box(spacing=8)
            self.copy_row.set_no_show_all(True)
            self.copy_button = Gtk.Button(label="Copy", can_focus=False)
            self.copy_button.get_style_context().add_class("chip-flat")
            self.copy_button.connect("clicked", self._copy)
            self.copy_button.show()
            self.copy_row.pack_start(self.copy_button, False, False, 0)
            self.pack_start(self.copy_row, False, False, 0)
        else:
            text = make_label("", "msg-error" if item.role == "error" else "dim")
            text.set_text(item.text)
            self.pack_start(text, False, False, 0)
        self.update(item)
        self.show_all()

    def _copy(self, _button: Gtk.Button) -> None:
        copy_text(self._text)
        self.copy_button.set_label("Copied")
        GLib.timeout_add(1400, lambda: (self.copy_button.set_label("Copy"), GLib.SOURCE_REMOVE)[1])

    def update(self, item: ChatItem) -> None:
        self._text = item.text
        if self.user_label is not None:
            self.user_label.set_text(item.text)
            screen = item.screen
            if screen is None:
                self.screen_chip.hide()
            else:
                text = {
                    "capturing": "👁 Reading your screen…",
                    "attached": f"👁 Shared {screen.app}" if screen.app else "👁 Shared your screen",
                    "unavailable": f"Sent without screen · {screen.reason or ''}",
                }[screen.state]
                self.screen_chip.set_text(text)
                self.screen_chip.show()
        if self.markdown is not None:
            self.markdown.update(item.text, item.is_streaming)
            self.copy_row.set_visible(not item.is_streaming and bool(item.text))
            self._update_images(item)

    def _update_images(self, item: ChatItem) -> None:
        session = self.app.session
        for attachment in item.images:
            if attachment.id in self._shown_images:
                continue
            pixbuf = session.images.get(attachment.id)
            if pixbuf is None:
                session.load_image(attachment)
                continue
            max_width = 460
            width, height = pixbuf.get_width(), pixbuf.get_height()
            if width > max_width:
                pixbuf_scaled = pixbuf.scale_simple(max_width, max(1, int(height * max_width / width)), GdkPixbuf.InterpType.BILINEAR)
            else:
                pixbuf_scaled = pixbuf
            image = Gtk.Image.new_from_pixbuf(pixbuf_scaled)
            image.set_halign(Gtk.Align.START)
            event = Gtk.EventBox()
            event.add(image)
            event.set_tooltip_text(attachment.alt or "Click to open · right-click to copy or save")
            event.connect("button-press-event", self._image_clicked, pixbuf)
            event.show_all()
            self.images_box.pack_start(event, False, False, 0)
            self._shown_images[attachment.id] = event

    def _image_clicked(self, _widget: Gtk.Widget, event: Gdk.EventButton, pixbuf: GdkPixbuf.Pixbuf) -> bool:
        if event.button == 3:
            menu = Gtk.Menu()
            copy_item = Gtk.MenuItem(label="Copy image")
            copy_item.connect("activate", lambda _w: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_image(pixbuf))
            save_item = Gtk.MenuItem(label="Save image…")
            save_item.connect("activate", lambda _w: self._save(pixbuf))
            menu.append(copy_item)
            menu.append(save_item)
            menu.show_all()
            self.app.panel.track_menu(menu)
            menu.popup_at_pointer(event)
        elif event.button == 1:
            self.app.open_pixbuf(pixbuf)
        return True

    def _save(self, pixbuf: GdkPixbuf.Pixbuf) -> None:
        dialog = Gtk.FileChooserNative.new("Save image", None, Gtk.FileChooserAction.SAVE, "Save", "Cancel")
        dialog.set_current_name("image.png")
        dialog.set_do_overwrite_confirmation(True)
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            pixbuf.savev(dialog.get_filename(), "png", [], [])


class Conversation(Gtk.Overlay):
    def __init__(self, app: Any) -> None:
        super().__init__()
        self.app = app
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_overlay_scrolling(True)
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.list.set_margin_end(6)
        self.empty = Gtk.Box(spacing=10)
        self.rows: dict = {}
        self.order: list = []
        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.progress_box.get_style_context().add_class("progress-card")
        self.progress_box.set_no_show_all(True)
        self.column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.column.pack_start(self.empty, False, False, 0)
        self.column.pack_start(self.list, False, False, 0)
        self.column.pack_start(self.progress_box, False, False, 0)
        self.scroller.add(self.column)
        self.add(self.scroller)
        self.jump = Gtk.Button(label="↓", can_focus=False, tooltip_text="Jump to latest")
        self.jump.get_style_context().add_class("round")
        self.jump.set_halign(Gtk.Align.CENTER)
        self.jump.set_valign(Gtk.Align.END)
        self.jump.set_no_show_all(True)
        self.jump.connect("clicked", lambda _b: self.scroll_to_bottom())
        self.add_overlay(self.jump)
        adjustment = self.scroller.get_vadjustment()
        adjustment.connect("value-changed", self._scrolled)
        adjustment.connect("changed", self._content_resized)
        self.at_bottom = True
        self._last_prepend_upper: Optional[float] = None

    def _scrolled(self, adjustment: Gtk.Adjustment) -> None:
        self.at_bottom = adjustment.get_value() + adjustment.get_page_size() >= adjustment.get_upper() - 24
        self.jump.set_visible(not self.at_bottom and adjustment.get_upper() > adjustment.get_page_size())
        if adjustment.get_value() < 60 and self.app.session.has_older_history and not self.app.session.is_loading_older and self.order:
            self._last_prepend_upper = adjustment.get_upper()
            self.app.session.load_older_history()

    def _content_resized(self, adjustment: Gtk.Adjustment) -> None:
        if self._last_prepend_upper is not None and adjustment.get_upper() != self._last_prepend_upper and not self.app.session.is_loading_older:
            # Keep the message you were reading in place after older ones are added above it.
            adjustment.set_value(adjustment.get_value() + adjustment.get_upper() - self._last_prepend_upper)
            self._last_prepend_upper = None
        elif self.at_bottom:
            GLib.idle_add(self.scroll_to_bottom)

    def scroll_to_bottom(self) -> bool:
        adjustment = self.scroller.get_vadjustment()
        adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
        self.at_bottom = True
        self.jump.hide()
        return GLib.SOURCE_REMOVE

    def refresh(self) -> None:
        session = self.app.session
        items = [item for item in session.items if item.text or item.images or item.role != "assistant" or item.is_streaming]
        wanted = [item.id for item in items]
        had_new_user = bool(items) and items[-1].role == "user" and items[-1].id not in self.rows
        for gone in set(self.rows) - set(wanted):
            row = self.rows.pop(gone)
            self.list.remove(row)
            row.destroy()
        for position, item in enumerate(items):
            row = self.rows.get(item.id)
            if row is None:
                row = ChatRow(item, self.app)
                self.rows[item.id] = row
                self.list.pack_start(row, False, False, 0)
                self.list.reorder_child(row, position)
            else:
                row.update(item)
        self.order = wanted
        if had_new_user:  # Your own new message always brings you down to it.
            self.at_bottom = True
        self._refresh_empty(not items)
        self._refresh_progress()

    def _refresh_empty(self, is_empty: bool) -> None:
        settings = self.app.settings
        clear(self.empty)
        if not is_empty:
            return
        emoji = Gtk.Label()
        emoji.set_markup(f'<span size="22000">{GLib.markup_escape_text(settings.resolved_emoji)}</span>')
        self.empty.pack_start(emoji, False, False, 0)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        column.pack_start(label(f"What can {settings.resolved_name} do for you?"), False, False, 0)
        hint = f"Type below, or hold {describe_accelerator(settings.get('push_to_talk'))} and speak." if settings.get("push_to_talk_enabled") else "Type a message below."
        column.pack_start(label(hint, "dim"), False, False, 0)
        self.empty.pack_start(column, False, False, 0)
        self.empty.show_all()

    def _refresh_progress(self) -> None:
        session = self.app.session
        activity = session.activity
        last = session.items[-1] if session.items else None
        pending = activity.kind == "thinking" and (last is None or last.role != "assistant" or not last.text)
        compact = activity.kind == "streaming" and session.progress.active_step is not None
        clear(self.progress_box)
        if not pending and not compact:
            self.progress_box.hide()
            return
        progress = session.progress
        head = Gtk.Box(spacing=7)
        head.pack_start(ThinkingDots(), False, False, 0)
        head.pack_start(label(progress.headline or activity.phase or "Thinking…", "dim", ellipsize=True), True, True, 0)
        if progress.started_at:
            head.pack_end(label(f"{int(time.time() - progress.started_at)}s", "faint"), False, False, 0)
        self.progress_box.pack_start(head, False, False, 0)
        if pending:
            for plan in progress.plan[:6]:
                mark = {"completed": "✓", "in_progress": "◉"}.get(plan.status, "○")
                self.progress_box.pack_start(label(f"{mark} {plan.text}", "dim", ellipsize=True), False, False, 0)
            for step in progress.steps[-5:]:
                mark = "⚠" if step.is_error else "◌" if step.is_active else "✓"
                self.progress_box.pack_start(label(f"{mark} {step.title}", "faint", ellipsize=True), False, False, 0)
        self.progress_box.show_all()


class Composer(Gtk.Box):
    def __init__(self, app: Any) -> None:
        super().__init__(spacing=6)
        self.app = app
        self.get_style_context().add_class("composer")
        self.buffer = Gtk.TextBuffer()
        self.view = Gtk.TextView(buffer=self.buffer, wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
        self.view.set_top_margin(4)
        self.view.set_bottom_margin(4)
        self.view.connect("key-press-event", self._key)
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_propagate_natural_height(True)
        self.scroll.set_max_content_height(110)
        self.scroll.add(self.view)
        overlay = Gtk.Overlay()
        overlay.add(self.scroll)
        self.placeholder = label("", "faint")
        self.placeholder.set_halign(Gtk.Align.START)
        self.placeholder.set_valign(Gtk.Align.CENTER)
        self.placeholder.set_can_target(False) if hasattr(self.placeholder, "set_can_target") else None
        overlay.add_overlay(self.placeholder)
        overlay.set_overlay_pass_through(self.placeholder, True)
        self.pack_start(overlay, True, True, 0)
        # None: decide from the message (auto). True/False: the user overrode it.
        self.share_override: Optional[bool] = None
        self.eye = icon_button("👁", "round", "", self._cycle_eye)
        self.eye.set_valign(Gtk.Align.END)
        self.pack_start(self.eye, False, False, 0)
        self.send = Gtk.Button(label="↑", can_focus=False, tooltip_text="Send (Enter)")
        self.send.get_style_context().add_class("send")
        self.send.set_valign(Gtk.Align.END)
        self.send.connect("clicked", lambda _b: self._submit())
        self.pack_start(self.send, False, False, 0)
        self.buffer.connect("changed", lambda _b: self.refresh())

    def text(self) -> str:
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), False)

    def set_text(self, text: str) -> None:
        self.buffer.set_text(text)
        self.buffer.place_cursor(self.buffer.get_end_iter())

    def focus(self) -> None:
        self.view.grab_focus()

    def _cycle_eye(self) -> None:
        self.share_override = {None: True, True: False, False: None}[self.share_override]
        self.refresh()

    def _key(self, _view: Gtk.TextView, event: Gdk.EventKey) -> bool:
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if event.state & (Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SHIFT_MASK):
                self.buffer.insert_at_cursor("\n")
                return True
            self._submit()
            return True
        if event.keyval == Gdk.KEY_Escape:
            if self.text():
                self.set_text("")
            else:
                self.app.panel.show_home()
            return True
        return False

    def _submit(self) -> None:
        session = self.app.session
        text = self.text().strip()
        if not text:
            if session.activity.is_busy:
                session.abort()
            return
        share = self.app.should_share_screen(text, self.share_override)
        session.send(text, share_screen=share)
        self.set_text("")
        self.share_override = None
        self.refresh()

    def refresh(self) -> None:
        app = self.app
        settings = app.settings
        has_text = bool(self.text().strip())
        self.placeholder.set_visible(not has_text)
        self.placeholder.set_text(f"Message {settings.resolved_name}…  (Enter sends · Alt+Enter new line)")
        busy = app.session.activity.is_busy
        self.send.set_label("■" if busy and not has_text else "↑")
        context = self.send.get_style_context()
        context.add_class("stop") if busy and not has_text else context.remove_class("stop")
        self.eye.set_visible(settings.get("visual_intelligence"))
        eye_context = self.eye.get_style_context()
        sharing = app.should_share_screen(self.text(), self.share_override) if has_text else self.share_override is True
        eye_context.add_class("active") if sharing else eye_context.remove_class("active")
        self.eye.set_tooltip_text({
            None: "Share your screen: automatic (when your message refers to it)",
            True: "Share your screen with this message",
            False: "Don't share your screen with this message",
        }[self.share_override])


class Panel:
    """Owns the window, its placement under the bar, and the two pages."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.page = "home"
        self.pinned = False
        self.chat_pending = False
        self.anchor: tuple = (0, 0)
        self._menus_open = 0
        self._suspended = False
        self._last_focus_lost = 0.0
        self._geometry_source = 0
        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL, title="BarClaw")
        self.window.set_decorated(False)
        self.window.set_resizable(False)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.set_keep_above(True)
        self.window.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.window.set_wmclass("barclaw-panel", "BarClaw")
        self.window.get_style_context().add_class("barclaw-window")
        self.window.set_app_paintable(True)
        screen = self.window.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.window.set_visual(visual)
        else:
            self.window.get_style_context().add_class("barclaw-opaque")
        self.layer_shell = session_type() == "wayland" and GtkLayerShell is not None and GtkLayerShell.is_supported()
        if self.layer_shell:
            GtkLayerShell.init_for_window(self.window)
            GtkLayerShell.set_namespace(self.window, "barclaw")
            GtkLayerShell.set_layer(self.window, GtkLayerShell.Layer.TOP)
            GtkLayerShell.set_keyboard_mode(self.window, GtkLayerShell.KeyboardMode.ON_DEMAND)
            GtkLayerShell.set_anchor(self.window, GtkLayerShell.Edge.TOP, True)
        self.window.connect("delete-event", lambda *_: (self.collapse(), True)[1])
        self.window.connect("focus-out-event", self._focus_out)
        self.window.connect("key-press-event", self._key)
        self.window.connect("size-allocate", lambda *_: self._schedule_placement())

        # GtkOverlay paints no CSS background, so the visible surface is a box around it.
        backdrop = Gtk.Box()
        backdrop.get_style_context().add_class("panel-surface")
        self.window.add(backdrop)
        self.surface = Gtk.Overlay()
        backdrop.pack_start(self.surface, True, True, 0)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        self.stack.set_vhomogeneous(False)
        self.stack.set_hhomogeneous(False)
        self.stack.set_interpolate_size(False)
        self.surface.add(self.stack)
        self.toast = label("", "toast", xalign=0.5)
        self.toast.set_halign(Gtk.Align.CENTER)
        self.toast.set_valign(Gtk.Align.END)
        self.toast.set_margin_bottom(10)
        self.toast.set_no_show_all(True)
        self.surface.add_overlay(self.toast)
        self.surface.set_overlay_pass_through(self.toast, True)
        self._build_home()
        self._build_chat()
        self.stack.add_named(self.home, "home")
        self.stack.add_named(self.chat, "chat")
        backdrop.show_all()
        self.stack.set_visible_child_name("home")

    # MARK: Building

    def _header(self, shows_back: bool) -> tuple:
        row = Gtk.Box(spacing=8)
        row.set_size_request(-1, 26)
        if shows_back:
            row.pack_start(icon_button("‹", "round", "Back", self.show_home), False, False, 0)
        name = label("", "agent-title")
        row.pack_start(name, False, False, 0)
        tag = Gtk.Button(can_focus=False, tooltip_text="Model — click to switch")
        tag.get_style_context().add_class("model-tag")
        content = Gtk.Box(spacing=5)
        dot = Gtk.DrawingArea()
        dot.set_size_request(7, 7)
        dot.set_valign(Gtk.Align.CENTER)
        dot.color = (0.5, 0.5, 0.5)
        dot.connect("draw", self._draw_dot)
        content.pack_start(dot, False, False, 0)
        tag_label = label("", ellipsize=True)
        tag_label.set_max_width_chars(26)
        content.pack_start(tag_label, False, False, 0)
        tag.add(content)
        tag.connect("clicked", lambda _b: self._show_model_menu(tag))
        row.pack_start(tag, False, False, 0)
        usage_button = Gtk.Button(can_focus=False)
        usage_button.get_style_context().add_class("chip-flat")
        usage_content = Gtk.Box(spacing=6)
        usage_label = label("", "faint")
        usage_bar = UsageBar()
        usage_content.pack_start(usage_label, False, False, 0)
        usage_content.pack_start(usage_bar, False, False, 0)
        usage_button.add(usage_content)
        usage_button.connect("clicked", lambda _b: self._show_usage_menu(usage_button))
        usage_button.set_no_show_all(True)
        usage_content.show_all()
        row.pack_end(usage_button, False, False, 0)
        return row, name, tag_label, dot, usage_button, usage_label, usage_bar

    @staticmethod
    def _draw_dot(area: Gtk.DrawingArea, c: cairo.Context) -> bool:
        c.set_source_rgb(*area.color)
        c.arc(3.5, 3.5, 3.2, 0, 6.3)
        c.fill()
        return False

    def _build_home(self) -> None:
        self.home = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.home.set_size_request(HOME_WIDTH, -1)
        self.home.set_border_width(16)
        self.home_header = self._header(False)
        self.home.pack_start(self.home_header[0], False, False, 0)
        self.home_banner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.home.pack_start(self.home_banner, False, False, 0)
        self.home_approvals = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.home.pack_start(self.home_approvals, False, False, 0)

        center = Gtk.Box(spacing=22)
        center.set_halign(Gtk.Align.CENTER)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_valign(Gtk.Align.CENTER)
        session = self.app.session
        self.action_model = PillAction("◈", "Model", lambda: self._show_model_menu(self.action_model), menu=True)
        self.action_effort = PillAction("🧠", "Effort", lambda: self._show_effort_menu(self.action_effort), menu=True)
        self.context_ring = ContextRing()
        self.context_ring.set_no_show_all(True)
        self.context_ring.set_valign(Gtk.Align.CENTER)
        self.action_compact = PillAction("⇲", "Compact", session.compact, trailing=self.context_ring)
        for action in (self.action_model, self.action_effort, self.action_compact):
            left.pack_start(action, False, False, 0)
        center.pack_start(left, False, False, 0)
        self.stage = AgentStage(self.app.toggle_listening)
        center.pack_start(self.stage, False, False, 0)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_valign(Gtk.Align.CENTER)
        self.action_chat = PillAction("💬", "Chat", lambda: self.show_chat(True), prominent=True)
        self.action_session = PillAction("🗂", "Main", lambda: self._show_session_menu(self.action_session), menu=True)
        self.action_settings = PillAction("⚙", "Settings", self.app.open_settings)
        for action in (self.action_chat, self.action_session, self.action_settings):
            right.pack_start(action, False, False, 0)
        center.pack_start(right, False, False, 0)
        self.home.pack_start(center, False, False, 0)

        self.speech = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.speech.set_halign(Gtk.Align.CENTER)
        self.speech.set_size_request(-1, 34)
        self.bubble = Gtk.Box(spacing=8)
        self.bubble.get_style_context().add_class("bubble")
        self.bubble_wave = Waveform()
        self.bubble_wave.set_no_show_all(True)
        self.bubble.pack_start(self.bubble_wave, False, False, 0)
        self.bubble_dots = ThinkingDots()
        self.bubble_dots.set_no_show_all(True)
        self.bubble.pack_start(self.bubble_dots, False, False, 0)
        self.bubble_label = Gtk.Label(wrap=True, max_width_chars=56, xalign=0, lines=3)
        self.bubble_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.bubble.pack_start(self.bubble_label, False, False, 0)
        self.bubble.set_no_show_all(True)
        self.bubble_wave.get_parent()
        self.hint = label("", "faint", xalign=0.5)
        self.hint.set_no_show_all(True)
        self.work_line = label("", "dim", xalign=0.5, ellipsize=True)
        self.work_line.set_max_width_chars(70)
        self.work_line.set_no_show_all(True)
        self.speech.pack_start(self.bubble, False, False, 0)
        self.speech.pack_start(self.hint, False, False, 0)
        self.speech.pack_start(self.work_line, False, False, 0)
        self.home.pack_start(self.speech, False, False, 0)

    def _build_chat(self) -> None:
        self.chat = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.chat.set_border_width(14)
        self.chat_header = self._header(True)
        self.pin = icon_button("📌", "round", "Keep open", self.toggle_pin)
        self.chat_header[0].pack_end(self.pin, False, False, 0)
        self.chat.pack_start(self.chat_header[0], False, False, 0)
        self.chat_banner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.chat.pack_start(self.chat_banner, False, False, 0)
        self.chat_approvals = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.chat.pack_start(self.chat_approvals, False, False, 0)
        self.listening_card = Gtk.Box(spacing=12)
        self.listening_card.get_style_context().add_class("banner")
        self.listening_wave = Waveform(5)
        self.listening_wave.set_no_show_all(True)
        self.listening_card.pack_start(self.listening_wave, False, False, 0)
        self.listening_text = make_label("", selectable=False)
        self.listening_card.pack_start(self.listening_text, True, True, 0)
        self.listening_card.set_no_show_all(True)
        self.listening_text.show()
        self.chat.pack_start(self.listening_card, False, False, 0)
        self.conversation = Conversation(self.app)
        self.conversation.set_vexpand(True)
        self.chat.pack_start(self.conversation, True, True, 0)
        self.composer = Composer(self.app)
        self.chat.pack_start(self.composer, False, False, 0)

    # MARK: Showing and hiding

    @property
    def is_expanded(self) -> bool:
        return self.window.get_visible()

    def toggle(self, x: int = 0, y: int = 0) -> None:
        # A click on the icon first steals focus, so the panel has just hidden itself; don't reopen it.
        if self.is_expanded or time.monotonic() - self._last_focus_lost < 0.25:
            self.collapse()
        else:
            self.expand(x, y)

    def expand(self, x: int = 0, y: int = 0, page: Optional[str] = None) -> None:
        if x or y:
            self.anchor = (x, y)
        elif not self.is_expanded:
            self.anchor = self._pointer()
        if page:
            self.page = page
        elif self.chat_pending:
            self.page = "chat"
        self.chat_pending = False
        self.app.session.clear_attention()
        self.refresh()
        self._apply_page()
        self._place()
        self.window.present_with_time(Gtk.get_current_event_time() or Gdk.CURRENT_TIME)
        if self.page == "chat":
            self.composer.focus()
        self.app.panel_visibility_changed()

    def collapse(self) -> None:
        if self.window.get_visible():
            self.window.hide()
            self.app.panel_visibility_changed()

    def show_home(self) -> None:
        self.page = "home"
        self._apply_page()

    def show_chat(self, focus_composer: bool) -> None:
        self.page = "chat"
        self._apply_page()
        if focus_composer:
            self.composer.focus()

    def toggle_pin(self) -> None:
        self.pinned = not self.pinned
        context = self.pin.get_style_context()
        context.add_class("active") if self.pinned else context.remove_class("active")
        self.pin.set_tooltip_text("Pinned — click to unpin" if self.pinned else "Keep open")

    def _apply_page(self) -> None:
        self.stack.set_visible_child_name(self.page)
        self._apply_chat_size()
        self._schedule_placement()

    def max_size(self) -> tuple:
        work = self._work_area()
        settings = self.app.settings
        return max(420, int(work.width * settings.get("max_width_fraction"))), max(320, int(work.height * settings.get("max_height_fraction")))

    def _apply_chat_size(self) -> None:
        max_w, max_h = self.max_size()
        wide = wants_wide_layout(self.app.session.latest_assistant_text)
        width = max_w if wide else min(CHAT_WIDTH, max_w)
        self.chat.set_size_request(width, max_h if self.app.session.items else min(max_h, 300))

    # MARK: Placement

    def _pointer(self) -> tuple:
        seat = Gdk.Display.get_default().get_default_seat()
        pointer = seat.get_pointer() if seat else None
        if pointer is None:
            return (0, 0)
        _screen, x, y = pointer.get_position()
        return (x, y)

    def _monitor(self) -> Gdk.Monitor:
        display = Gdk.Display.get_default()
        return display.get_monitor_at_point(*self.anchor) or display.get_monitor(0)

    def _work_area(self) -> Gdk.Rectangle:
        return self._monitor().get_workarea()

    def placement(self, width: int, height: int) -> tuple:
        """Top-left corner: under (or above) the icon, kept inside the monitor's work area."""
        area = self._work_area()
        x, y = self.anchor
        px = max(area.x + BAR_GAP, min(x - width // 2, area.x + area.width - width - BAR_GAP))
        if y <= area.y + area.height / 2:
            py = max(area.y + BAR_GAP, y + BAR_GAP)
        else:
            py = max(area.y + BAR_GAP, min(y - height - BAR_GAP, area.y + area.height - height - BAR_GAP))
        return px, py

    def _schedule_placement(self) -> None:
        if not self._geometry_source and self.window.get_visible():
            self._geometry_source = GLib.idle_add(self._place_idle)

    def _place_idle(self) -> bool:
        self._geometry_source = 0
        self._place()
        return GLib.SOURCE_REMOVE

    def _place(self) -> None:
        natural = self.window.get_preferred_size()[1]
        px, py = self.placement(natural.width, natural.height)
        if self.layer_shell:
            area = self._monitor().get_geometry()
            on_right = self.anchor[0] > area.x + area.width / 2
            GtkLayerShell.set_anchor(self.window, GtkLayerShell.Edge.RIGHT, on_right)
            GtkLayerShell.set_anchor(self.window, GtkLayerShell.Edge.LEFT, not on_right)
            margin = area.x + area.width - px - natural.width if on_right else px - area.x
            GtkLayerShell.set_margin(self.window, GtkLayerShell.Edge.RIGHT if on_right else GtkLayerShell.Edge.LEFT, max(0, margin))
            GtkLayerShell.set_margin(self.window, GtkLayerShell.Edge.TOP, max(0, py - area.y))
            return
        self.window.move(px, py)

    # MARK: Focus

    def track_menu(self, menu: Gtk.Menu) -> None:
        self._menus_open += 1

        def closed(_menu: Gtk.Menu) -> None:
            self._menus_open = max(0, self._menus_open - 1)
            GLib.idle_add(self.window.present) if self.window.get_visible() else None

        menu.connect("deactivate", closed)

    def suspend_autohide(self, value: bool) -> None:
        self._suspended = value

    def _focus_out(self, *_args: Any) -> bool:
        if self.pinned or self._menus_open or self._suspended or self.app.voice.is_active:
            return False

        def check() -> bool:
            if self.window.is_active() or self.pinned or self._menus_open or self._suspended:
                return GLib.SOURCE_REMOVE
            if self.window.get_visible():
                self._last_focus_lost = time.monotonic()
                self.collapse()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(80, check)
        return False

    def _key(self, _window: Gtk.Window, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_comma and event.state & Gdk.ModifierType.CONTROL_MASK:
            self.app.open_settings()
            return True
        if event.keyval == Gdk.KEY_Escape:
            if self.page == "chat":
                self.show_home()
            else:
                self.collapse()
            return True
        return False

    # MARK: Menus

    def _popup(self, entries: list, anchor: Gtk.Widget) -> None:
        menu = Gtk.Menu()
        group = None
        for entry in entries:
            if entry is None:
                menu.append(Gtk.SeparatorMenuItem())
                continue
            text, callback, checked = entry[0], entry[1], entry[2] if len(entry) > 2 else None
            if callback is None and checked is None:
                item = Gtk.MenuItem(label=text, sensitive=False)
            elif checked is None:
                item = Gtk.MenuItem(label=text)
            else:
                item = Gtk.CheckMenuItem(label=text, active=checked, draw_as_radio=True)
            if callback is not None:
                item.connect("activate", lambda _w, cb=callback: cb())
            menu.append(item)
        _ = group
        menu.show_all()
        self.track_menu(menu)
        menu.attach_to_widget(anchor, None)
        menu.popup_at_widget(anchor, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)

    def _show_model_menu(self, anchor: Gtk.Widget) -> None:
        session = self.app.session
        current = session.session_settings.model_ref
        entries: list = [("Default model", lambda: session.set_model(None), current is None)]
        providers: dict = {}
        for model in session.models:
            providers.setdefault(model.provider, []).append(model)
        for provider in sorted(providers):
            entries += [None, (provider, None)]
            entries += [(m.name, (lambda ref=m.id: session.set_model(ref)), m.id == current) for m in providers[provider]]
        if not session.models:
            entries.append(("Loading models…" if self.app.gateway.state.is_connected else "Connect to choose a model", None))
            session.load_models()
        self._popup(entries, anchor)

    def _show_effort_menu(self, anchor: Gtk.Widget) -> None:
        session = self.app.session
        settings = session.session_settings
        entries: list = [("Default effort", lambda: session.set_thinking(None), settings.thinking_level is None), None]
        entries += [(level.title(), (lambda l=level: session.set_thinking(l)), level == settings.thinking_level) for level in settings.thinking_options]
        self._popup(entries, anchor)

    def _show_session_menu(self, anchor: Gtk.Widget) -> None:
        session = self.app.session
        title = session.current_session_title
        entries: list = [("New session", session.create_session)]
        if self.app.gateway.has_admin:
            entries.append((f"Reset “{title}”", session.reset_session))
        else:
            entries.append((f"Reset “{title}” (needs admin)…", self.app.open_admin_settings))
        entries += [None, ("Sessions", None)]
        if not session.sessions:
            entries.append((title, lambda: None, True))
        from ..agent.session import matches_session

        for row in session.sessions:
            text = row.title if len(row.title) <= 40 else row.title[:39] + "…"
            entries.append((text, (lambda key=row.key: session.switch_session(key)), matches_session(row.key, session.session_key)))
        session.load_sessions()
        self._popup(entries, anchor)

    def _show_usage_menu(self, anchor: Gtk.Widget) -> None:
        usage = self.app.session.usage
        if usage is None:
            return
        settings = self.app.settings
        entries: list = [(f"{usage.provider_name} usage", None)]
        for choice, (title, _short) in USAGE_WINDOWS.items():
            window = usage.window(choice)
            text = f"{title} — {int(window.used_percent)}% used" if window else f"{title} — unavailable"
            entries.append((text, (lambda c=choice: settings.set("usage_window", c)) if window else None, settings.get("usage_window") == choice))
        self._popup(entries, anchor)

    # MARK: Refresh

    def refresh(self) -> None:
        for header in (self.home_header, self.chat_header):
            self._refresh_header(header)
        self._refresh_banner(self.home_banner)
        self._refresh_banner(self.chat_banner)
        self._refresh_approvals(self.home_approvals)
        self._refresh_approvals(self.chat_approvals)
        self._refresh_home()
        self._refresh_chat()
        self._refresh_toast()

    def _status_label(self) -> tuple:
        app = self.app
        state = app.gateway.state
        activity = app.session.activity
        if state.kind == "connected":
            text = {
                "thinking": "Working…", "streaming": "Replying…", "compacting": "Compacting…",
            }.get(activity.kind) or (short_model_name(app.session.session_settings.model_ref) if app.session.session_settings.model_ref else "Default model")
            return text, (0.3, 0.9, 0.55)
        return {
            "connecting": ("Connecting…", (1.0, 0.65, 0.2)),
            "pairing": ("Awaiting approval", (1.0, 0.65, 0.2)),
            "offline": ("Offline", (1.0, 0.4, 0.4)),
            "auth_failed": ("Sign-in failed", (1.0, 0.4, 0.4)),
            "idle": ("Not set up", (0.5, 0.5, 0.5)),
        }[state.kind]

    def _refresh_header(self, header: tuple) -> None:
        _row, name, tag_label, dot, usage_button, usage_label, usage_bar = header
        name.set_text(self.app.settings.resolved_name)
        text, color = self._status_label()
        tag_label.set_text(text)
        dot.color = color
        dot.queue_draw()
        settings = self.app.settings
        usage = self.app.session.usage
        window = usage.best_window(settings.get("usage_window")) if usage else None
        if settings.get("show_usage") and window is not None:
            usage_label.set_text(f"{int(window.used_percent)}%")
            usage_bar.fraction = window.used_percent / 100
            usage_bar.queue_draw()
            usage_button.set_tooltip_text(f"{usage.provider_name}: {window.label} window, {int(window.used_percent)}% used — click to change")
            usage_button.show()
        else:
            usage_button.hide()

    def _refresh_banner(self, box: Gtk.Box) -> None:
        app = self.app
        state = app.gateway.state
        key = (state, app.gateway.network_available)
        if getattr(box, "_key", None) == key:
            return
        box._key = key
        clear(box)
        host = app.gateway.host
        spec = None
        if state.kind == "connecting":
            spec = ("📡", "Reconnecting…" if state.attempt else "Connecting…", host, None)
        elif state.kind == "offline":
            available = app.gateway.network_available
            spec = ("⚠", "Disconnected" if available else "No network",
                    f"{state.message} · retrying automatically" if available else "Will reconnect when you're back online",
                    ("Retry", lambda: app.gateway.reconnect_now("user")))
        elif state.kind == "pairing":
            spec = ("🔑", "Approve this computer on your gateway", "Run this on the gateway host, then BarClaw connects automatically.", None)
        elif state.kind == "auth_failed":
            spec = ("🔒", "Couldn't sign in", state.message, ("Settings…", app.open_settings))
        elif state.kind == "idle":
            spec = ("🔗", "Connect to your OpenClaw gateway", "Add the gateway URL and token in Settings.", ("Set Up…", app.open_settings))
        if spec is None:
            box.hide()
            return
        row = Gtk.Box(spacing=10)
        row.get_style_context().add_class("banner")
        row.pack_start(label(spec[0]), False, False, 0)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        column.pack_start(label(spec[1], "agent-title"), False, False, 0)
        if spec[2]:
            subtitle = label(spec[2], "dim")
            subtitle.set_line_wrap(True)
            column.pack_start(subtitle, False, False, 0)
        row.pack_start(column, True, True, 0)
        if spec[3]:
            button = chip(*spec[3])
            button.set_valign(Gtk.Align.CENTER)
            row.pack_end(button, False, False, 0)
        box.pack_start(row, False, False, 0)
        if state.kind == "pairing":
            command = f"openclaw devices approve {state.request_id or '--latest'}"
            code_row = Gtk.Box(spacing=8)
            text = make_label("", "command")
            text.set_text(command)
            code_row.pack_start(text, True, True, 0)
            code_row.pack_end(chip("Copy", lambda: copy_text(command)), False, False, 0)
            box.pack_start(code_row, False, False, 0)
        box.show_all()

    def _refresh_approvals(self, box: Gtk.Box) -> None:
        approvals = self.app.session.approvals
        key = [a.id for a in approvals]
        if getattr(box, "_key", None) == key:
            return
        box._key = key
        clear(box)
        for approval in approvals:
            box.pack_start(self._approval_card(approval), False, False, 0)
        box.show_all()

    def _approval_card(self, approval: PendingApproval) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class("card")
        card.pack_start(label("✋ Approval needed", "agent-title"), False, False, 0)
        title = make_label("", "command")
        title.set_text(approval.title)
        card.pack_start(title, False, False, 0)
        if approval.detail:
            card.pack_start(label(approval.detail, "dim"), False, False, 0)
        row = Gtk.Box(spacing=6)
        for decision in approval.decisions:
            row.pack_start(chip(approval_title(decision), lambda d=decision: self.app.session.resolve_approval(approval, d)), False, False, 0)
        card.pack_start(row, False, False, 0)
        return card

    def _refresh_home(self) -> None:
        app = self.app
        session, voice, settings = app.session, app.voice, app.settings
        session_settings = session.session_settings
        self.action_model.title.set_text(short_model_name(session_settings.model_ref) if session_settings.model_ref else "Model")
        self.action_effort.title.set_text((session_settings.thinking_level or "Effort").title())
        fraction = session_settings.context_fraction
        if fraction is not None:
            self.context_ring.fraction = fraction
            self.context_ring.show()
            self.context_ring.queue_draw()
            self.action_compact.set_tooltip_text(f"Compact context ({int(fraction * 100)}% used)")
        else:
            self.context_ring.hide()
            self.action_compact.set_tooltip_text("Compact context")
        self.action_compact.set_sensitive(not session.activity.is_busy)
        replies = sum(1 for item in session.items if item.role == "assistant")
        self.action_chat.badge.set_text(str(replies))
        self.action_chat.badge.set_visible(replies > 0)
        self.action_session.title.set_text(session.current_session_title)

        stage = self.stage
        stage.emoji = settings.resolved_emoji
        stage.connected = app.gateway.state.is_connected
        stage.level = voice.level
        stage.mode = "listening" if voice.is_active else "thinking" if session.activity.is_busy else "idle"
        self.stage.queue_draw()
        shortcut = describe_accelerator(settings.get("push_to_talk"))
        stage.set_tooltip_text(f"Click or hold {shortcut} to talk" if settings.get("push_to_talk_enabled") else "Click to talk")

        phase = voice.phase
        text, placeholder = None, False
        if phase == "listening":
            text, placeholder = (voice.transcript, False) if voice.transcript else ("Listening…", True)
        elif phase == "finishing":
            text, placeholder = (voice.transcript, False) if voice.transcript else ("…", True)
        elif phase == "failed":
            text, placeholder = voice.message, True
        elif app.spoken_text:
            text = app.spoken_text
        self.bubble.set_visible(text is not None)
        if text is not None:
            self.bubble_label.show()
            self.bubble_label.set_text(text)
            self.bubble_label.set_opacity(0.6 if placeholder else 1)
            context = self.bubble.get_style_context()
            context.add_class("listening") if phase == "listening" else context.remove_class("listening")
            self.bubble_wave.set_visible(phase == "listening")
            self.bubble_wave.level = voice.level
            self.bubble_dots.set_visible(phase != "listening" and session.activity.is_busy)
        idle_hint = text is None and not session.activity.is_busy
        self.hint.set_visible(idle_hint)
        if idle_hint:
            name = settings.resolved_name
            self.hint.set_text(f"Hold {shortcut} or click {name} to talk" if settings.get("push_to_talk_enabled") else f"Click {name} to talk")
        headline = session.progress.headline if session.activity.is_busy and phase == "idle" else None
        self.work_line.set_visible(headline is not None)
        if headline is not None:
            elapsed = f"   {int(time.time() - session.progress.started_at)}s" if session.progress.started_at else ""
            self.work_line.set_text(headline + elapsed)

    def _refresh_chat(self) -> None:
        voice = self.app.voice
        self.conversation.refresh()
        self.composer.refresh()
        if voice.phase == "idle":
            self.listening_card.hide()
        else:
            self.listening_card.show()
            if voice.phase == "failed":
                self.listening_wave.hide()
                self.listening_text.set_text(voice.message)
            else:
                self.listening_wave.show()
                self.listening_wave.level = voice.level
                self.listening_text.set_text(voice.transcript or ("Finishing…" if voice.phase == "finishing" else "Listening…"))
        if self.page == "chat":
            self._apply_chat_size()

    def _refresh_toast(self) -> None:
        toast = self.app.session.toast
        self.toast.set_visible(toast is not None)
        if toast is not None:
            self.toast.set_text(toast)

    @staticmethod
    def should_auto_share(text: str) -> bool:
        return refers_to_screen(text)
