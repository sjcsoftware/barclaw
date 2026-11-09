// BarClaw's top bar button for GNOME Shell, which has no tray and which GTK can't add to.
// The app does all the work; this draws its state from dev.barclaw.Control, forwards clicks,
// and places the panel, which Wayland doesn't let a GTK window do for itself.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Meta from 'gi://Meta';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';

const APP_BUS = 'dev.barclaw.Barclaw';
const CONTROL_PATH = '/dev/barclaw/Control';
const CONTROL_IFACE = 'dev.barclaw.Control';
// Owning this name tells the app not to show a tray icon as well.
const HOST_BUS = 'dev.barclaw.Shell';
const PANEL_WM_INSTANCE = 'barclaw-panel';
const PANEL_GAP = 6;

const BarClawButton = GObject.registerClass(
class BarClawButton extends PanelMenu.Button {
    _init() {
        super._init(0.5, 'BarClaw', true);
        this.add_style_class_name('barclaw-button');
        const box = new St.BoxLayout({y_align: Clutter.ActorAlign.CENTER});
        this._emoji = new St.Label({text: '🦞', style_class: 'barclaw-emoji', y_align: Clutter.ActorAlign.CENTER});
        this._usage = new St.Label({style_class: 'barclaw-usage', y_align: Clutter.ActorAlign.CENTER, visible: false});
        box.add_child(this._emoji);
        box.add_child(this._usage);
        this.add_child(box);
        this.connect('button-press-event', (_actor, event) => this._pressed(event));
        this.connect('touch-event', (_actor, event) => {
            if (event.type() === Clutter.EventType.TOUCH_BEGIN)
                this.toggle();
            return Clutter.EVENT_PROPAGATE;
        });

        this._running = false;
        this._bus = Gio.DBus.session;
        this._watch = Gio.bus_watch_name(Gio.BusType.SESSION, APP_BUS, Gio.BusNameWatcherFlags.NONE,
            () => this._appeared(), () => this._vanished());
        this._signal = this._bus.signal_subscribe(APP_BUS, CONTROL_IFACE, 'Changed', CONTROL_PATH, null,
            Gio.DBusSignalFlags.NONE, () => this._refresh());
        this._windowCreated = global.display.connect('window-created', (_display, window) => this._placeIfPanel(window));
    }

    _pressed(event) {
        const button = event.get_button();
        if (button === 1)
            this.toggle();
        else if (button === 2)
            this._call('TalkStart');
        else if (button === 3)
            this._spawn('--settings');
        return Clutter.EVENT_STOP;
    }

    _anchor() {
        const [x, y] = this.get_transformed_position();
        const [width, height] = this.get_transformed_size();
        return [Math.round(x + width / 2), Math.round(y + height)];
    }

    toggle() {
        if (!this._running) {
            this._spawn();
            return;
        }
        const [x, y] = this._anchor();
        this._call('Toggle', new GLib.Variant('(ii)', [x, y]));
    }

    _spawn(...args) {
        try {
            // The shell's PATH often lacks ~/.local/bin, where the installer puts the launcher.
            const launcher = GLib.find_program_in_path('barclaw') ?? `${GLib.get_home_dir()}/.local/bin/barclaw`;
            Gio.Subprocess.new([launcher, ...args], Gio.SubprocessFlags.NONE);
        } catch (error) {
            Main.notify('BarClaw', 'The BarClaw app isn\'t installed.');
            console.error(error);
        }
    }

    _call(method, parameters = null) {
        this._bus.call(APP_BUS, CONTROL_PATH, CONTROL_IFACE, method, parameters, null,
            Gio.DBusCallFlags.NONE, 5000, null, (connection, result) => {
                try {
                    connection.call_finish(result);
                } catch (error) {
                    console.error(`BarClaw: ${method} failed: ${error.message}`);
                }
            });
    }

    _appeared() {
        this._running = true;
        this._refresh();
    }

    _vanished() {
        this._running = false;
        this._apply('🦞', 'offline', 'BarClaw isn\'t running. Click to start it.', -1);
    }

    _refresh() {
        this._bus.call(APP_BUS, CONTROL_PATH, 'org.freedesktop.DBus.Properties', 'GetAll',
            new GLib.Variant('(s)', [CONTROL_IFACE]), new GLib.VariantType('(a{sv})'),
            Gio.DBusCallFlags.NONE, 5000, null, (connection, result) => {
                try {
                    const [values] = connection.call_finish(result).recursiveUnpack();
                    this._apply(values.Emoji, values.Mode, values.Tooltip, values.UsagePercent);
                } catch (error) {
                    console.error(`BarClaw: couldn't read state: ${error.message}`);
                }
            });
    }

    _apply(emoji, mode, tooltip, usage) {
        this._emoji.text = emoji;
        for (const name of ['offline', 'thinking', 'listening', 'attention'])
            this.remove_style_class_name(`barclaw-${name}`);
        const group = mode.startsWith('attention') ? 'attention' : mode;
        if (group !== 'idle')
            this.add_style_class_name(`barclaw-${group}`);
        this.set_accessible_name(tooltip);
        this._usage.visible = usage >= 0;
        if (usage >= 0)
            this._usage.text = `${usage}%`;
    }

    // Wayland clients can't position their own windows, so the shell does it for the panel.
    _placeIfPanel(window) {
        const place = () => {
            if (window.get_wm_class_instance?.() !== PANEL_WM_INSTANCE && window.get_title() !== 'BarClaw')
                return GLib.SOURCE_REMOVE;
            if (window.get_window_type() !== Meta.WindowType.NORMAL && window.get_window_type() !== Meta.WindowType.UTILITY)
                return GLib.SOURCE_REMOVE;
            const [anchorX, anchorY] = this._anchor();
            const frame = window.get_frame_rect();
            const area = window.get_work_area_current_monitor();
            const x = Math.max(area.x + PANEL_GAP, Math.min(anchorX - Math.round(frame.width / 2), area.x + area.width - frame.width - PANEL_GAP));
            window.move_frame(true, x, Math.max(area.y + PANEL_GAP, anchorY + PANEL_GAP));
            return GLib.SOURCE_REMOVE;
        };
        const actor = window.get_compositor_private();
        if (!actor)
            return;
        const handler = actor.connect('first-frame', () => {
            actor.disconnect(handler);
            place();
        });
    }

    destroy() {
        Gio.bus_unwatch_name(this._watch);
        this._bus.signal_unsubscribe(this._signal);
        global.display.disconnect(this._windowCreated);
        super.destroy();
    }
});

export default class BarClawExtension extends Extension {
    enable() {
        this._button = new BarClawButton();
        Main.panel.addToStatusArea('barclaw', this._button);
        this._host = Gio.bus_own_name(Gio.BusType.SESSION, HOST_BUS, Gio.BusNameOwnerFlags.NONE, null, null, null);
    }

    disable() {
        Gio.bus_unown_name(this._host);
        this._host = null;
        this._button.destroy();
        this._button = null;
    }
}
