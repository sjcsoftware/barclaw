# BarClaw for GNOME Shell

GNOME Shell has no system tray, and GTK apps can't add anything to its top bar. On a desktop without the [AppIndicator extension](https://extensions.gnome.org/extension/615/appindicator-support/), this extension gives BarClaw its button.

It's a thin front end: the BarClaw app from `main` still does all the work. The extension draws the app's state and forwards clicks over D-Bus (`dev.barclaw.Control`). On Wayland it also places the panel under the button, which a GTK window can't do for itself.

## Install

The [BarClaw installer](https://github.com/sjcsoftware/barclaw#install) adds this automatically on GNOME when no AppIndicator extension is on. Log out and back in to see the button.

To force it anyway:

```bash
curl -fsSL https://raw.githubusercontent.com/sjcsoftware/barclaw/main/install.sh | BARCLAW_VARIANT=gnome bash
```

## Use

| Do this | To |
| --- | --- |
| Left-click | Open or close the panel (starts BarClaw if it isn't running) |
| Middle-click | Start talking |
| Right-click | Open Settings |

While the extension is on, BarClaw doesn't show a tray icon, so you won't get two.

Written for GNOME 45 to 49.

## Status

Not yet run inside a live GNOME Shell. The D-Bus interface it depends on is tested from the app side and the JavaScript passes a syntax check, but expect rough edges until it has had a real session.
