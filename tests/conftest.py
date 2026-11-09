"""Tests never read or write the real config, and never reach a real display server unless given one."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("BARCLAW_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("BARCLAW_DATA_DIR", str(tmp_path / "data"))
    from barclaw.gateway.identity import SecureStore

    monkeypatch.setattr(SecureStore, "_shared", None)
    yield


def has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
