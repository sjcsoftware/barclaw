"""Shared plumbing: paths, logging, change notification, and generator-based tasks.

BarClaw runs everything on the GLib main loop. Long operations are written as generators that
`yield` a `Future` and resume with its result, so protocol code reads top to bottom without
callback chains or a second event loop.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Generator, Iterable, Optional

from gi.repository import GLib

log = logging.getLogger("barclaw")


def setup_logging() -> None:
    level = logging.DEBUG if os.environ.get("BARCLAW_DEBUG") else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")


# MARK: Paths


def config_dir() -> Path:
    override = os.environ.get("BARCLAW_CONFIG_DIR")
    return Path(override) if override else Path(GLib.get_user_config_dir()) / "barclaw"


def data_dir() -> Path:
    override = os.environ.get("BARCLAW_DATA_DIR")
    return Path(override) if override else Path(GLib.get_user_data_dir()) / "barclaw"


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def resource(*parts: str) -> Path:
    """A bundled file. Source checkouts keep `data/` at the repo root; installs keep it in the package."""
    bundled = package_dir().joinpath("data", *parts)
    if bundled.exists():
        return bundled
    return package_dir().parent.parent.joinpath("data", *parts)


def app_version() -> str:
    for candidate in (package_dir() / "VERSION", package_dir().parent.parent / "VERSION"):
        try:
            return candidate.read_text().strip()
        except OSError:
            continue
    return "0.0.0-dev"


def write_private(path: Path, data: bytes) -> None:
    """Atomically writes a file only the user can read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# MARK: Small helpers


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def text_of(value: Any) -> Optional[str]:
    """A non-empty string form of a JSON scalar, like NotchClaw's `JSON.text`."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return None


def number_of(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def dig(value: Any, *keys: str) -> Any:
    """Safe nested lookup through dicts; returns None as soon as a key is missing."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


# MARK: Observation


class Observable:
    """Coalesced change notification: any number of `changed()` calls runs observers once."""

    def __init__(self) -> None:
        self._observers: list[Callable[[], None]] = []
        self._notify_source: int = 0

    def observe(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._observers.append(callback)

        def remove() -> None:
            if callback in self._observers:
                self._observers.remove(callback)

        return remove

    def changed(self) -> None:
        if self._notify_source == 0:
            self._notify_source = GLib.idle_add(self._flush, priority=GLib.PRIORITY_DEFAULT_IDLE - 10)

    def _flush(self) -> bool:
        self._notify_source = 0
        for callback in list(self._observers):
            try:
                callback()
            except Exception:
                log.exception("Observer failed")
        return GLib.SOURCE_REMOVE


class observed:
    """A descriptor that calls `changed()` when the value actually changes."""

    def __init__(self, default: Any = None) -> None:
        self.default = default

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = "_" + name

    def __get__(self, instance: Any, owner: Optional[type] = None) -> Any:
        if instance is None:
            return self
        return instance.__dict__.get(self.name, self.default)

    def __set__(self, instance: Any, value: Any) -> None:
        if instance.__dict__.get(self.name, self.default) != value or self.name not in instance.__dict__:
            instance.__dict__[self.name] = value
            instance.changed()


# MARK: Tasks


class CancelledError(Exception):
    pass


class Future:
    """A value that arrives later on the main loop."""

    def __init__(self) -> None:
        self.done = False
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self._callbacks: list[Callable[["Future"], None]] = []

    def set_result(self, value: Any = None) -> None:
        if not self.done:
            self.done, self.result = True, value
            self._fire()

    def set_error(self, error: BaseException) -> None:
        if not self.done:
            self.done, self.error = True, error
            self._fire()

    def add_done(self, callback: Callable[["Future"], None]) -> None:
        if self.done:
            callback(self)
        else:
            self._callbacks.append(callback)

    def _fire(self) -> None:
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(self)


TaskBody = Generator[Future, Any, Any]


class Task(Future):
    """Drives a generator, resuming it each time the future it yielded completes."""

    def __init__(self, body: TaskBody) -> None:
        super().__init__()
        self._body = body
        self._waiting: Optional[Future] = None
        self.cancelled = False
        self._step(None, None)

    def _step(self, value: Any, error: Optional[BaseException]) -> None:
        try:
            yielded = self._body.throw(error) if error is not None else self._body.send(value)
        except StopIteration as stop:
            self.set_result(stop.value)
            return
        except CancelledError as cancelled:
            self.set_error(cancelled)
            return
        except Exception as failure:
            log.exception("Task failed") if not isinstance(failure, ExpectedError) else None
            self.set_error(failure)
            return
        self._waiting = yielded
        yielded.add_done(self._resume)

    def _resume(self, future: Future) -> None:
        if future is not self._waiting:
            return
        self._waiting = None
        self._step(future.result, future.error)

    def cancel(self) -> None:
        if self.done or self.cancelled:
            return
        self.cancelled = True
        waiting, self._waiting = self._waiting, None
        if isinstance(waiting, Task):
            waiting.cancel()
        if isinstance(waiting, Sleep):
            waiting.stop()
        self._step(None, CancelledError())


class ExpectedError(Exception):
    """A failure that is reported to the user rather than logged as a bug."""


def spawn(body: TaskBody) -> Task:
    return Task(body)


class Sleep(Future):
    def __init__(self, seconds: float) -> None:
        super().__init__()
        self._source = GLib.timeout_add(max(0, int(seconds * 1000)), self._elapsed)

    def _elapsed(self) -> bool:
        self._source = 0
        self.set_result(None)
        return GLib.SOURCE_REMOVE

    def stop(self) -> None:
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0


def sleep(seconds: float) -> Future:
    return Sleep(seconds)


def in_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
    """Runs blocking work on a daemon thread and completes on the main loop."""
    future = Future()

    def finish(result: Any, error: Optional[BaseException]) -> bool:
        future.set_error(error) if error is not None else future.set_result(result)
        return GLib.SOURCE_REMOVE

    def worker() -> None:
        try:
            result, error = fn(*args, **kwargs), None
        except Exception as failure:  # Delivered to whoever awaits the future.
            result, error = None, failure
        GLib.idle_add(finish, result, error)

    threading.Thread(target=worker, daemon=True).start()
    return future


def run_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Spins the main context until `predicate` holds (tests and scripted runs)."""
    context = GLib.MainContext.default()
    deadline = GLib.get_monotonic_time() + int(timeout * 1_000_000)
    while not predicate():
        if GLib.get_monotonic_time() > deadline:
            return False
        if not context.iteration(False):
            GLib.usleep(1000)
    return True


def first_present(values: Iterable[Any]) -> Any:
    return next((value for value in values if value is not None), None)
