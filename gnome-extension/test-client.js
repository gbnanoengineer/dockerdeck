// Run: gjs -m test-client.js [url]
import GLib from 'gi://GLib';
import {DockerDeckClient} from './dockerdeck@gbnanoengineer.github.io/api.js';

const url = ARGV[0] ?? 'http://127.0.0.1:8787';
const loop = new GLib.MainLoop(null, false);
const client = new DockerDeckClient(url);
let failed = false;

try {
    const s = await client.summary();
    print(`ok   ${url}: RAM ${s.ram_pct}%  swap ${s.swap_pct}%  ${s.running}/${s.total} running  problems: ${s.problems.length}`);
} catch (e) {
    print(`FAIL ${url}: ${e.message}`);
    failed = true;
}
client.destroy();
imports.system.exit(failed ? 1 : 0);
