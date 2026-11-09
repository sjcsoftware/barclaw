"""Checks GitHub Releases for a newer BarClaw and installs it.

A self-contained install (tarball, `~/.local/share/barclaw/app`) is swapped in place after its
checksum and contents are verified. A .deb install goes through `pkexec apt-get`. Other package
managers own their files, so BarClaw points you at them instead of fighting them.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .support import ExpectedError, Observable, app_version, data_dir, in_thread, log, observed, package_dir, read_json, spawn, write_private

REPOSITORY = "sjcsoftware/barclaw"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"


class UpdateError(ExpectedError):
    pass


def parse_version(text: str) -> Optional[tuple]:
    """A dotted numeric version. A leading `v` and any `-beta`/`+build` suffix are ignored."""
    core = text.strip().lstrip("vV").split("-")[0].split("+")[0]
    try:
        parts = tuple(int(p) for p in core.split("."))
    except ValueError:
        return None
    # (1, 2) and (1, 2, 0) are the same version.
    while len(parts) > 1 and parts[-1] == 0:
        parts = parts[:-1]
    return parts


def format_version(version: tuple) -> str:
    return ".".join(map(str, version))


def parse_digest(text: str) -> Optional[str]:
    """Accepts GitHub's `sha256:<hex>` digest or a `sha256sum` line (`<hex>  file`)."""
    fields = text.strip().split()
    if not fields:
        return None
    token = fields[0].lower()
    if token.startswith("sha256:"):
        token = token[7:]
    return token if len(token) == 64 and all(c in "0123456789abcdef" for c in token) else None


@dataclass
class Release:
    version: tuple
    tag: str
    notes: str
    page_url: str
    tar_url: Optional[str] = None
    tar_name: Optional[str] = None
    deb_url: Optional[str] = None
    deb_name: Optional[str] = None
    assets: Optional[dict] = None

    def checksum_source(self, name: str) -> tuple:
        """(GitHub's digest or None, URL of `<name>.sha256` or None)."""
        assets = self.assets or {}
        entry = assets.get(name, {})
        sidecar = assets.get(name + ".sha256", {})
        return parse_digest(entry.get("digest") or ""), sidecar.get("url")


def parse_release(payload: bytes) -> Release:
    try:
        data = json.loads(payload)
    except ValueError as error:
        raise UpdateError("GitHub sent an unreadable answer.") from error
    tag = data.get("tag_name") or ""
    version = parse_version(tag)
    if version is None:
        raise UpdateError(f"The latest release has an unexpected tag ({tag}).")
    assets = {a["name"]: {"url": a["browser_download_url"], "digest": a.get("digest")} for a in data.get("assets", []) if "name" in a and "browser_download_url" in a}
    tar = next((n for n in assets if n.lower().startswith("barclaw-") and n.endswith(".tar.gz")), None)
    deb = next((n for n in assets if n.lower().startswith("barclaw") and n.endswith(".deb")), None)
    return Release(
        version=version, tag=tag, notes=data.get("body") or "", page_url=data.get("html_url") or RELEASES_PAGE,
        tar_url=assets[tar]["url"] if tar else None, tar_name=tar,
        deb_url=assets[deb]["url"] if deb else None, deb_name=deb, assets=assets,
    )


def feed_url() -> str:
    override = os.environ.get("BARCLAW_UPDATE_FEED")
    return override or f"https://api.github.com/repos/{REPOSITORY}/releases/latest"


def fetch_latest() -> Release:
    request = urllib.request.Request(feed_url(), headers={"Accept": "application/vnd.github+json", "User-Agent": f"BarClaw/{app_version()}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return parse_release(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise UpdateError("No releases are published yet.") from error
        if error.code in (403, 429):
            raise UpdateError("GitHub is rate-limiting update checks. Try again in a little while.") from error
        raise UpdateError(f"GitHub returned an error (HTTP {error.code}).") from error
    except (urllib.error.URLError, OSError) as error:
        raise UpdateError(f"Couldn't reach GitHub: {error}") from error


def install_kind() -> str:
    """`bundle` (replaceable in place), `deb`, `other` (another package manager), or `source`."""
    root = package_dir().resolve()
    if (root.parent / ".barclaw-install").exists():
        return "bundle"
    if str(root).startswith(("/usr/", "/opt/")):
        if shutil.which("dpkg-query"):
            done = subprocess.run(["dpkg-query", "-W", "-f=${Status}", "barclaw"], capture_output=True, text=True)
            if done.returncode == 0 and "install ok installed" in done.stdout:
                return "deb"
        return "other"
    return "source"


def download(url: str, target: Path, progress: Any = None) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": f"BarClaw/{app_version()}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                output.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
    except urllib.error.URLError as error:
        raise UpdateError(f"The download failed: {error}") from error


def verify_checksum(path: Path, release: Release, name: str) -> None:
    expected, sidecar = release.checksum_source(name)
    if expected is None and sidecar:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sum"
            download(sidecar, target)
            expected = parse_digest(target.read_text())
    if expected is None:
        raise UpdateError("The release has no checksum, so BarClaw won't install it.")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise UpdateError("The download didn't match its checksum. Try again.")


def stage_bundle(archive: Path, work: Path, expected: tuple) -> Path:
    """Unpacks the tarball and checks it's really BarClaw at the version the release claims."""
    unpacked = work / "unpacked"
    unpacked.mkdir()
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(unpacked, filter="data")
    except (tarfile.TarError, OSError) as error:
        raise UpdateError(f"The archive couldn't be unpacked: {error}") from error
    roots = [p for p in unpacked.iterdir() if p.is_dir()]
    if len(roots) != 1 or not (roots[0] / "barclaw" / "__init__.py").exists():
        raise UpdateError("The downloaded archive doesn't contain BarClaw.")
    try:
        version = parse_version((roots[0] / "barclaw" / "VERSION").read_text())
    except OSError as error:
        raise UpdateError("The downloaded archive has no version file.") from error
    if version != expected:
        raise UpdateError(f"The downloaded version doesn't match release {format_version(expected)}.")
    return roots[0]


def swap_bundle(installed: Path, staged: Path, work: Path) -> None:
    backup = work / "previous"
    try:
        os.rename(installed, backup)
    except OSError as error:
        raise UpdateError(f"Couldn't replace {installed}: {error}") from error
    try:
        shutil.move(str(staged), str(installed))
        (installed / ".barclaw-install").touch()
    except OSError as error:
        shutil.rmtree(installed, ignore_errors=True)
        os.rename(backup, installed)
        raise UpdateError(f"Couldn't install the new version: {error}") from error


class Updater(Observable):
    status_text: str = observed("")
    is_busy: bool = observed(False)

    MARKER = "updated.json"

    def __init__(self) -> None:
        super().__init__()
        marker = data_dir() / self.MARKER
        previous = read_json(marker).get("from")
        self.updated_from: Optional[str] = previous if isinstance(previous, str) else None
        if self.updated_from:
            self.status_text = f"Updated from {self.updated_from}."
            try:
                marker.unlink()
            except OSError:
                pass

    def check_for_updates(self) -> None:
        if not self.is_busy:
            spawn(self._run())

    def _run(self):
        self.is_busy = True
        self.status_text = "Checking…"
        try:
            yield from self._update()
        except UpdateError as error:
            log.error("Update failed: %s", error)
            self.status_text = str(error)
        except (OSError, subprocess.SubprocessError) as error:
            log.error("Update failed: %s", error)
            self.status_text = f"The update failed: {error}"
        finally:
            self.is_busy = False

    def _update(self):
        release = yield in_thread(fetch_latest)
        current = parse_version(app_version())
        if current is None:
            raise UpdateError(f"This is a development build, so it can't update itself. Latest release: {format_version(release.version)}.")
        if release.version <= current:
            self.status_text = f"You're up to date ({format_version(current)})."
            return
        version = format_version(release.version)
        kind = install_kind()
        if kind == "source":
            raise UpdateError(f"BarClaw {version} is out. You're running from a source checkout: pull and rerun, or see {release.page_url}")
        if kind == "other":
            raise UpdateError(f"BarClaw {version} is out. Update it through your package manager, or see {release.page_url}")
        log.info("Updating BarClaw to %s (%s)", version, kind)
        with tempfile.TemporaryDirectory(prefix="barclaw-update-") as directory:
            work = Path(directory)
            if kind == "deb":
                yield from self._install_deb(release, version, work)
            else:
                yield from self._install_bundle(release, version, work)
            write_private(data_dir() / self.MARKER, json.dumps({"from": app_version()}).encode())
        self.status_text = f"Restarting into {version}…"
        os.execv(sys.executable, [sys.executable, "-m", "barclaw"])

    def _progress(self, version: str) -> Any:
        def report(fraction: float) -> None:
            from gi.repository import GLib

            GLib.idle_add(lambda: (setattr(self, "status_text", f"Downloading {version}… {int(fraction * 100)}%"), GLib.SOURCE_REMOVE)[1])

        return report

    def _install_bundle(self, release: Release, version: str, work: Path):
        if not release.tar_url:
            raise UpdateError(f"BarClaw {version} is out, but its archive isn't uploaded yet. Try again in a few minutes.")
        archive = work / release.tar_name
        yield in_thread(download, release.tar_url, archive, self._progress(version))
        self.status_text = f"Installing {version}…"
        yield in_thread(verify_checksum, archive, release, release.tar_name)
        # The update stages next to the install so the final rename stays on one filesystem.
        installed = package_dir().resolve().parent
        staging = Path(tempfile.mkdtemp(prefix=".update-", dir=installed.parent))
        try:
            staged = yield in_thread(stage_bundle, archive, staging, release.version)
            yield in_thread(swap_bundle, installed, staged, staging)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _install_deb(self, release: Release, version: str, work: Path):
        if not release.deb_url:
            raise UpdateError(f"BarClaw {version} is out, but its package isn't uploaded yet. Try again in a few minutes.")
        package = work / release.deb_name
        yield in_thread(download, release.deb_url, package, self._progress(version))
        self.status_text = f"Installing {version}…"
        yield in_thread(verify_checksum, package, release, release.deb_name)
        if not shutil.which("pkexec"):
            raise UpdateError(f"Installing needs administrator rights. Run: sudo apt install {package}")
        # apt runs as the `_apt` user and needs to be able to read the file.
        os.chmod(work, 0o755)
        os.chmod(package, 0o644)
        done = yield in_thread(subprocess.run, ["pkexec", "apt-get", "install", "-y", str(package)], capture_output=True, text=True)
        if done.returncode != 0:
            raise UpdateError("The package install was cancelled or failed.")
