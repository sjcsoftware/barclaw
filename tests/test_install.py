"""The installer's desktop detection, run for real with stubbed desktop tools."""
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parent.parent / "install.sh"


def detect(tmp_path, env, gnome_shell=True, enabled=""):
    # Only these tools exist for the script, so the machine's own desktop can't affect the result.
    stubs = tmp_path / f"stubs{len(list(tmp_path.iterdir()))}"
    stubs.mkdir()
    for tool in ("tr", "grep"):
        (stubs / tool).symlink_to(shutil.which(tool))
    if gnome_shell:
        (stubs / "gnome-shell").write_text("#!/bin/sh\n")
        (stubs / "gnome-extensions").write_text(f"#!/bin/sh\nprintf '%s\\n' '{enabled}'\n")
        (stubs / "gnome-shell").chmod(0o755)
        (stubs / "gnome-extensions").chmod(0o755)
    clean = {"PATH": str(stubs), "HOME": str(tmp_path), **env}
    done = subprocess.run([shutil.which("bash"), str(INSTALLER), "--detect"], env=clean, capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.mark.parametrize("desktop", ["GNOME", "ubuntu:GNOME", "pop:GNOME", "GNOME-Classic:GNOME", "zorin:GNOME"])
def test_gnome_without_a_tray_gets_the_extension(tmp_path, desktop):
    assert detect(tmp_path, {"XDG_CURRENT_DESKTOP": desktop}) == "gnome"


def test_gnome_with_appindicator_already_has_a_tray(tmp_path):
    enabled = "ubuntu-appindicators@ubuntu.com"
    assert detect(tmp_path, {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, enabled=enabled) == "gtk"


@pytest.mark.parametrize("desktop", [
    "i3", "KDE", "XFCE", "X-Cinnamon", "MATE", "LXQt", "sway", "Hyprland", "COSMIC",
    "Budgie:GNOME", "GNOME-Flashback:GNOME", "Unity:Unity7:ubuntu", "",
])
def test_everything_else_gets_the_gtk_build(tmp_path, desktop):
    assert detect(tmp_path, {"XDG_CURRENT_DESKTOP": desktop}) == "gtk"


def test_session_name_is_the_fallback(tmp_path):
    assert detect(tmp_path, {"DESKTOP_SESSION": "gnome-xorg"}) == "gnome"


def test_gnome_name_without_the_shell_is_gtk(tmp_path):
    assert detect(tmp_path, {"XDG_CURRENT_DESKTOP": "GNOME"}, gnome_shell=False) == "gtk"


def test_override_wins(tmp_path):
    assert detect(tmp_path, {"XDG_CURRENT_DESKTOP": "GNOME", "BARCLAW_VARIANT": "gtk"}) == "gtk"
    assert detect(tmp_path, {"XDG_CURRENT_DESKTOP": "i3", "BARCLAW_VARIANT": "gnome"}) == "gnome"
