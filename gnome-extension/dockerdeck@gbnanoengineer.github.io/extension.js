// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Gaurav Kumar

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {DockerDeckClient} from './api.js';

const OFFLINE_DELAY = 30;  // seconds. Ask less often when the server is off.

const Indicator = GObject.registerClass(
class DockerDeckIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.0, 'DockerDeck');
        this._ext = extension;
        this._settings = extension.getSettings();
        this._client = null;
        this._timeoutId = 0;
        this._summary = null;
        this._online = false;
        this._problemKey = '';
        this._seen = new Set();
        this._firstRun = true;

        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        this._icon = new St.Icon({
            gicon: Gio.icon_new_for_string(`${extension.path}/icons/dockerdeck-symbolic.svg`),
            style_class: 'system-status-icon',
        });
        this._label = new St.Label({
            style_class: 'dockerdeck-label',
            y_align: Clutter.ActorAlign.CENTER,
        });
        box.add_child(this._icon);
        box.add_child(this._label);
        this.add_child(box);

        this._buildMenu();
        this._connectClient();

        this._settingsIds = [
            this._settings.connect('changed::url', () => {
                this._connectClient();
                this._tick();
            }),
            this._settings.connect('changed::interval', () => this._schedule(true)),
            this._settings.connect('changed::show-label', () => this._render()),
        ];
        // Refresh the moment the menu opens, so numbers are fresh.
        this._openId = this.menu.connect('open-state-changed', (_m, open) => {
            if (open)
                this._tick();
        });

        this._render();
        this._tick();
    }

    _buildMenu() {
        this._ramItem = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
        this._dockerItem = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
        this.menu.addMenuItem(this._ramItem);
        this.menu.addMenuItem(this._dockerItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._problems = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._problems);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this.menu.addAction('Open dashboard', () => this._open(''));
        this.menu.addAction('Problems', () => this._open('#problems'));
        this.menu.addAction('Compose projects', () => this._open('#compose'));
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction('Settings', () => this._ext.openPreferences());
    }

    _connectClient() {
        this._client?.destroy();
        this._client = new DockerDeckClient(this._settings.get_string('url'));
    }

    _baseUrl() {
        return this._settings.get_string('url').replace(/\/+$/, '');
    }

    _open(hash) {
        try {
            Gio.AppInfo.launch_default_for_uri(
                `${this._baseUrl()}/${hash}`, global.create_app_launch_context(0, -1));
        } catch (e) {
            Main.notify('DockerDeck', `Could not open the browser: ${e.message}`);
        }
    }

    async _tick() {
        if (!this._client)
            return;
        if (Main.sessionMode.isLocked) {
            this._schedule();
            return;
        }
        try {
            this._summary = await this._client.summary();
            this._online = true;
        } catch (e) {
            if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                return;
            this._summary = null;
            this._online = false;
        }
        if (!this._client)
            return;  // extension was disabled while waiting
        this._notifyNew();
        this._render();
        this._schedule();
    }

    _schedule(restart = false) {
        if (this._timeoutId && !restart)
            return;
        if (this._timeoutId)
            GLib.source_remove(this._timeoutId);
        const every = this._online ? this._settings.get_int('interval') : OFFLINE_DELAY;
        this._timeoutId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, every, () => {
            this._timeoutId = 0;
            this._tick();
            return GLib.SOURCE_REMOVE;
        });
    }

    _notifyNew() {
        const now = new Set((this._summary?.problems ?? []).map(p => `${p.name}:${p.reason}`));
        // Do not notify for problems that already existed when the shell started.
        if (!this._firstRun && this._settings.get_boolean('notify')) {
            for (const p of this._summary?.problems ?? []) {
                if (!this._seen.has(`${p.name}:${p.reason}`))
                    Main.notify('DockerDeck', `${p.name}: ${p.reason}`);
            }
        }
        this._seen = now;
        if (this._online)
            this._firstRun = false;
    }

    _render() {
        const s = this._summary;
        this._label.visible = this._settings.get_boolean('show-label');
        this._icon.remove_style_class_name('dockerdeck-offline');
        for (const c of ['dockerdeck-warn', 'dockerdeck-bad'])
            this._label.remove_style_class_name(c);

        if (!s) {
            this._label.text = '';
            this._icon.add_style_class_name('dockerdeck-offline');
            this._ramItem.label.text = 'DockerDeck is not running';
            this._dockerItem.label.text = 'Run ./run.sh in the dockerdeck folder, or check Settings';
            this._setProblems([]);
            return;
        }

        const bad = s.problems.length > 0;
        const hot = s.ram_pct >= 85 || s.swap_pct >= 80;
        this._label.text = `${Math.round(s.ram_pct)}%${bad ? ` ⚠${s.problems.length}` : ''}`;
        if (bad)
            this._label.add_style_class_name('dockerdeck-bad');
        else if (hot)
            this._label.add_style_class_name('dockerdeck-warn');

        const gb = n => (n / 1073741824).toFixed(1);
        this._ramItem.label.text =
            `RAM ${Math.round(s.ram_pct)}% (${gb(s.ram_used)} of ${gb(s.ram_total)} GB)  ·  Swap ${Math.round(s.swap_pct)}%`;
        this._dockerItem.label.text = `${s.running} of ${s.total} containers running`;
        this._setProblems(s.problems);
    }

    _setProblems(list) {
        const key = JSON.stringify(list);
        if (key === this._problemKey)
            return;  // same list: do not rebuild, so an open menu does not flicker
        this._problemKey = key;
        this._problems.removeAll();
        if (!list.length) {
            this._problems.addMenuItem(new PopupMenu.PopupMenuItem(
                this._online ? 'No problems found' : '—', {reactive: false, can_focus: false}));
            return;
        }
        for (const p of list.slice(0, 8)) {
            const item = new PopupMenu.PopupMenuItem(`⚠  ${p.name}  ·  ${p.reason}`);
            item.connect('activate', () => this._open(`#debug/${encodeURIComponent(p.name)}`));
            this._problems.addMenuItem(item);
        }
        if (list.length > 8) {
            this._problems.addMenuItem(new PopupMenu.PopupMenuItem(
                `and ${list.length - 8} more...`, {reactive: false, can_focus: false}));
        }
    }

    destroy() {
        for (const id of this._settingsIds ?? [])
            this._settings.disconnect(id);
        this._settingsIds = [];
        if (this._openId)
            this.menu.disconnect(this._openId);
        if (this._timeoutId) {
            GLib.source_remove(this._timeoutId);
            this._timeoutId = 0;
        }
        this._client?.destroy();
        this._client = null;
        this._settings = null;

        this._ramItem?.destroy();
        this._ramItem = null;
        this._dockerItem?.destroy();
        this._dockerItem = null;
        this._problems?.destroy();
        this._problems = null;
        this._label?.destroy();
        this._label = null;
        this._icon?.destroy();
        this._icon = null;
        super.destroy();
    }
});

export default class DockerDeckExtension extends Extension {
    enable() {
        this._indicator = new Indicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
