BarClaw is an OpenClaw agent node for Linux, modeled on [NotchClaw](https://github.com/sjcsoftware/notchclaw) for the MacBook notch.

# Goals

- Works the same as NotchClaw wherever Linux allows it, focused for Linux.
- Lives as a menu bar icon rather than in a notch.
- A general-purpose GTK app on `main`. Desktops GTK can't reach get their own branch (`platform/gnome-shell`).

# Where it differs from NotchClaw

| NotchClaw | BarClaw |
| --- | --- |
| Hover the notch | Click the icon |
| Notch glow | Icon ring and optional desktop notifications |
| Apple Speech | Vosk, or your own command |
| Vision OCR | Tesseract |
| ScreenCaptureKit | GDK on X11; grim, Spectacle, or the portal on Wayland |
| Carbon hotkey | X11 key grab or the GlobalShortcuts portal |
