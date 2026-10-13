// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Gaurav Kumar
//
// Tiny client for the DockerDeck server.
// It has no GNOME Shell imports, so it can be tested with plain `gjs -m`.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Soup from 'gi://Soup?version=3.0';

Gio._promisify(Soup.Session.prototype, 'send_and_read_async', 'send_and_read_finish');

export class DockerDeckClient {
    constructor(baseUrl) {
        this._base = baseUrl.replace(/\/+$/, '');
        this._cancellable = new Gio.Cancellable();
        this._session = new Soup.Session({timeout: 4, idle_timeout: 4});
    }

    async summary() {
        const msg = Soup.Message.new('GET', `${this._base}/api/summary`);
        const bytes = await this._session.send_and_read_async(
            msg, GLib.PRIORITY_DEFAULT, this._cancellable);
        if (msg.get_status() !== Soup.Status.OK)
            throw new Error(`HTTP ${msg.get_status()}`);
        return JSON.parse(new TextDecoder().decode(bytes.get_data()));
    }

    destroy() {
        this._cancellable.cancel();
        this._session.abort();
        this._session = null;
    }
}
