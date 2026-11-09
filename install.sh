#!/usr/bin/env bash
# BarClaw installer. Installs the latest release for the current user, with the build for your desktop:
#
#   curl -fsSL https://raw.githubusercontent.com/sjcsoftware/barclaw/main/install.sh | bash
#
# Options:     --detect                  print which build this desktop gets, and exit
# Environment: BARCLAW_VARIANT=gtk|gnome  override the detected build
#              BARCLAW_VERSION=x.y.z      install a specific release
set -euo pipefail

RELEASES="${BARCLAW_RELEASES_URL:-https://github.com/sjcsoftware/barclaw/releases}"
EXTENSION_UUID="barclaw@sjcsoftware.github.io"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APP="$DATA/barclaw/app"
BIN="$HOME/.local/bin"

say() { printf '%s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

# MARK: Which build this desktop gets

desktop_name() {
  printf '%s' "${XDG_CURRENT_DESKTOP:-${XDG_SESSION_DESKTOP:-${DESKTOP_SESSION:-}}}" | tr '[:upper:]' '[:lower:]'
}

has_appindicator() {
  command -v gnome-extensions >/dev/null && gnome-extensions list --enabled 2>/dev/null | grep -q appindicator
}

# `gnome` adds the GNOME Shell extension from the platform/gnome-shell branch. Everything else is `gtk`.
# GNOME with the AppIndicator extension already has a working tray, so it's `gtk` too.
detect_variant() {
  if [[ -n "${BARCLAW_VARIANT:-}" ]]; then
    [[ "$BARCLAW_VARIANT" == gtk || "$BARCLAW_VARIANT" == gnome ]] || die "BARCLAW_VARIANT must be gtk or gnome."
    printf '%s\n' "$BARCLAW_VARIANT"
    return
  fi
  local desktop
  desktop="$(desktop_name)"
  case ":$desktop:" in
    # These set GNOME or ubuntu in the name but run their own panel, with a tray.
    *budgie*|*flashback*|*unity*) printf 'gtk\n'; return ;;
    *:gnome*|*:ubuntu*) ;;
    *) printf 'gtk\n'; return ;;
  esac
  if command -v gnome-shell >/dev/null && ! has_appindicator; then
    printf 'gnome\n'
  else
    printf 'gtk\n'
  fi
}

# MARK: System packages

check_python() {
  python3 - 2>/dev/null <<'PY'
import sys
assert sys.version_info >= (3, 10)
import cairo, cryptography, gi
gi.require_foreign("cairo")
gi.require_version("Gtk", "3.0")
gi.require_version("Soup", "3.0")
from gi.repository import Gtk, Soup
PY
}

package_manager() {
  local tool
  for tool in apt-get dnf pacman zypper; do
    command -v "$tool" >/dev/null && { printf '%s\n' "$tool"; return; }
  done
}

required_packages() {
  case "$1" in
    apt-get) printf 'python3-gi python3-gi-cairo python3-cairo python3-cryptography gir1.2-gtk-3.0 gir1.2-soup-3.0' ;;
    dnf) printf 'python3-gobject python3-cairo python3-cryptography gtk3 libsoup3' ;;
    pacman) printf 'python-gobject python-cairo python-cryptography gtk3 libsoup3' ;;
    zypper) printf 'python3-gobject python3-gobject-cairo python3-cryptography typelib-1_0-Gtk-3_0 typelib-1_0-Soup-3_0' ;;
  esac
}

optional_packages() {
  case "$1" in
    apt-get) printf 'python3-venv x11-utils tesseract-ocr fonts-noto-color-emoji fonts-lmodern' ;;
    dnf) printf 'xprop tesseract google-noto-color-emoji-fonts' ;;
    pacman) printf 'xorg-xprop tesseract tesseract-data-eng noto-fonts-emoji' ;;
    zypper) printf 'xprop tesseract-ocr noto-coloremoji-fonts' ;;
  esac
}

install_command() {
  case "$1" in
    apt-get) printf 'sudo apt-get install -y' ;;
    dnf) printf 'sudo dnf install -y' ;;
    pacman) printf 'sudo pacman -S --needed --noconfirm' ;;
    zypper) printf 'sudo zypper install -y' ;;
  esac
}

can_prompt() { { : </dev/tty; } 2>/dev/null; }

ensure_dependencies() {
  check_python && return
  local manager command required optional answer
  manager="$(package_manager)"
  [[ -n "$manager" ]] || die "BarClaw needs Python 3.10+, GTK 3, libsoup 3, PyGObject, pycairo, and cryptography. Install them with your package manager, then run this again."
  command="$(install_command "$manager")"
  required="$(required_packages "$manager")"
  optional="$(optional_packages "$manager")"
  say "BarClaw needs a few system packages:"
  say "  $command $required"
  can_prompt || die "Run that command, then run this installer again."
  read -r -p "Install them now? [Y/n] " answer </dev/tty
  case "$answer" in
    ""|y|Y|yes|YES) ;;
    *) die "Run that command, then run this installer again." ;;
  esac
  # shellcheck disable=SC2086  # Both lists are meant to split into words.
  $command $required </dev/tty
  # Optional extras must never fail the install.
  # shellcheck disable=SC2086
  $command $optional </dev/tty || warn "Some optional packages weren't installed; OCR or voice may be limited."
  check_python || die "The packages installed, but Python still can't load GTK. Check that your default python3 is the system one."
}

suggest_optional() {
  command -v tesseract >/dev/null && command -v xprop >/dev/null && python3 -c 'import ensurepip' 2>/dev/null && return
  local manager
  manager="$(package_manager)"
  [[ -n "$manager" ]] || return 0
  say ""
  say "Optional, for reading the screen and for voice:"
  say "  $(install_command "$manager") $(optional_packages "$manager")"
}

# MARK: Downloading

latest_version() {
  local url version
  url="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$RELEASES/latest")" || die "Couldn't reach GitHub Releases."
  version="${url##*/}"
  version="${version#v}"
  [[ "$version" =~ ^[0-9]+(\.[0-9]+)*$ ]] || die "Couldn't find a published release."
  printf '%s\n' "$version"
}

# Fetches an asset and its .sha256 into the directory, and verifies it.
fetch_verified() {
  local version="$1" name="$2" directory="$3"
  curl -fsSL --retry 2 -o "$directory/$name" "$RELEASES/download/v$version/$name" || return 1
  curl -fsSL --retry 2 -o "$directory/$name.sha256" "$RELEASES/download/v$version/$name.sha256" || return 1
  (cd "$directory" && sha256sum -c --quiet "$name.sha256" >/dev/null) || die "$name didn't match its checksum, so nothing was installed."
}

# MARK: Installing

install_app() {
  local source="$1" staged
  [[ -f "$source/barclaw/__init__.py" ]] || die "The download doesn't contain BarClaw."
  mkdir -p "$DATA/barclaw" "$BIN" "$DATA/applications" "$DATA/icons/hicolor/256x256/apps"
  # Built beside the old install and swapped in, so a failure can't leave half of each.
  staged="$(mktemp -d "$DATA/barclaw/.install.XXXXXX")"
  chmod 755 "$staged"
  cp -r "$source/barclaw" "$staged/"
  touch "$staged/.barclaw-install"
  if [[ -d "$APP" ]]; then
    mv "$APP" "$staged.old"
  fi
  mv "$staged" "$APP"
  rm -rf "$staged.old"

  cat > "$BIN/barclaw" <<LAUNCH
#!/bin/sh
PYTHONPATH="$APP" exec python3 -m barclaw "\$@"
LAUNCH
  chmod +x "$BIN/barclaw"
  sed "s|^Exec=barclaw|Exec=$BIN/barclaw|" "$source/dev.barclaw.Barclaw.desktop" > "$DATA/applications/dev.barclaw.Barclaw.desktop"
  cp "$source/icon.png" "$DATA/icons/hicolor/256x256/apps/dev.barclaw.Barclaw.png"
}

# On Wayland the shell only discovers a new extension at login, so it's also added to the
# enabled list, which takes effect then.
enable_extension() {
  gnome-extensions enable "$EXTENSION_UUID" 2>/dev/null && return 0
  local current updated
  current="$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null)" || return 1
  updated="$(python3 -c '
import ast, sys
values = ast.literal_eval(sys.argv[1].replace("@as ", ""))
if sys.argv[2] not in values:
    values.append(sys.argv[2])
print(str(values))' "$current" "$EXTENSION_UUID")" || return 1
  gsettings set org.gnome.shell enabled-extensions "$updated" 2>/dev/null
}

gnome_supported() {
  local major
  major="$(gnome-shell --version 2>/dev/null | sed -E 's/[^0-9]*([0-9]+).*/\1/')"
  [[ "$major" =~ ^[0-9]+$ ]] && (( major >= 45 ))
}

install_extension() {
  local version="$1" directory="$2" name target
  name="gnome-shell-extension-$version.tar.gz"
  if ! gnome_supported; then
    warn "The BarClaw button needs GNOME 45 or newer. Install the AppIndicator extension to get the icon instead."
    return 0
  fi
  if ! fetch_verified "$version" "$name" "$directory"; then
    warn "Couldn't download the GNOME Shell extension. Install the AppIndicator extension to get the icon instead."
    return 0
  fi
  tar -xzf "$directory/$name" -C "$directory"
  target="$DATA/gnome-shell/extensions/$EXTENSION_UUID"
  mkdir -p "$target"
  cp "$directory/gnome-shell-extension-$version/gnome-shell/$EXTENSION_UUID/"* "$target/"
  say "Installed the GNOME Shell button."
  enable_extension || warn "Couldn't enable the extension. After logging back in, run: gnome-extensions enable $EXTENSION_UUID"
  say "Log out and back in to see it in the top bar."
}

main() {
  if [[ "${1:-}" == "--detect" ]]; then
    detect_variant
    return
  fi
  [[ "$(id -u)" -ne 0 ]] || die "Run this as your normal user, not root. It installs into your home folder."
  local tool
  for tool in curl tar sha256sum python3; do
    command -v "$tool" >/dev/null || die "$tool is required."
  done

  local variant upgrading=""
  variant="$(detect_variant)"
  [[ -d "$APP" ]] && upgrading=1
  ensure_dependencies

  local work version source here
  work="$(mktemp -d)"
  # shellcheck disable=SC2064  # The path is fixed now, on purpose.
  trap "rm -rf '$work'" EXIT

  # Run from an unpacked release tarball, use it. Piped from curl, download the release.
  here=""
  if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  fi
  if [[ -n "$here" && -f "$here/barclaw/__init__.py" && -f "$here/barclaw/VERSION" ]]; then
    source="$here"
    version="$(cat "$here/barclaw/VERSION")"
    say "Installing BarClaw $version from $here"
  else
    version="${BARCLAW_VERSION:-$(latest_version)}"
    say "Downloading BarClaw $version"
    fetch_verified "$version" "barclaw-$version.tar.gz" "$work" || die "Couldn't download BarClaw $version."
    tar -xzf "$work/barclaw-$version.tar.gz" -C "$work"
    source="$work/barclaw-$version"
  fi

  install_app "$source"
  if [[ "$variant" == gnome ]]; then
    install_extension "$version" "$work"
  fi

  say ""
  if [[ -n "$upgrading" ]]; then
    say "BarClaw $version is installed. If it's running, quit it and start it again."
  else
    say "BarClaw $version is installed. Open it from your apps menu, or run: barclaw"
  fi
  case ":$PATH:" in
    *":$BIN:"*) ;;
    *) say "To run it from a terminal, add this folder to your PATH: $BIN" ;;
  esac
  suggest_optional
}

# Wrapped in a function so a connection dropped partway through a `curl | bash` runs nothing.
main "$@"
