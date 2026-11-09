# Contributing to BarClaw

Thanks for helping! BarClaw is a small GTK app with few dependencies, and we'd like to keep it that way.

- [Development](#development)
- [How it works](#how-it-works)
- [Guidelines](#guidelines)
- [Commits and PRs](#commits-and-prs)
- [Releases](#releases)
- [Privacy and security](#privacy-and-security)
- [Troubleshooting](#troubleshooting)

## Development

```bash
PYTHONPATH=src python3 -m barclaw   # run from the checkout
scripts/test.sh                     # tests, on a private Xvfb and session bus
scripts/build.sh                    # tarball, .deb, and checksums in dist/
scripts/screenshots.sh              # regenerate README images
```

`xvfb` and `pytest` are needed for the full test suite.

**Develop without a real agent.** `tools/mock-gateway` is a fake gateway that checks every request against OpenClaw's published protocol schema and streams a canned reply. A message containing `rich-demo` exercises every renderer:

```bash
cd tools/mock-gateway && npm install && PORT=18799 npm start
# then set the gateway URL in BarClaw to ws://127.0.0.1:18799
```

**Keep test runs off your desktop.** Set `BARCLAW_CONFIG_DIR` and `BARCLAW_DATA_DIR` so credentials stay out of your everyday install, and run the app on a private `DISPLAY` with `dbus-run-session`. In scripts, stop processes by the PID you started. Never use `pkill` or `killall` by name: an `i3` or `Xorg` it matches may be your live session.

The original product brief lives in [docs/IDEA.md](docs/IDEA.md).

## How it works

BarClaw is an operator client for the [OpenClaw Gateway WebSocket protocol](https://github.com/openclaw/openclaw/tree/main/docs/gateway/protocol) (v4).

```
src/barclaw/
├── application.py  AppModel (wiring), Gtk.Application, D-Bus control interface
├── updater.py      In-app updates from GitHub Releases
├── support.py      Paths, change notification, generator-based tasks on the GLib loop
├── gateway/        GatewayClient (handshake, RPC, events, reconnect), device identity (Ed25519)
├── agent/          AgentSession (chat stream, history, approvals, model/effort/compact, usage, images)
├── bar/            Tray backends (SNI + DBusMenu, XEmbed), the panel, custom-drawn widgets, theme
├── markdown/       Block parser, GTK renderer, LaTeX typesetter, charts, syntax highlighting
├── voice/          Global hotkey (X11, XDG portal), recorder + speech worker, voice-command parser
├── vision/         Capture and OCR, screen intent, the OpenClaw node, edge glow
└── settings/       Preferences and the Settings window
```

- **Why GTK 3.** The panel needs to place itself under the icon, and legacy trays need `GtkStatusIcon`. GTK 4 removed both.
- **Why no WebKit.** Everything is drawn with Pango and Cairo, so replies never use the GPU and the download stays small.
- **Handshake.** BarClaw waits for `connect.challenge` and signs the challenge-bound payload with a per-install Ed25519 key. It connects as `openclaw-linux` with platform `linux` and device family `Linux`; OpenClaw's node policy reads those two fields.
- **Streaming.** It subscribes with `sessions.messages.subscribe` and renders `chat` events (`delta`, `final`, `error`), restoring history with `chat.history`.
- **Controls** call `sessions.patch` (model, `thinkingLevel`), `sessions.compact`, `sessions.reset`, `chat.abort`, `exec.approval.resolve`, `usage.status`, and `artifacts.download`.
- **Liveness.** A tick watchdog, `GNetworkMonitor`, and logind's resume signal trigger an immediate reconnect, with exponential backoff (up to 10 s) otherwise.
- **The tray.** `SniTray` implements StatusNotifierItem and DBusMenu directly, because libappindicator can't report left clicks. `XEmbedTray` covers bars with only a legacy tray.
- **Voice.** A separate worker process runs Vosk in BarClaw's private virtualenv. It reads 16 kHz PCM on stdin and writes JSON lines on stdout, so recognition can never stall the UI.
- **Visual intelligence** has two paths, both behind **Settings › Screen** (on by default):
  - *Direct.* When a message refers to the screen (`vision/context.py`, or the composer's eye toggle), `ScreenReader` hides BarClaw's windows for an instant, grabs the active monitor, and runs Tesseract on the CPU with a 5 s deadline. The image goes as a `chat.send` attachment, and a `<barclaw-screen>` block with the app, title, and OCR text goes ahead of the message (stripped again when history loads).
  - *Node.* A second connection in role `node` declares `screen.snapshot` (on OpenClaw's default Linux allowlist, so no gateway config) and publishes a `barclaw_screen` tool and `barclaw-screen` skill. The node needs device and command-surface approval once. With `operator.admin`, BarClaw approves itself.
- **Control interface.** `dev.barclaw.Control` at `/dev/barclaw/Control` exposes the icon state and `Toggle`, `TalkStart`, `TalkStop`. The GNOME Shell extension is built on it.

## Guidelines

- **Keep dependencies small.** GTK, libsoup, and `cryptography` are the only hard ones. Anything else is optional and must fail gracefully.
- **Never touch the GPU.** Rendering is Cairo, speech and OCR are CPU-only. Please keep it that way.
- **Be a good desktop citizen**: respect `gtk-enable-animations`, and only animate while something is visible.
- **Protocol changes**: follow OpenClaw's [gateway protocol docs](https://github.com/openclaw/openclaw/tree/main/docs/gateway/protocol) and check new requests against the mock gateway.
- **Add tests** for parsing and protocol logic in `tests/`.
- **UI changes**: run `scripts/screenshots.sh` and include before/after images in your PR.

## Commits and PRs

- Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat(tray): …`, `fix(voice): …`).
- Keep PRs focused, and describe how you tested (real gateway, mock gateway, or both, and which desktop).

## Releases

In-app updates read GitHub's `releases/latest` for `sjcsoftware/barclaw`, so a release has to look like this:

1. Bump `VERSION`, add a `CHANGELOG.md` entry, commit, and push to `main`.
2. Run `scripts/build.sh`.
3. Publish a non-draft, non-prerelease release tagged `v<version>` with every file in `dist/`:

   ```bash
   gh release create v<version> dist/* --title "BarClaw <version>" --notes "…"
   ```

The updater installs only if the release is newer than the running app, and requires a checksum (GitHub's asset digest or the `.sha256` file). Tarball installs must unpack to BarClaw at the same version as the tag. `.deb` installs go through `pkexec apt-get`. Other package managers are left to update themselves.

To test an update without publishing anything, set `BARCLAW_UPDATE_FEED` to the URL of a `releases/latest`-shaped JSON.

## Privacy and security

- The gateway token and device key are stored in `~/.local/share/barclaw/credentials.json` with `0600` permissions.
- Speech is transcribed on your computer. Audio is only captured while you hold the shortcut, and goes only to the recognizer you chose.
- Screenshots are taken only while **visual intelligence** is on: when you send a message that refers to the screen (or turn on the eye toggle), or when your agent calls the BarClaw node's screen tool. The screen edges glow during every capture, and BarClaw tells you when the agent looked. BarClaw's own windows are never captured. Images and OCR text go only to your gateway and aren't stored.
- BarClaw talks only to the gateway you configure, plus GitHub when you click **Check for Updates** and the Vosk model server when you install speech recognition. It has no analytics or telemetry.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| No icon appears | Your desktop may have no tray host; see [Desktops](README.md#desktops). `barclaw --toggle` always opens the panel |
| "Approve this computer on your gateway" | Run `openclaw devices approve <id>` on the gateway host |
| "Gateway token doesn't match" | Re-enter `gateway.auth.token` in Settings › Gateway |
| Holding <kbd>Alt Space</kbd> does nothing | Another app may own that shortcut (Settings › Voice warns about this); pick a different one. On Wayland see [Desktops](README.md#desktops) |
| "Install speech recognition…" in the bubble | Click the install button in Settings › Voice. It needs `python3-venv` |
| No usage meter | The gateway's model isn't using a Codex/ChatGPT login, or **Show Codex usage** is off |
| "Sent without screen" | Your compositor refused the capture. On Wayland install `grim` or use a desktop with the screenshot portal |
| A screenshot went out without OCR text | `tesseract-ocr` isn't installed, or OCR hit its deadline; the image itself was sent |
| Settings › Screen says "Waiting for approval" | Turn on **Admin access** (BarClaw then approves itself) or run the command shown there on the gateway host |
