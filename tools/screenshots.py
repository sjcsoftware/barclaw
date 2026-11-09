"""Renders README images from the real panel widgets with staged data. Run through scripts/screenshots.sh."""
import math
import sys
import time

import barclaw
import cairo
from gi.repository import Gdk, Pango, PangoCairo

from barclaw.agent.models import AgentActivity, ChatItem, ModelOption, RunProgress, Step, UsageInfo, UsageWindow
from barclaw.application import AppModel
from barclaw.bar.theme import install_css
from barclaw.bar.tray import TrayState, render_icon
from barclaw.gateway.client import ConnectionState
from barclaw.support import run_until

OUT = sys.argv[1]
SCREEN_W, SCREEN_H = 1600, 900
BAR_H = 30
CHART = '{"type":"bar","title":"Weekly signups","x":"Week","y":"Signups","data":[{"x":"W1","y":120},{"x":"W2","y":180},{"x":"W3","y":240},{"x":"W4","y":310}]}'
REPLY = f"""## Signups are up 158%

Growth held steady across **four weeks**, compounding at about $r = \\sqrt[3]{{310/120}} - 1 \\approx 37\\%$ a week.

```chart
{CHART}
```

| Plan | Users | Change |
| :-- | --: | :-: |
| Free | 1,204 | +12% |
| Pro | 318 | +41% |

```python
def growth(values):
    return [b / a - 1 for a, b in zip(values, values[1:])]
```
"""


def pump(seconds=0.35):
    run_until(lambda: False, seconds)


def wallpaper(c):
    gradient = cairo.LinearGradient(0, 0, SCREEN_W, SCREEN_H)
    for stop, color in ((0, (0.11, 0.09, 0.2)), (0.55, (0.05, 0.13, 0.25)), (1, (0.04, 0.2, 0.26))):
        gradient.add_color_stop_rgb(stop, *color)
    c.set_source(gradient)
    c.paint()


def text(c, value, x, y, size=13, color=(1, 1, 1, 0.85)):
    layout = PangoCairo.create_layout(c)
    font = Pango.FontDescription("sans-serif")
    font.set_absolute_size(size * Pango.SCALE)
    layout.set_font_description(font)
    layout.set_text(value, -1)
    c.set_source_rgba(*color)
    c.move_to(x, y)
    PangoCairo.show_layout(c, layout)


def menu_bar(c, state):
    c.set_source_rgba(0.06, 0.06, 0.08, 0.96)
    c.rectangle(0, 0, SCREEN_W, BAR_H)
    c.fill()
    text(c, "1  2  3", 14, 7, 13, (1, 1, 1, 0.6))
    text(c, "Mon 21:48", SCREEN_W - 92, 7, 13)
    icon = render_icon(44, state, 0.15)
    c.save()
    c.translate(SCREEN_W - 132, 4)
    c.scale(0.5, 0.5)
    c.set_source_surface(icon, 0, 0)
    c.paint()
    c.restore()


def capture(window):
    gdk_window = window.get_window()
    pixbuf = Gdk.pixbuf_get_from_window(gdk_window, 0, 0, gdk_window.get_width(), gdk_window.get_height())
    return Gdk.cairo_surface_create_from_pixbuf(pixbuf, 1, None)


def rounded(c, x, y, w, h, r):
    c.new_sub_path()
    c.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    c.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    c.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    c.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    c.close_path()


def scene(name, panel, state, crop_bottom=30):
    """The panel, on a generated desktop under its menu bar icon, with the corners a compositor would give it."""
    shot = capture(panel.window)
    width, height = shot.get_width(), shot.get_height()
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, SCREEN_W, min(SCREEN_H, BAR_H + height + crop_bottom + 14))
    c = cairo.Context(surface)
    wallpaper(c)
    menu_bar(c, state)
    x, y = SCREEN_W - width - 16, BAR_H + 8
    rounded(c, x, y, width, height, 18)
    c.save()
    c.clip()
    c.set_source_surface(shot, x, y)
    c.paint()
    c.restore()
    surface.write_to_png(f"{OUT}/{name}.png")


def tray_strip():
    modes = [("idle", "Idle"), ("thinking", "Thinking"), ("listening", "Listening"), ("attention_reply", "New reply"), ("attention_input", "Needs you"), ("offline", "Offline")]
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 132 * len(modes), 124)
    c = cairo.Context(surface)
    rounded(c, 0, 0, surface.get_width(), 124, 16)
    c.set_source_rgb(0.07, 0.07, 0.09)
    c.fill()
    for i, (mode, title) in enumerate(modes):
        c.save()
        c.translate(38 + i * 132, 14)
        c.set_source_surface(render_icon(56, TrayState(mode=mode, usage=0.37), 0.15), 0, 0)
        c.paint()
        c.restore()
        layout = PangoCairo.create_layout(c)
        font = Pango.FontDescription("sans-serif")
        font.set_absolute_size(13 * Pango.SCALE)
        layout.set_font_description(font)
        layout.set_text(title, -1)
        c.set_source_rgba(1, 1, 1, 0.75)
        c.move_to(66 + i * 132 - layout.get_pixel_size()[0] / 2, 86)
        PangoCairo.show_layout(c, layout)
    surface.write_to_png(f"{OUT}/tray-states.png")


def main():
    install_css()
    app = AppModel()
    app.settings.set("agent_name", "Claw")
    gateway, session, voice, panel = app.gateway, app.session, app.voice, app.panel
    gateway.simulate_connected("2026.11.2")
    session.usage = UsageInfo("Codex", "plus", [UsageWindow("5h", 37), UsageWindow("Week", 64)])
    session.models = [ModelOption("openai-codex/terra-6.1", "Terra 6.1", "openai-codex")]
    session.session_settings.model = "terra-6.1"
    session.session_settings.model_provider = "openai-codex"
    session.session_settings.thinking_level = "high"
    session.session_settings.total_tokens = 62000
    session.session_settings.context_tokens = 200000
    now = time.time()
    session.items = [ChatItem("0", "user", "how are signups trending?", date=now), ChatItem("1", "assistant", REPLY, date=now, run_id="r")]
    panel.window.set_screen(Gdk.Screen.get_default())
    panel.pinned = True
    panel.expand(1500, 30, page="home")
    pump()
    idle = app._tray_state()
    scene("home", panel, idle)

    voice.phase, voice.transcript, voice.level = "listening", "summarize this page for me", 0.7
    app._sync()
    pump()
    scene("listening", panel, TrayState("🦞", "", "listening", 0.37))
    voice.phase, voice.level, voice.transcript = "idle", 0.0, ""

    session.progress = RunProgress(started_at=now - 12, steps=[Step("t", "Searching “signup growth benchmarks”", True)])
    session.activity = AgentActivity("thinking", "Searching")
    app.spoken_text = "how do these numbers compare with last quarter?"
    app._sync()
    pump()
    scene("thinking", panel, TrayState("🦞", "", "thinking", 0.37))
    session.activity = AgentActivity()
    session.progress = RunProgress()
    app.spoken_text = None

    panel.expand(page="chat")
    app._sync()
    pump(0.8)
    panel.conversation.scroller.get_vadjustment().set_value(0)
    pump(0.3)
    scene("chat", panel, idle)

    gateway.state = ConnectionState("offline", message="Gateway unreachable")
    app._sync()
    pump()
    panel.show_home()
    pump()
    scene("offline", panel, TrayState("🦞", "", "offline", 0.37))
    tray_strip()
    print("wrote screenshots to", OUT)


main()
