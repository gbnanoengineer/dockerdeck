// Run: gjs -m test-service.js   (needs ./install.sh to have created the service)
import GLib from 'gi://GLib';
import {DockerDeckClient, controlService} from './dockerdeck@gbnanoengineer.github.io/api.js';

const client = new DockerDeckClient('http://127.0.0.1:8787');
const sleep = s => new Promise(r => GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, s, () => { r(); return GLib.SOURCE_REMOVE; }));
const up = async () => { try { await client.summary(); return true; } catch { return false; } };
let bad = 0;
const check = (name, ok) => { print(`${ok ? 'ok  ' : 'FAIL'} ${name}`); if (!ok) bad++; };

await controlService('start');   await sleep(2);  check('start -> server answers', await up());
await controlService('restart'); await sleep(2);  check('restart -> server answers', await up());
await controlService('stop');    await sleep(1);  check('stop -> server is gone', !(await up()));
try { await controlService('rm -rf'); check('bad action rejected', false); } catch { check('bad action rejected', true); }

client.destroy();
imports.system.exit(bad ? 1 : 0);
