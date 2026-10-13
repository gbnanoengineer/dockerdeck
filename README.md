<div align="center">

# DockerDeck

**See what your containers are doing. Find out why they break. Fix it.**

A small local dashboard for Docker on Linux.
No install, no dependencies, about 30 MB of RAM.

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-none-2ea44f)
![Platform](https://img.shields.io/badge/platform-linux-informational)
![License](https://img.shields.io/badge/license-AGPL--3.0-blue)

</div>

---

## Why DockerDeck?

Most Docker tools show you a list of containers. When one breaks, you still do the work by hand:
read logs, check the exit code, check health, check the database, check memory.

DockerDeck does that checking for you.
Click the bug icon on any container. It tells you **what is wrong, why, and what to try**.

```
Something looks wrong
  Permission denied

  Why:        The app is not allowed to use a file, folder or socket.
  What to try:
    - Check who owns the mounted folder:   [ ls -ln <path> ]   (copy)
    - Run the container as the right user, or fix the folder owner.
  What the logs say:
    failed to write /home/agent/.pi/agent/settings.json: EACCES: permission denied
```

It also looks at your **whole machine**, so you can tell when a container is slow
because the host ran out of RAM, not because of a bug.

## Features

### Debug a container

| It checks | What you learn |
|---|---|
| Exit code | What `137`, `139`, `143`, `127` and others really mean |
| OOM flag | Was it killed for using too much memory? |
| Host OOM killer | Did the Linux kernel kill it? (matches the kernel log by time) |
| Crash loop | Restart count and the error behind each restart |
| Health checks | Last results and the command that fails |
| Dependencies | Does it wait on a service that is down or unhealthy? |
| Docker events | What happened in the last 24 hours (die, kill, oom, restart) |
| Log patterns | 15+ known problems, each with a fix (see below) |
| Live numbers | CPU, memory, and how close it is to its limit |

**Run a check** inside a running container, with one click:
processes, disk space, env variable names (never values), DNS, open ports,
and a lookup of each service it depends on. Or type your own command.

### Log patterns it knows

Port in use, disk full, out of memory, wrong CPU type, missing code package,
missing database tables, login failed, DNS failure, connection refused,
permission denied, TLS errors, file not found, missing setting, timeouts,
HTTP 5xx errors, and crash traces.

Lines logged as `info` or `debug` are ignored, so normal start-up text does not raise false alarms.

### Watch your machine

- RAM, swap, CPU and disk gauges, plus RAM history
- Memory pressure (how long apps wait for memory) and swappiness
- Top programs by memory, grouped (Chrome with 12 processes shows as one line)
- Out-of-memory kills from the last 7 days
- Tips in plain words, like "this container is idle but holds 900 MB"
- Optional desktop alert when RAM or swap runs low

### Manage everything

- **Containers**: start, stop, restart, remove. Grouped by compose project, with start-all and stop-all.
- **Live logs**: follow, filter, pause, wrap, download. Errors and warnings are colored.
- **Details**: env (secrets hidden until you click), mounts, networks, ports, limits, labels.
- **Images, volumes, networks**: sizes, what uses them, remove.
- **Cleanup**: preview first, then delete. Named volumes are never touched.
- **Contexts**: switch between any `docker context`. Dead ones show as not reachable.
- **Ctrl+K** to jump to anything. **/** to filter containers.
- Dark and light theme.

## Quick start

You need Linux, Python 3.11+, and a running Docker Engine (25 or newer).

```bash
git clone https://github.com/gbnanoengineer/dockerdeck.git
cd dockerdeck
./run.sh
```

Open **http://127.0.0.1:8787**

Your user must be able to use Docker (member of the `docker` group).
To use another port: `PORT=9000 ./run.sh`

### Start on login (optional)

Create `~/.config/systemd/user/dockerdeck.service`:

```ini
[Unit]
Description=DockerDeck

[Service]
ExecStart=/usr/bin/python3 %h/dockerdeck/app.py
Restart=on-failure

[Install]
WantedBy=default.target
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now dockerdeck
```

## Light on RAM

DockerDeck is meant to run all day on a busy machine.

- **Standard library only.** No web framework, no Docker SDK.
- **No background work.** It does nothing until the browser asks.
- **The page stops polling** when its tab is hidden.
- **One short thread per request.** Closing a log view frees its stream.
- **Shared samples.** Many open tabs share one stats sample for a few seconds.
- Measured at about 25 to 35 MB while in use, on a machine with 20 containers.

## How it works

```
 Browser  ──►  DockerDeck (Python, 127.0.0.1:8787)  ──►  Docker Engine
 one HTML                │                                unix socket
 file, no build          ├──►  /proc  (RAM, swap, CPU, pressure, top programs)
                         └──►  journalctl  (kernel OOM kills, cached 60 s)
```

- `app.py` is the whole backend. It talks to Docker's HTTP API over its socket.
- `static/index.html` is the whole frontend. Plain JavaScript, no libraries, no build step.
- Everything the diagnosis says comes from rules in `app.py` (`PATTERNS`, `EXIT_HINTS`).
  To teach it a new problem, add one entry.

<details>
<summary><b>API reference</b></summary>

All calls take `?ctx=<docker context>` (default: `default`).
Anything other than GET needs the header `X-DockerDeck: 1`.

| Method | Path | Does |
|---|---|---|
| GET | `/api/contexts` | List contexts and whether each is reachable |
| GET | `/api/overview` | Containers and engine info |
| GET | `/api/stats` | CPU, memory, network, pids per running container |
| GET | `/api/system` | RAM, swap, CPU, disk, pressure (`?top=1` adds programs and OOM kills) |
| GET | `/api/diagnose/{id}` | Full diagnosis with findings and fixes |
| GET | `/api/inspect/{id}` | Trimmed container details |
| GET | `/api/logs/{id}` | Recent logs |
| GET | `/api/logs/{id}/stream` | Live logs (server-sent events) |
| POST | `/api/exec/{id}` | Run a command inside a container (`{"cmd": "..."}`) |
| POST | `/api/containers/{id}/{action}` | start, stop, restart, pause, unpause, kill, remove |
| POST | `/api/projects/{name}/{action}` | start, stop, restart a compose project |
| GET | `/api/images`, `/api/volumes`, `/api/networks` | Lists |
| GET / POST | `/api/cleanup/scan`, `/api/cleanup/run` | Preview and run cleanup |

</details>

## Safety

Docker access is root-level. DockerDeck is built to stay on your own machine.

- Listens on **127.0.0.1 only**.
- Rejects any request with a different `Host` header (blocks DNS rebinding).
- Changes need a custom header, so other websites cannot trigger them.
- Cleanup shows a preview and asks before it deletes. It never removes named volumes.
- Env values that look like secrets are hidden until you click them.
- **Run a check** can run any command in your containers. Do not expose this port to a network.

If you put it behind a reverse proxy, add your own login first.

## Limits

- Linux only. It reads `/proc` for host numbers.
- Connects over `unix://` and `tcp://`. `ssh://` contexts are not supported yet.
- Needs Docker API 1.44 or newer.
- Tested on Ubuntu with Docker 29 and Python 3.14.
- Diagnosis is rule-based. It finds known problems well, and says "no problems found"
  when it does not know. It is a fast first look, not a replacement for reading the logs.

## Troubleshooting

| Problem | Try |
|---|---|
| "No reachable Docker context" | Is Docker running? `systemctl status docker` |
| Permission denied on the socket | Add yourself to the group: `sudo usermod -aG docker $USER`, then log in again |
| No OOM kills shown | Your user may not read the kernel log. Add yourself to `adm` or `systemd-journal` |
| Port 8787 in use | `PORT=9000 ./run.sh` |

## Ideas for later

- `ssh://` contexts
- Compose file view and `up` / `down`
- Custom rules in a config file
- Save history to disk for longer charts

## Contributing

Issues and pull requests are welcome.
Keep it small and dependency-free. That is the point.
By contributing, you agree your work is released under AGPL-3.0.

## License

**AGPL-3.0.** DockerDeck is free and open source, and it stays that way.

- You can use, study, change and share it, for free.
- If you share a changed version, or run one as a service for other people,
  you must publish your source under the same license.
- You cannot take it, close it, and sell it.

Running it on your own machines does not make your own projects open source.
Only changes to DockerDeck itself must be shared.

See [LICENSE](LICENSE) for the full text.
