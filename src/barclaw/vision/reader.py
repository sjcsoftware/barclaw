"""Captures the screen and reads it locally with Tesseract (CPU only).

X11 uses GDK directly. Wayland has no general capture API for ordinary clients, so it goes through
`grim` (wlroots), `spectacle` (KDE), or the XDG Screenshot portal, in that order.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlsplit

from gi.repository import Gdk, GdkPixbuf, Gio, GLib

from ..support import Future, in_thread, log, sleep, spawn
from .snapshot import ScreenReadError, ScreenSnapshot
from .windows import active_window, session_type

OCR_MAX_WIDTH = 2048


@dataclass
class CaptureOptions:
    screen_index: Optional[int] = None  # None means the monitor the user is working on.
    max_width: int = 1600
    quality: float = 0.75
    format: str = "jpeg"
    max_bytes: Optional[int] = None
    ocr_timeout: float = 5.0


def tesseract_languages() -> list:
    try:
        output = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in output.splitlines()[1:] if line.strip() and line.strip() != "osd"]


def run_ocr(png: bytes, timeout: float, languages: list) -> str:
    """OCR text top to bottom, or empty on timeout/absence: the image itself still goes out."""
    if not languages:
        return ""
    env = dict(os.environ, OMP_THREAD_LIMIT="2", CUDA_VISIBLE_DEVICES="")
    with tempfile.NamedTemporaryFile(suffix=".png") as image:
        image.write(png)
        image.flush()
        try:
            result = subprocess.run(
                ["tesseract", image.name, "stdout", "-l", "+".join(languages[:4]), "--psm", "3"],
                capture_output=True, text=True, timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            log.info("OCR hit its %.0fs deadline; sending the image without text", timeout)
            return ""
        except OSError as error:
            log.error("OCR failed: %s", error)
            return ""
    lines = [line.rstrip() for line in result.stdout.splitlines()]
    collapsed = []
    for line in lines:
        if line or (collapsed and collapsed[-1]):
            collapsed.append(line)
    return "\n".join(collapsed).strip()


def _scaled(pixbuf: GdkPixbuf.Pixbuf, max_width: int) -> GdkPixbuf.Pixbuf:
    width, height = pixbuf.get_width(), pixbuf.get_height()
    if width <= max_width:
        return pixbuf
    return pixbuf.scale_simple(max_width, max(1, int(height * max_width / width)), GdkPixbuf.InterpType.BILINEAR)


def encode(pixbuf: GdkPixbuf.Pixbuf, fmt: str, quality: float) -> bytes:
    if fmt == "png":
        ok, data = pixbuf.save_to_bufferv("png", [], [])
    else:
        ok, data = pixbuf.save_to_bufferv("jpeg", ["quality"], [str(int(quality * 100))])
    if not ok:
        raise ScreenReadError.failed("couldn't encode the screenshot")
    return bytes(data)


def fit_within(pixbuf: GdkPixbuf.Pixbuf, options: CaptureOptions) -> tuple:
    """Encodes at the requested size, stepping quality and then resolution down to fit `max_bytes`."""
    scaled = _scaled(pixbuf, options.max_width)
    if options.format == "png":
        data = encode(scaled, "png", 1.0)
        if options.max_bytes is None or len(data) <= options.max_bytes:
            return data, "png", scaled.get_width(), scaled.get_height()
    quality = options.quality
    for _ in range(8):
        data = encode(scaled, "jpeg", quality)
        if options.max_bytes is None or len(data) <= options.max_bytes:
            return data, "jpeg", scaled.get_width(), scaled.get_height()
        if quality > 0.5:
            quality -= 0.1
        else:
            scaled = _scaled(scaled, int(scaled.get_width() * 0.75))
    raise ScreenReadError.failed("the screenshot wouldn't fit the gateway's size limit")


class ScreenReader:
    def __init__(self) -> None:
        self.on_reading: Optional[Callable[[Optional[Gdk.Monitor]], None]] = None
        # Hides BarClaw's own windows for the instant of the grab, and restores them.
        self.hide_own_windows: Callable[[], Future] = self._no_windows
        self.restore_own_windows: Callable[[], None] = lambda: None
        self._languages: Optional[list] = None
        self._portal_token = 0
        self._window_before_panel: tuple = (None, None)

    @staticmethod
    def _no_windows() -> Future:
        future = Future()
        future.set_result(None)
        return future

    def remember_active_window(self) -> None:
        """Call just before the panel takes focus: that's the app the user means by "this"."""
        self._window_before_panel = active_window()

    def _user_window(self) -> tuple:
        current = active_window()
        if current[0] is None or current[0].lower().startswith("barclaw"):
            return self._window_before_panel
        return current

    def languages(self) -> list:
        if self._languages is None:
            self._languages = tesseract_languages()
        return self._languages

    def capture(self, options: Optional[CaptureOptions] = None) -> Future:
        return spawn(self._capture(options or CaptureOptions()))

    def _capture(self, options: CaptureOptions):
        display = Gdk.Display.get_default()
        if display is None or display.get_n_monitors() == 0:
            raise ScreenReadError.no_display()
        monitors = [display.get_monitor(i) for i in range(display.get_n_monitors())]
        index, monitor = self._choose_monitor(display, monitors, options.screen_index)

        yield self.hide_own_windows()
        window = self._user_window()
        try:
            if session_type() == "x11":
                full = self._grab_x11(monitor)
            else:
                full = yield spawn(self._grab_wayland(monitor))
        finally:
            self.restore_own_windows()

        # The glow starts after the grab, so it never appears in the picture.
        started = time.monotonic()
        if self.on_reading:
            self.on_reading(monitor)
        try:
            data, fmt, width, height = fit_within(full, options)
            ocr_source = _scaled(full, OCR_MAX_WIDTH)
            png = encode(ocr_source, "png", 1.0)
            languages = self.languages()
            text = yield in_thread(run_ocr, png, options.ocr_timeout, languages)
            # Long enough to notice, even when OCR is instant or absent.
            remaining = 0.7 - (time.monotonic() - started)
            if remaining > 0:
                yield sleep(remaining)
        finally:
            if self.on_reading:
                self.on_reading(None)
        return ScreenSnapshot(
            image_data=data, format=fmt, width=width, height=height,
            display_index=index, display_count=len(monitors),
            display_name=monitor.get_model() or f"Display {index + 1}",
            app_name=window[0], window_title=window[1], text=text, captured_at=time.time(),
        )

    @staticmethod
    def _choose_monitor(display: Gdk.Display, monitors: list, requested: Optional[int]) -> tuple:
        if requested is not None and 0 <= requested < len(monitors):
            return requested, monitors[requested]
        seat = display.get_default_seat()
        pointer = seat.get_pointer() if seat else None
        if pointer is not None:
            _screen, x, y = pointer.get_position()
            at_pointer = display.get_monitor_at_point(x, y)
            if at_pointer in monitors:
                return monitors.index(at_pointer), at_pointer
        return 0, monitors[0]

    @staticmethod
    def _grab_x11(monitor: Gdk.Monitor) -> GdkPixbuf.Pixbuf:
        root = Gdk.get_default_root_window()
        geometry = monitor.get_geometry()
        pixbuf = Gdk.pixbuf_get_from_window(root, geometry.x, geometry.y, geometry.width, geometry.height)
        if pixbuf is None:
            raise ScreenReadError.failed("the X server returned no image")
        return pixbuf

    def _grab_wayland(self, monitor: Gdk.Monitor):
        with tempfile.TemporaryDirectory(prefix="barclaw-") as directory:
            target = Path(directory) / "screen.png"
            if shutil.which("grim"):
                command = ["grim", str(target)]
                result = yield in_thread(subprocess.run, command, capture_output=True, timeout=10)
                if result.returncode == 0 and target.exists():
                    return self._load(target, monitor)
                log.error("grim failed: %s", result.stderr.decode(errors="replace").strip())
            if shutil.which("spectacle"):
                command = ["spectacle", "--background", "--nonotify", "--current", "--output", str(target)]
                result = yield in_thread(subprocess.run, command, capture_output=True, timeout=10)
                if result.returncode == 0 and target.exists():
                    return self._load(target, monitor)
            uri = yield self._portal_screenshot()
            path = Path(unquote(urlsplit(uri).path))
            try:
                return self._load(path, monitor)
            finally:
                # The portal saves into the user's Pictures folder; don't leave copies behind.
                try:
                    path.unlink()
                except OSError:
                    pass

    @staticmethod
    def _load(path: Path, monitor: Gdk.Monitor) -> GdkPixbuf.Pixbuf:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
        except GLib.Error as error:
            raise ScreenReadError.failed(error.message) from error
        geometry = monitor.get_geometry()
        scale = monitor.get_scale_factor()
        # Whole-desktop captures come back as one image; crop to the active monitor when that's the case.
        expected = (geometry.width * scale, geometry.height * scale)
        if (pixbuf.get_width(), pixbuf.get_height()) != expected and pixbuf.get_width() >= geometry.x * scale + expected[0]:
            pixbuf = pixbuf.new_subpixbuf(geometry.x * scale, geometry.y * scale, expected[0], min(expected[1], pixbuf.get_height() - geometry.y * scale))
        return pixbuf

    def _portal_screenshot(self) -> Future:
        """org.freedesktop.portal.Screenshot; asks the user the first time on most desktops."""
        future = Future()
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as error:
            future.set_error(ScreenReadError.failed(error.message))
            return future
        self._portal_token += 1
        token = f"barclaw{os.getpid()}_{self._portal_token}"
        sender = bus.get_unique_name()[1:].replace(".", "_")
        handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        subscription = 0

        def on_response(_bus, _sender, _path, _iface, _signal, params) -> None:
            nonlocal subscription
            code, results = params.unpack()
            bus.signal_unsubscribe(subscription)
            if code == 0 and results.get("uri"):
                future.set_result(results["uri"])
            elif code == 1:
                future.set_error(ScreenReadError.denied("The screenshot request was cancelled."))
            else:
                future.set_error(ScreenReadError.denied("The desktop refused the screenshot request."))

        subscription = bus.signal_subscribe(
            "org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request", "Response", handle, None,
            Gio.DBusSignalFlags.NONE, on_response,
        )

        def called(connection, result) -> None:
            try:
                connection.call_finish(result)
            except GLib.Error as error:
                bus.signal_unsubscribe(subscription)
                future.set_error(ScreenReadError.failed(f"no screenshot portal is available ({error.message})"))

        options = {"handle_token": GLib.Variant("s", token), "interactive": GLib.Variant("b", False)}
        bus.call(
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop", "org.freedesktop.portal.Screenshot",
            "Screenshot", GLib.Variant("(sa{sv})", ("", options)), GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 15000, None, called,
        )
        return future

    def test_capture(self) -> Future:
        """Settings › Screen: captures once without keeping anything."""
        return self.capture(CaptureOptions(ocr_timeout=8))
