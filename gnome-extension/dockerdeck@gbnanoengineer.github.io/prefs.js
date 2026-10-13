// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Gaurav Kumar

import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

export default class DockerDeckPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();
        const page = new Adw.PreferencesPage({
            title: 'DockerDeck',
            icon_name: 'preferences-system-symbolic',
        });
        window.add(page);

        const connection = new Adw.PreferencesGroup({
            title: 'Connection',
            description: 'The extension reads from the DockerDeck server running on your machine.',
        });
        page.add(connection);

        const url = new Adw.EntryRow({title: 'DockerDeck address'});
        settings.bind('url', url, 'text', Gio.SettingsBindFlags.DEFAULT);
        connection.add(url);

        const interval = new Adw.SpinRow({
            title: 'Refresh every (seconds)',
            subtitle: 'Higher uses less CPU. It slows down by itself when the server is off.',
            adjustment: new Gtk.Adjustment({lower: 2, upper: 300, step_increment: 1, page_increment: 10}),
        });
        settings.bind('interval', interval, 'value', Gio.SettingsBindFlags.DEFAULT);
        connection.add(interval);

        const look = new Adw.PreferencesGroup({title: 'Top bar'});
        page.add(look);

        const label = new Adw.SwitchRow({title: 'Show RAM percent next to the icon'});
        settings.bind('show-label', label, 'active', Gio.SettingsBindFlags.DEFAULT);
        look.add(label);

        const notify = new Adw.SwitchRow({
            title: 'Notify when a container starts failing',
            subtitle: 'Restart loops, failed health checks and crashes.',
        });
        settings.bind('notify', notify, 'active', Gio.SettingsBindFlags.DEFAULT);
        look.add(notify);
    }
}
