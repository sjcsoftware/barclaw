# Changelog

## 0.1.5

- The six buttons around the agent are all the same width again. A long session or model name is shortened with "…" instead of widening its column, and the full name shows on hover.

## 0.1.4

- Start at login on X11 no longer leaves you without an icon when BarClaw starts before a StatusNotifier bar like Eww or Waybar. The icon moves to the bar as soon as it appears, and the panel BarClaw opened while it waited closes again.
- Launching BarClaw while it's already running (from rofi or an app menu) now opens the panel instead of doing nothing.
- `scripts/test.sh` without Xvfb no longer runs the tests on your real display and session bus.

## 0.1.3

- The menu bar icon no longer has a usage stripe under it, in any state. Codex usage still shows in the icon's tooltip and next to the model name in the panel.

## 0.1.2

- The panel now sits below the menu bar on every desktop. Bars that reserve no space, like Eww with i3 gaps, no longer overlap it.

## 0.1.1

- One-line install: `install.sh` pulls the latest release and picks the build for your desktop, including the GNOME Shell button where it's needed.
- Removed the `.deb` package and the Arch and Fedora recipes. Everything now installs the same way.
- Start at login now works for installed copies.
- The "jump to latest" button no longer lingers once a conversation fits on screen.

## 0.1.0

First release: NotchClaw's feature set for the Linux menu bar.

- Menu bar icon over StatusNotifierItem and XEmbed, with state rings, a usage meter, and a right-click menu.
- Drop-down panel with the home and chat screens, tool progress, approvals, and session, model, effort, and compaction controls.
- Replies as Markdown with LaTeX math, charts, tables, highlighted code, and generated images.
- Hold to talk with live on-device transcription and voice commands.
- Visual intelligence: on-device OCR, screen sharing with messages, and an OpenClaw node so the agent can look when it needs to.
- In-app updates, a Debian package, and a no-root tarball install.
