"""The panel's look: BarClaw's neon palette and its GTK stylesheet."""
from __future__ import annotations

from gi.repository import Gdk, Gtk

# Hot pink, violet, blue, cyan, amber.
SIRI = [(1.00, 0.18, 0.58), (0.66, 0.33, 0.97), (0.23, 0.51, 0.96), (0.13, 0.83, 0.93), (1.00, 0.62, 0.04)]

CSS = b"""
.barclaw-window, .barclaw-window * { font-size: 13px; }
window.barclaw-window { background-color: transparent; }
window.barclaw-opaque { background-color: #111216; }
.panel-surface {
  background-color: #111216;
  border-radius: 18px;
  border: 1px solid rgba(255,255,255,0.10);
  color: rgba(255,255,255,0.92);
}
window.barclaw-opaque .panel-surface { border-radius: 0; }
.panel-surface label { color: rgba(255,255,255,0.92); }
.panel-surface label.dim, label.dim { color: rgba(255,255,255,0.55); }
.panel-surface label.faint, label.faint { color: rgba(255,255,255,0.38); font-size: 11px; }
.panel-surface scrollbar, .panel-surface scrollbar slider { background: transparent; border: none; }
.panel-surface scrollbar slider { background-color: rgba(255,255,255,0.18); border-radius: 4px; min-width: 4px; min-height: 4px; }
.panel-surface scrollbar trough { background: transparent; border: none; }
.panel-surface selection, .panel-surface *:selected { background-color: rgba(168,84,247,0.55); color: #fff; }
.panel-surface scrolledwindow, .panel-surface viewport { background: transparent; border: none; }
.panel-surface undershoot, .panel-surface overshoot { background: none; border: none; }

button.pill {
  background-image: none; background-color: rgba(255,255,255,0.065);
  border: 1px solid rgba(255,255,255,0.10); border-radius: 10px;
  color: rgba(255,255,255,0.92); padding: 0 10px; min-height: 30px; box-shadow: none; text-shadow: none;
}
button.pill:hover { background-color: rgba(255,255,255,0.12); }
button.pill:active { background-color: rgba(255,255,255,0.18); }
button.pill:disabled { opacity: 0.4; }
button.pill.prominent { background-image: linear-gradient(135deg, rgba(168,84,247,0.62), rgba(59,130,245,0.62)); border-color: rgba(255,255,255,0.18); }
button.pill.prominent:hover { background-image: linear-gradient(135deg, rgba(168,84,247,0.85), rgba(59,130,245,0.85)); }
button.pill .badge { background-color: rgba(255,255,255,0.22); border-radius: 8px; padding: 0 5px; font-size: 10px; font-weight: bold; color: #fff; }

button.chip, button.chip-flat {
  background-image: none; background-color: rgba(255,255,255,0.08); border: none; border-radius: 8px;
  color: rgba(255,255,255,0.8); padding: 0 8px; min-height: 22px; min-width: 0; font-size: 11px; box-shadow: none; text-shadow: none;
}
button.chip-flat { background-color: transparent; color: rgba(255,255,255,0.5); }
button.chip:hover, button.chip-flat:hover { background-color: rgba(255,255,255,0.16); color: #fff; }
button.round {
  background-image: none; background-color: rgba(255,255,255,0.08); border: none; border-radius: 12px;
  min-width: 24px; min-height: 24px; padding: 0; color: rgba(255,255,255,0.8); box-shadow: none; text-shadow: none;
}
button.round:hover { background-color: rgba(255,255,255,0.18); }
button.round.active, button.chip.active { background-color: rgba(168,84,247,0.6); color: #fff; }
button.send {
  background-image: linear-gradient(135deg, #a854f7, #3b82f6); border: none; border-radius: 15px;
  min-width: 30px; min-height: 30px; padding: 0; color: #fff; box-shadow: none; text-shadow: none;
}
button.send.stop { background-image: none; background-color: rgba(255,255,255,0.18); }

.model-tag { background-color: rgba(255,255,255,0.06); border-radius: 10px; padding: 0 8px; min-height: 20px; color: rgba(255,255,255,0.65); border: none; background-image: none; box-shadow: none; text-shadow: none; font-size: 11px; }
.model-tag:hover { background-color: rgba(255,255,255,0.12); }
.agent-title { font-weight: 600; }
.banner { background-color: rgba(255,255,255,0.06); border-radius: 12px; padding: 8px 10px; }
.card { background-color: rgba(255,255,255,0.06); border-radius: 12px; padding: 10px; border: 1px solid rgba(168,84,247,0.5); }
.command { font-family: monospace; font-size: 11px; background-color: rgba(0,0,0,0.4); border-radius: 6px; padding: 4px 8px; color: #ffd7a8; }
.bubble { background-color: rgba(255,255,255,0.12); border-radius: 14px; padding: 8px 13px; border: 1px solid rgba(255,255,255,0.1); }
.bubble.listening { border-color: rgba(168,84,247,0.9); }
.toast { background-color: rgba(30,31,38,0.97); border-radius: 12px; padding: 5px 12px; border: 1px solid rgba(255,255,255,0.14); font-size: 12px; }

.msg-user { background-image: linear-gradient(135deg, rgba(168,84,247,0.45), rgba(59,130,245,0.4)); border-radius: 13px; padding: 7px 11px; }
.msg-error { background-color: rgba(255,80,80,0.14); border-radius: 10px; padding: 7px 10px; }
.chip-screen { background-color: rgba(255,255,255,0.07); border-radius: 8px; padding: 1px 7px; font-size: 10px; }
.progress-card { background-color: rgba(255,255,255,0.04); border-radius: 10px; padding: 8px 10px; }

.composer { background-color: rgba(255,255,255,0.07); border-radius: 15px; padding: 5px 6px 5px 10px; border: 1px solid rgba(255,255,255,0.09); }
.composer textview, .composer textview text { background-color: transparent; color: rgba(255,255,255,0.95); caret-color: #c4a7ff; }

.md-text { color: rgba(255,255,255,0.92); }
.md-code { background-color: rgba(0,0,0,0.38); border-radius: 10px; border: 1px solid rgba(255,255,255,0.07); }
.md-code-header { padding: 3px 4px 3px 10px; border-bottom: 1px solid rgba(255,255,255,0.05); }
.md-code-body { padding: 8px 10px; color: #e8ebf0; }
.md-card { background-color: rgba(255,255,255,0.05); border-radius: 12px; padding: 12px; border: 1px solid rgba(255,255,255,0.08); }
.md-table { border: 1px solid rgba(255,255,255,0.10); border-radius: 8px; }
.md-th { background-color: rgba(255,255,255,0.07); padding: 5px 9px; }
.md-td { padding: 5px 9px; border-top: 1px solid rgba(255,255,255,0.06); }
.md-quote { border-left: 3px solid rgba(168,84,247,0.7); padding-left: 10px; }
.md-rule { background-color: rgba(255,255,255,0.12); min-height: 1px; }
"""


def install_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
