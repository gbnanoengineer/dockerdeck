// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Gaurav Kumar
//
// Tiny client for the DockerDeck server.
// It has no GNOME Shell imports, so it can be tested with plain `gjs -m`.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Soup from 'gi://Soup?version=3.0';

export class DockerDeckClient {
    constructor(baseUrl) {
        this._base = baseUrl.replace(/\/+$/, '');
        this._cancellable = new Gio.Cancellable();
        this._session = new Soup.Session({timeout: 4, idle_timeout: 4});
    }

    summary() {
        return new Promise((resolve, reject) => {
            const msg = Soup.Message.new('GET', `${this._base}/api/summary`);
            this._session.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, this._cancellable, (session, res) => {
                try {
                    const bytes = session.send_and_read_finish(res);
                    if (msg.get_status() !== Soup.Status.OK)
                        throw new Error(`HTTP ${msg.get_status()}`);
                    resolve(JSON.parse(new TextDecoder().decode(bytes.get_data())));
                } catch (e) {
                    reject(e);
                }
            });
        });
    }

    destroy() {
        this._cancellable.cancel();
        this._session.abort();
        this._session = null;
    }
}

const SERVICE = 'dockerdeck.service';
const ACTIONS = ['start', 'stop', 'restart'];

/**
 * Start, stop or restart the DockerDeck systemd user service.
 * The command is fixed. Nothing from settings is ever run.
 */
export function controlService(action, cancellable = null) {
    if (!ACTIONS.includes(action))
        return Promise.reject(new Error(`bad action: ${action}`));
    return new Promise((resolve, reject) => {
        try {
            const proc = Gio.Subprocess.new(
                ['systemctl', '--user', action, SERVICE],
                Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE);
            proc.communicate_utf8_async(null, cancellable, (p, res) => {
                try {
                    const [, , err] = p.communicate_utf8_finish(res);
                    if (p.get_successful())
                        resolve();
                    else
                        reject(new Error((err ?? '').trim() || `systemctl ${action} failed`));
                } catch (e) {
                    reject(e);
                }
            });
        } catch (e) {
            reject(e);
        }
    });
}
