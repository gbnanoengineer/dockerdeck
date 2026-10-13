# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Gaurav Kumar
"""DockerDeck: tiny local dashboard for Docker.

Standard library only. No background work: it only does something
when the browser asks. Idle RAM stays small.
"""
import http.client
import json
import os
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

API = "/v1.44"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
ACTIONS = {"start", "stop", "restart", "pause", "unpause", "kill"}
INDEX = Path(__file__).parent / "static" / "index.html"
STREAMS = threading.BoundedSemaphore(6)
DONE = object()
ROUTES = []


class DockerError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


# ---------- docker over a socket ----------

class UnixConn(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("docker", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def connect(endpoint, timeout=15):
    if endpoint.startswith("unix://"):
        return UnixConn(endpoint[7:], timeout)
    if endpoint.startswith("tcp://"):
        host, _, port = endpoint[6:].partition(":")
        return http.client.HTTPConnection(host, int(port or 2375), timeout=timeout)
    raise DockerError(400, f"unsupported endpoint: {endpoint.split(':')[0]}")


def contexts():
    home = Path.home() / ".docker"
    rows = [{"name": "default", "endpoint": os.environ.get("DOCKER_HOST") or "unix:///var/run/docker.sock"}]
    for meta in sorted(home.glob("contexts/meta/*/meta.json")):
        try:
            d = json.loads(meta.read_text())
            rows.append({"name": d["Name"], "endpoint": d["Endpoints"]["docker"]["Host"]})
        except (OSError, KeyError, ValueError):
            continue
    try:
        current = json.loads((home / "config.json").read_text()).get("currentContext", "default")
    except (OSError, ValueError):
        current = "default"
    current = os.environ.get("DOCKER_CONTEXT", current)
    for r in rows:
        r["current"] = r["name"] == current
    return rows


def endpoint(name):
    for c in contexts():
        if c["name"] == name:
            return c["endpoint"]
    raise DockerError(404, f"unknown context: {name}")


def dk(ctx, method, path, body=None, timeout=15, text=False):
    c = connect(endpoint(ctx), timeout)
    try:
        data, hdr = None, {}
        if body is not None:
            data, hdr = json.dumps(body).encode(), {"Content-Type": "application/json"}
        c.request(method, API + path, data, hdr)
        r = c.getresponse()
        raw = r.read()
    except (OSError, http.client.HTTPException) as e:
        raise DockerError(503, f"cannot reach '{ctx}': {e}")
    finally:
        c.close()
    if r.status >= 400:
        try:
            msg = json.loads(raw).get("message")
        except ValueError:
            msg = raw.decode(errors="replace")
        raise DockerError(r.status, msg or r.reason)
    if text:
        return raw
    return json.loads(raw) if raw else {}


def pmap(fn, items):
    """Run fn on every item in its own thread. Errors become None."""
    out = [None] * len(items)

    def run(i, x):
        try:
            out[i] = fn(x)
        except Exception:
            out[i] = None

    threads = [threading.Thread(target=run, args=(i, x)) for i, x in enumerate(items)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def demux(raw: bytes, tty: bool) -> str:
    if tty:
        return raw.decode(errors="replace")
    out, i = [], 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    return b"".join(out).decode(errors="replace")


# ---------- host info from /proc ----------

def meminfo():
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            d[k] = int(v.split()[0]) * 1024
    return d


def pressure(kind):
    try:
        text = Path(f"/proc/pressure/{kind}").read_text()
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        parts = line.split()
        out[parts[0]] = {k: float(v) for k, v in (p.split("=") for p in parts[1:]) if k.startswith("avg")}
    return out


_cpu = {"prev": None}


def cpu_percent():
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:]]
    idle, total = vals[3] + vals[4], sum(vals)
    prev, _cpu["prev"] = _cpu["prev"], (idle, total)
    if not prev or total == prev[1]:
        return 0.0
    return round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1)


def top_programs():
    groups = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        name = rss = None
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("Name:"):
                        name = line.split(None, 1)[1].strip()
                    elif line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) * 1024
                        break
        except OSError:
            continue
        if rss is None:
            continue
        g = groups.setdefault(name, [0, 0])
        g[0] += rss
        g[1] += 1
    top = sorted(groups.items(), key=lambda kv: kv[1][0], reverse=True)[:12]
    return [{"name": n, "rss": v[0], "count": v[1]} for n, v in top]


_oom = {"at": 0, "rows": []}


def oom_kills():
    if time.time() - _oom["at"] < 60:
        return _oom["rows"]
    rows = []
    try:
        out = subprocess.run(
            ["journalctl", "-k", "--since", "7 days ago", "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=8,
        ).stdout
        for line in out.splitlines():
            m = re.search(r"Out of memory: Killed process (\d+) \((.+?)\)", line)
            if m:
                rows.append({"time": line.split()[0], "name": m.group(2)})
    except (OSError, subprocess.SubprocessError):
        pass
    _oom.update(at=time.time(), rows=rows[-10:][::-1])
    return _oom["rows"]


# ---------- routes ----------

def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile(pattern), fn))
        return fn
    return deco


def ctx_of(qs):
    return qs.get("ctx") or "default"


@route("GET", r"/api/contexts")
def r_contexts(h, qs, body):
    def ping(c):
        try:
            k = connect(c["endpoint"], 2)
            k.request("GET", "/_ping")
            ok = k.getresponse().status == 200
            k.close()
        except (OSError, http.client.HTTPException, DockerError):
            ok = False
        return {**c, "reachable": ok}

    return pmap(ping, contexts())


def short_row(c):
    status = c.get("Status", "")
    m = re.search(r"\((healthy|unhealthy|health: starting)\)", status)
    labels = c.get("Labels") or {}
    ports = sorted({f'{p["PublicPort"]}->{p["PrivatePort"]}' for p in c.get("Ports", []) if p.get("PublicPort")})
    return {
        "id": c["Id"][:12],
        "name": (c.get("Names") or ["/?"])[0].lstrip("/"),
        "image": c.get("Image", ""),
        "image_id": c.get("ImageID", ""),
        "state": c.get("State", ""),
        "status": status,
        "health": (m.group(1).replace("health: ", "") if m else ""),
        "project": labels.get("com.docker.compose.project", ""),
        "service": labels.get("com.docker.compose.service", ""),
        "ports": ports,
        "created": c.get("Created", 0),
        "mounts": [m.get("Name") for m in c.get("Mounts", []) if m.get("Type") == "volume"],
        "networks": list((c.get("NetworkSettings") or {}).get("Networks", {}).keys()),
    }


@route("GET", r"/api/overview")
def r_overview(h, qs, body):
    ctx = ctx_of(qs)
    rows = [short_row(c) for c in dk(ctx, "GET", "/containers/json?all=1")]
    info = dk(ctx, "GET", "/info")
    return {
        "containers": rows,
        "info": {
            "version": info.get("ServerVersion"), "os": info.get("OperatingSystem"),
            "driver": info.get("Driver"), "images": info.get("Images"),
            "ncpu": info.get("NCPU"), "mem": info.get("MemTotal"),
        },
    }


_stats = {}


def one_stat(ctx, cid, host_mem):
    s = dk(ctx, "GET", f"/containers/{cid}/stats?stream=false", timeout=10)
    cpu, pre = s.get("cpu_stats", {}), s.get("precpu_stats", {})
    d_cpu = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
    d_sys = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    mem = s.get("memory_stats", {})
    used = mem.get("usage", 0) - mem.get("stats", {}).get("inactive_file", 0)
    limit = mem.get("limit", 0)
    nets = (s.get("networks") or {}).values()
    return {
        "cpu": round(d_cpu / d_sys * (cpu.get("online_cpus") or 1) * 100, 1) if d_sys > 0 and d_cpu >= 0 else 0,
        "mem": max(used, 0),
        "limit": limit if 0 < limit < host_mem * 0.95 else 0,
        "rx": sum(n.get("rx_bytes", 0) for n in nets),
        "tx": sum(n.get("tx_bytes", 0) for n in nets),
        "pids": (s.get("pids_stats") or {}).get("current", 0),
    }


@route("GET", r"/api/stats")
def r_stats(h, qs, body):
    ctx = ctx_of(qs)
    hit = _stats.get(ctx)
    if hit and time.time() - hit[0] < 2.5:  # many tabs share one sample
        return hit[1]
    host_mem = meminfo()["MemTotal"]
    ids = [c["Id"][:12] for c in dk(ctx, "GET", "/containers/json")]
    res = pmap(lambda i: one_stat(ctx, i, host_mem), ids)
    data = {i: r for i, r in zip(ids, res) if r}
    _stats[ctx] = (time.time(), data)
    return data


@route("GET", r"/api/system")
def r_system(h, qs, body):
    m = meminfo()
    st = os.statvfs("/")
    try:
        swappiness = int(Path("/proc/sys/vm/swappiness").read_text())
    except OSError:
        swappiness = None
    out = {
        "ram": {"total": m["MemTotal"], "available": m["MemAvailable"], "used": m["MemTotal"] - m["MemAvailable"],
                "cached": m.get("Cached", 0)},
        "swap": {"total": m["SwapTotal"], "used": m["SwapTotal"] - m["SwapFree"]},
        "swappiness": swappiness,
        "cpu": cpu_percent(),
        "load": list(os.getloadavg()),
        "cores": os.cpu_count(),
        "disk": {"total": st.f_blocks * st.f_frsize, "used": (st.f_blocks - st.f_bfree) * st.f_frsize},
        "pressure": {k: pressure(k) for k in ("memory", "io", "cpu")},
        "uptime": float(Path("/proc/uptime").read_text().split()[0]),
    }
    if qs.get("top"):
        out["top"] = top_programs()
        out["oom"] = oom_kills()
    return out


SECRET = re.compile(r"(PASS|SECRET|TOKEN|KEY|PWD|CREDENTIAL|DSN|DATABASE_URL)", re.I)


@route("GET", r"/api/inspect/([0-9a-f]+)")
def r_inspect(h, qs, body, cid):
    d = dk(ctx_of(qs), "GET", f"/containers/{cid}/json")
    cfg, hc, st = d["Config"], d["HostConfig"], d["State"]
    env = []
    for e in cfg.get("Env") or []:
        k, _, v = e.partition("=")
        env.append({"key": k, "value": v, "secret": bool(SECRET.search(k))})
    ports = []
    for p, binds in ((d.get("NetworkSettings") or {}).get("Ports") or {}).items():
        for b in binds or [None]:
            ports.append(f'{b["HostIp"]}:{b["HostPort"]} -> {p}' if b else p)
    return {
        "id": d["Id"][:12], "name": d["Name"].lstrip("/"), "image": cfg.get("Image"),
        "created": d.get("Created"), "started": st.get("StartedAt"),
        "state": st.get("Status"), "exit_code": st.get("ExitCode"), "oom_killed": st.get("OOMKilled"),
        "health": (st.get("Health") or {}).get("Status"), "restarts": d.get("RestartCount"),
        "restart_policy": (hc.get("RestartPolicy") or {}).get("Name"),
        "mem_limit": hc.get("Memory"), "cmd": cfg.get("Cmd"), "entrypoint": cfg.get("Entrypoint"),
        "workdir": cfg.get("WorkingDir"), "user": cfg.get("User"), "tty": cfg.get("Tty"),
        "env": env,
        "mounts": [{"type": m.get("Type"), "src": m.get("Name") or m.get("Source"), "dst": m.get("Destination"),
                    "rw": m.get("RW")} for m in d.get("Mounts", [])],
        "networks": [{"name": n, "ip": v.get("IPAddress")} for n, v in
                     ((d.get("NetworkSettings") or {}).get("Networks") or {}).items()],
        "ports": ports,
        "labels": cfg.get("Labels") or {},
    }


@route("POST", r"/api/containers/([0-9a-f]+)/(\w+)")
def r_action(h, qs, body, cid, action):
    ctx = ctx_of(qs)
    if action == "remove":
        dk(ctx, "DELETE", f"/containers/{cid}?force={1 if qs.get('force') else 0}", timeout=30)
    elif action in ACTIONS:
        dk(ctx, "POST", f"/containers/{cid}/{action}", timeout=40)
    else:
        raise DockerError(400, "bad action")
    return {"ok": True}


@route("POST", r"/api/projects/([^/]+)/(start|stop|restart)")
def r_project(h, qs, body, project, action):
    ctx = ctx_of(qs)
    f = quote(json.dumps({"label": [f"com.docker.compose.project={project}"]}))
    items = dk(ctx, "GET", f"/containers/json?all=1&filters={f}")
    want = (lambda c: c["State"] != "running") if action == "start" else (lambda c: c["State"] == "running")
    ids = [c["Id"][:12] for c in items if want(c)]
    res = pmap(lambda i: dk(ctx, "POST", f"/containers/{i}/{action}", timeout=40), ids)
    return {"count": len(ids), "errors": sum(1 for r in res if r is None)}


def is_tty(ctx, cid):
    return bool(dk(ctx, "GET", f"/containers/{cid}/json")["Config"].get("Tty"))


@route("GET", r"/api/logs/([0-9a-f]+)")
def r_logs(h, qs, body, cid):
    ctx = ctx_of(qs)
    tail = min(int(qs.get("tail", 300)), 3000)
    raw = dk(ctx, "GET", f"/containers/{cid}/logs?stdout=1&stderr=1&timestamps=1&tail={tail}", timeout=20, text=True)
    return {"text": demux(raw, is_tty(ctx, cid))}


@route("GET", r"/api/logs/([0-9a-f]+)/stream")
def r_logs_stream(h, qs, body, cid):
    ctx = ctx_of(qs)
    if not STREAMS.acquire(blocking=False):
        raise DockerError(429, "too many live log streams")
    conn = None
    try:
        tty = is_tty(ctx, cid)
        tail = min(int(qs.get("tail", 200)), 3000)
        conn = connect(endpoint(ctx), 15)
        conn.request("GET", f"{API}/containers/{cid}/logs?stdout=1&stderr=1&timestamps=1&follow=1&tail={tail}")
        r = conn.getresponse()
        if r.status >= 400:
            raise DockerError(r.status, r.read().decode(errors="replace"))
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        buf, pending = b"", ""
        while True:
            try:
                chunk = r.read1(65536)
            except TimeoutError:
                h.wfile.write(b": ping\n\n")  # fails fast if the browser left
                h.wfile.flush()
                continue
            if not chunk:
                break
            buf += chunk
            text = ""
            if tty:
                text, buf = buf.decode(errors="replace"), b""
            else:
                while len(buf) >= 8:
                    size = int.from_bytes(buf[4:8], "big")
                    if len(buf) < 8 + size:
                        break
                    text += buf[8:8 + size].decode(errors="replace")
                    buf = buf[8 + size:]
            pending += text
            *lines, pending = pending.split("\n")
            if lines:
                h.wfile.write(b"data: " + json.dumps(lines).encode() + b"\n\n")
                h.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        STREAMS.release()
        if conn:
            conn.close()
    return DONE


@route("GET", r"/api/images")
def r_images(h, qs, body):
    ctx = ctx_of(qs)
    used = {}
    for c in dk(ctx, "GET", "/containers/json?all=1"):
        used.setdefault(c.get("ImageID"), []).append((c.get("Names") or ["/?"])[0].lstrip("/"))
    return [{
        "id": i["Id"][7:19], "full": i["Id"], "tags": i.get("RepoTags") or [], "size": i.get("Size", 0),
        "created": i.get("Created", 0), "used_by": used.get(i["Id"], []),
    } for i in dk(ctx, "GET", "/images/json")]


@route("POST", r"/api/images/remove")
def r_image_rm(h, qs, body):
    dk(ctx_of(qs), "DELETE", f"/images/{quote(qs['id'])}", timeout=30)
    return {"ok": True}


@route("GET", r"/api/volumes")
def r_volumes(h, qs, body):
    ctx = ctx_of(qs)
    used = {}
    for c in dk(ctx, "GET", "/containers/json?all=1"):
        for m in c.get("Mounts", []):
            if m.get("Type") == "volume":
                used.setdefault(m["Name"], []).append((c.get("Names") or ["/?"])[0].lstrip("/"))
    sizes = {v["Name"]: (v.get("UsageData") or {}).get("Size", -1) for v in dk(ctx, "GET", "/system/df", timeout=60).get("Volumes") or []}
    vols = dk(ctx, "GET", "/volumes").get("Volumes") or []
    return [{
        "name": v["Name"], "driver": v.get("Driver"), "created": v.get("CreatedAt"),
        "size": sizes.get(v["Name"], -1), "used_by": used.get(v["Name"], []),
        "anonymous": bool(re.fullmatch(r"[0-9a-f]{64}", v["Name"])),
    } for v in vols]


@route("POST", r"/api/volumes/remove")
def r_volume_rm(h, qs, body):
    dk(ctx_of(qs), "DELETE", f"/volumes/{quote(qs['name'])}")
    return {"ok": True}


@route("GET", r"/api/networks")
def r_networks(h, qs, body):
    ctx = ctx_of(qs)
    count = {}
    for c in dk(ctx, "GET", "/containers/json"):
        for n in (c.get("NetworkSettings") or {}).get("Networks", {}).values():
            count[n.get("NetworkID")] = count.get(n.get("NetworkID"), 0) + 1
    return [{
        "name": n["Name"], "id": n["Id"][:12], "driver": n.get("Driver"), "scope": n.get("Scope"),
        "subnet": ", ".join(c.get("Subnet", "") for c in (n.get("IPAM") or {}).get("Config") or []),
        "containers": count.get(n["Id"], 0),
    } for n in dk(ctx, "GET", "/networks")]


@route("GET", r"/api/cleanup/scan")
def r_scan(h, qs, body):
    ctx = ctx_of(qs)
    df = dk(ctx, "GET", "/system/df", timeout=60)
    f = quote(json.dumps({"status": ["exited", "created", "dead"]}))
    stopped = dk(ctx, "GET", f"/containers/json?all=1&filters={f}")
    images = df.get("Images") or []
    dangling = [i for i in images if not i.get("RepoTags") and i.get("Containers", 0) == 0]
    unused = [i for i in images if i.get("RepoTags") and i.get("Containers", 0) == 0]
    vols = [v for v in df.get("Volumes") or []
            if re.fullmatch(r"[0-9a-f]{64}", v["Name"]) and (v.get("UsageData") or {}).get("RefCount", 1) == 0]
    cache = [b for b in df.get("BuildCache") or [] if not b.get("InUse")]
    return {
        "containers": {"label": "Stopped containers", "count": len(stopped), "size": 0,
                       "items": [short_row(c)["name"] for c in stopped]},
        "dangling_images": {"label": "Dangling images (no tag)", "count": len(dangling),
                            "size": sum(i.get("Size", 0) for i in dangling), "items": [i["Id"][7:19] for i in dangling]},
        "unused_images": {"label": "Unused tagged images", "count": len(unused),
                          "size": sum(i.get("Size", 0) for i in unused), "items": [i["RepoTags"][0] for i in unused]},
        "volumes": {"label": "Unused anonymous volumes", "count": len(vols),
                    "size": sum(max((v.get("UsageData") or {}).get("Size", 0), 0) for v in vols),
                    "items": [v["Name"][:12] for v in vols]},
        "build_cache": {"label": "Build cache", "count": len(cache),
                        "size": sum(b.get("Size", 0) for b in cache), "items": []},
    }


@route("POST", r"/api/cleanup/run")
def r_cleanup(h, qs, body):
    ctx, freed, done = ctx_of(qs), 0, []
    for k in body or []:
        if k == "containers":
            r = dk(ctx, "POST", "/containers/prune", timeout=120)
        elif k == "dangling_images":
            r = dk(ctx, "POST", "/images/prune?filters=" + quote(json.dumps({"dangling": ["true"]})), timeout=120)
        elif k == "unused_images":
            r = dk(ctx, "POST", "/images/prune?filters=" + quote(json.dumps({"dangling": ["false"]})), timeout=300)
        elif k == "volumes":
            r = dk(ctx, "POST", "/volumes/prune", timeout=120)  # anonymous only. Named volumes stay.
        elif k == "build_cache":
            r = dk(ctx, "POST", "/build/prune?all=1", timeout=300)
        else:
            raise DockerError(400, f"unknown kind: {k}")
        freed += r.get("SpaceReclaimed") or 0
        done.append(k)
    return {"freed": freed, "done": done}


# ---------- debugging ----------

EXIT_HINTS = {
    1: ("The app stopped with an error.", "Read the last log lines below. The cause is usually printed just before it stopped."),
    2: ("The app stopped: wrong command or bad arguments.", "Check the command and arguments the container starts with."),
    126: ("The start command was found but cannot run.", "Check file permissions (chmod +x) on the entrypoint script."),
    127: ("The start command was not found in the image.", "Check the command spelling and that the file exists in the image. Rebuild the image if needed."),
    137: ("The container was killed (SIGKILL).", "This is usually out of memory, a manual kill, or a stop that took too long."),
    139: ("The app crashed (segmentation fault).", "This is a bug in the app or a native library. Check the image version."),
    143: ("The container was stopped (SIGTERM).", "This is normal after docker stop. Start it again if you need it."),
}

# (pattern, severity, title, why, fixes). Fixes starting with "$ " are commands.
PATTERNS = [
    (r"address already in use|EADDRINUSE|bind: address", "bad", "A port is already in use",
     "Another program or container already uses the port this app wants.",
     ["Find who uses it: $ ss -ltnp | grep <port>", "Stop that program, or change the host port in your compose file."]),
    (r"no space left on device", "bad", "The disk is full",
     "Docker or the app cannot write any more data.",
     ["See what uses space: $ docker system df", "Use the Cleanup tab to remove unused images and build cache."]),
    (r"out of memory|MemoryError|Cannot allocate memory|heap out of memory|JavaScript heap", "bad", "The app ran out of memory",
     "The app asked for more memory than it could get.",
     ["Free RAM on the host (see Overview) or raise the container memory limit.", "Look for a memory leak or a huge query or file load."]),
    (r"exec format error", "bad", "Wrong CPU type for this image",
     "The image was built for a different CPU (for example ARM vs x86).",
     ["Rebuild or pull the image for this machine: $ docker pull --platform linux/amd64 <image>"]),
    (r"ModuleNotFoundError|No module named|Cannot find module|ImportError|ClassNotFoundException", "bad", "A code package is missing",
     "The image does not have a package the app needs. The image may be old.",
     ["Rebuild the image so new packages are installed: $ docker compose build --no-cache <service>", "Check requirements.txt or package.json has the package."]),
    (r"relation .+ does not exist|no such table|table .+ doesn't exist|UndefinedTable|pending migration", "warn", "Database tables are missing or old",
     "The database has not been set up for this version of the app.",
     ["Run the database migrations (for example: alembic upgrade head).", "Check you are pointing at the right database."]),
    (r"password authentication failed|authentication failed|Access denied for user|invalid (credentials|api key)", "warn", "Login to another service failed",
     "A username, password, key or token is wrong or missing.",
     ["Check the .env values for this service.", "Recreate the container after changing env: $ docker compose up -d --force-recreate <service>"]),
    (r"could not translate host name|Temporary failure in name resolution|getaddrinfo|Name or service not known|ENOTFOUND|no such host", "warn", "A host name could not be found (DNS)",
     "The name does not exist, or DNS is not working right now. DNS to cloud hosts can fail for a short time.",
     ["Retry in a minute. If it keeps failing, check the name for typos.", "Between containers, use the service name, not localhost.", "Test it from inside: use Run a check below."]),
    (r"connection refused|ECONNREFUSED|connect: connection|Connection reset by peer", "warn", "Another service is not reachable",
     "A service this container needs is down, still starting, or the host or port is wrong.",
     ["Check the Dependencies section and start what is stopped.", "Inside Docker, use the service name (like db), not localhost.", "Check the port number in your env settings."]),
    (r"permission denied|EACCES|Operation not permitted", "warn", "Permission denied",
     "The app is not allowed to use a file, folder or socket.",
     ["Check who owns the mounted folder: $ ls -ln <path>", "Run the container as the right user, or fix the folder owner."]),
    (r"certificate verify failed|x509:|SSL: CERTIFICATE|SSLError|tls: (bad|failed)|handshake failure", "warn", "Secure connection (TLS) problem",
     "The certificate is wrong, expired or not trusted.",
     ["Check the system date is right.", "Add the CA certificate to the image if you use a private CA."]),
    (r"No such file or directory|ENOENT|FileNotFoundError", "warn", "A file or folder was not found",
     "The app looked for a path that does not exist inside the container.",
     ["Check your volume mounts in the Details tab.", "Check the path in your env or config."]),
    (r"KeyError|missing required|is required|environment variable .{0,40}(not set|missing|required)|undefined environment", "warn", "A setting may be missing",
     "The app wants a config or env value that it did not get.",
     ["Compare the env names with .env.example (Details > Environment).", "Recreate the container after fixing env."]),
    (r"timed out|timeout|deadline exceeded", "warn", "Something took too long (timeout)",
     "A request or connection did not answer in time. The other side may be slow or down.",
     ["Check the Dependencies section and the host load on Overview."]),
    (r'HTTP/1\.[01]" 5\d\d', "warn", "The app is answering with server errors (HTTP 5xx)",
     "Requests reach the app but it fails while handling them.",
     ["Read the log lines just before each 5xx for the real error."]),
    (r"Traceback \(most recent call last\)|panic:|Unhandled|Fatal error|Segmentation fault", "warn", "The app crashed with an error",
     "The log shows an error report from the app.",
     ["Read the last lines of the error. The final line is usually the cause."]),
]


def ts_of(s):
    from datetime import datetime
    try:
        s = re.sub(r"(\.\d{6})\d+", r"\1", s or "").replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0


ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Lines logged as info or debug are not problems. Skip them when matching.
NOISE = re.compile(r"\[\s*(info|debug|trace|I|D)\s*\]|\"level\"\s*:\s*\"(info|debug)\"|level=(info|debug)|^\s*(INFO|DEBUG)\b[: ]", re.I)


def clean_line(line):
    line = ANSI.sub("", line)
    m = re.match(r"^\d{4}-\d\d-\d\dT[\d:.]+Z? ", line)
    return (line[m.end():] if m else line)[:240]


def get_events(ctx, cid):
    now = int(time.time())
    f = quote(json.dumps({"container": [cid]}))
    try:
        raw = dk(ctx, "GET", f"/events?since={now - 86400}&until={now}&filters={f}", timeout=10, text=True)
    except DockerError:
        return []
    out = []
    for line in raw.decode(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        act = e.get("Action", "")
        if act.startswith("exec_") or act in ("attach", "resize"):
            continue
        a = (e.get("Actor") or {}).get("Attributes") or {}
        detail = ""
        if act == "die":
            detail = f"exit code {a.get('exitCode', '?')}"
        elif act == "kill":
            detail = f"signal {a.get('signal', '?')}"
        out.append({"time": e.get("time", 0), "action": act, "detail": detail})
    return out[-20:][::-1]


@route("GET", r"/api/diagnose/([0-9a-f]+)")
def r_diagnose(h, qs, body, cid):
    ctx = ctx_of(qs)
    d = dk(ctx, "GET", f"/containers/{cid}/json")
    cfg, hc, st = d["Config"], d["HostConfig"], d["State"]
    name = d["Name"].lstrip("/")
    labels = cfg.get("Labels") or {}
    status, code = st.get("Status", ""), st.get("ExitCode", 0)
    restarts = d.get("RestartCount", 0)
    running = bool(st.get("Running"))
    findings = []

    def add(sev, title, why, fix=(), evidence=(), action=None):
        findings.append({"sev": sev, "title": title, "why": why, "fix": list(fix), "evidence": list(evidence), "action": action})

    raw = dk(ctx, "GET", f"/containers/{cid}/logs?stdout=1&stderr=1&timestamps=1&tail=300", timeout=20, text=True)
    lines = [clean_line(x) for x in demux(raw, bool(cfg.get("Tty"))).splitlines() if x.strip()]
    tail = lines[-5:]
    mem_limit = hc.get("Memory") or 0

    # 1. what Docker itself says
    if st.get("OOMKilled"):
        add("bad", "Killed: it used more memory than its limit",
            "Docker stopped it because it went over its memory limit" + (f" of {mem_limit // 1048576} MB." if mem_limit else "."),
            ["Raise the limit (compose: mem_limit, or deploy.resources.limits.memory).",
             f"Quick test without recreating: $ docker update --memory 2g --memory-swap 2g {name}",
             "Or find out why it uses so much memory (big files, a leak, too many workers)."],
            tail, "start" if not running else None)
    if st.get("Error"):
        add("bad", "Docker could not start it", st["Error"],
            ["Read the error above. Common causes: wrong command, missing file, port in use, bad mount."], [], "logs")
    if status in ("exited", "dead") and code and not st.get("OOMKilled"):
        why, fix = EXIT_HINTS.get(code, (f"The app stopped with exit code {code}.", "Read the last log lines for the reason."))
        add("bad" if code not in (143,) else "info", f"Stopped with exit code {code}", why, [fix], tail, "start")
        if code == 137:
            fin = ts_of(st.get("FinishedAt"))
            hits = [k for k in oom_kills() if abs(ts_of(k["time"]) - fin) < 90]
            if hits:
                add("bad", "The host ran out of memory and the kernel killed it",
                    f"The Linux kernel killed '{hits[0]['name']}' at nearly the same time this container stopped.",
                    ["Free RAM: stop containers or apps you do not need (see Overview).", "Add swap, or lower swappiness so apps stay in RAM."], [], None)
    if status == "exited" and code == 0:
        add("info", "It finished normally (exit 0)",
            "Nothing is wrong. One-time jobs (init, migrate) are meant to stop after they finish.",
            ["If this should keep running, check why the main process ends: read the logs."], tail, "start")
    if status == "restarting" or restarts >= 3:
        add("bad", f"Crash loop: restarted {restarts} times",
            "It starts, fails, and Docker starts it again. Look at why it fails each time.",
            ["Pause the loop while you debug: $ docker update --restart=no " + name, "Read the errors found below."], tail, "logs")
    hl = st.get("Health") or {}
    if hl.get("Status") == "unhealthy":
        outs = [(e.get("Output") or "").strip()[-200:] for e in (hl.get("Log") or [])[-3:]]
        test = (cfg.get("Healthcheck") or {}).get("Test") or []
        fix = ["The health check command is failing. Run it by hand below with Run a check."]
        if test:
            fix.append("Health command: $ " + " ".join(test[1:] if test[0] in ("CMD", "CMD-SHELL") else test))
        fix.append("If the app is just slow to start, raise start_period in the healthcheck.")
        add("bad", "The health check is failing", "Docker runs a test command on a timer. It keeps failing.", fix, [o for o in outs if o], "logs")
    elif hl.get("Status") == "starting" and running:
        add("info", "Health check is still starting", "Docker has not had a good check result yet. Wait a little.", [], [], None)

    # 2. things this container waits for
    project = labels.get("com.docker.compose.project", "")
    deps = []
    dep_names = [x.split(":")[0] for x in (labels.get("com.docker.compose.depends_on") or "").split(",") if x]
    if project and dep_names:
        f = quote(json.dumps({"label": [f"com.docker.compose.project={project}"]}))
        for c in dk(ctx, "GET", f"/containers/json?all=1&filters={f}"):
            row = short_row(c)
            if row["service"] in dep_names:
                deps.append({"name": row["name"], "service": row["service"], "state": row["state"], "health": row["health"], "id": row["id"]})
                if row["state"] != "running" or row["health"] == "unhealthy":
                    add("bad", f"It depends on '{row['service']}', which is {row['health'] or row['state']}",
                        "This container needs that service. It will fail until the service works.",
                        [f"Fix '{row['service']}' first, then restart this one.", f"$ docker start {row['name']}"], [], "restart_dep")

    # 3. what the logs say
    seen = set()
    for pat, sev, title, why, fix in PATTERNS:
        hits = [x for x in lines if re.search(pat, x, re.I) and (pat.startswith("HTTP") or not NOISE.search(x))]
        if hits and title not in seen:
            seen.add(title)
            add(sev, title, why, fix, hits[-3:], None)

    # 4. numbers right now
    stats_row = None
    if running:
        try:
            stats_row = one_stat(ctx, cid, meminfo()["MemTotal"])
        except DockerError:
            pass
    if stats_row:
        if stats_row["limit"] and stats_row["mem"] / stats_row["limit"] > 0.9:
            add("warn", "It is close to its memory limit",
                f"Using {stats_row['mem'] // 1048576} MB of {stats_row['limit'] // 1048576} MB. It may be killed soon.",
                [f"$ docker update --memory {max(2, stats_row['limit'] // 1073741824 * 2)}g --memory-swap {max(2, stats_row['limit'] // 1073741824 * 2)}g {name}"], [], None)
        if stats_row["cpu"] > 90:
            add("warn", "It is using almost a full CPU core", f"CPU is at {stats_row['cpu']}%. It may be stuck in a loop or very busy.",
                ["Check the logs for repeated lines.", "Look at running processes with Run a check > Processes."], [], None)
    m = meminfo()
    if m["MemAvailable"] / m["MemTotal"] < 0.1 or (m["SwapTotal"] and (m["SwapTotal"] - m["SwapFree"]) / m["SwapTotal"] > 0.8):
        add("warn", "The host is short on memory",
            "Everything on this machine can be slow or get killed when RAM and swap run low.",
            ["Stop containers you do not need (Containers tab).", "Close heavy apps, or add swap."], [], None)

    if not findings and running:
        add("ok", "No problems found", "It is running and no known problem shows in the logs, health or events.", [], [], None)
    elif not findings:
        add("info", f"It is {status}", "Nothing in the logs or events explains it. Start it and watch the logs.", [], tail, "start")
    order = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
    findings.sort(key=lambda x: order[x["sev"]])
    return {
        "summary": {"name": name, "image": cfg.get("Image"), "status": status, "exit_code": code, "restarts": restarts,
                    "started": st.get("StartedAt"), "finished": st.get("FinishedAt"), "health": hl.get("Status"),
                    "running": running, "restart_policy": (hc.get("RestartPolicy") or {}).get("Name"),
                    "mem_limit": mem_limit, "service": labels.get("com.docker.compose.service", "")},
        "findings": findings,
        "timeline": get_events(ctx, cid),
        "health_log": [{"time": e.get("End"), "exit": e.get("ExitCode"), "output": (e.get("Output") or "").strip()[-200:]}
                       for e in (hl.get("Log") or [])[-5:]][::-1],
        "deps": deps,
        "stats": stats_row,
        "last_logs": lines[-40:],
    }


@route("POST", r"/api/exec/([0-9a-f]+)")
def r_exec(h, qs, body, cid):
    ctx = ctx_of(qs)
    cmd = ((body or {}).get("cmd") or "").strip()
    if not cmd or len(cmd) > 500:
        raise DockerError(400, "give a command up to 500 characters")
    ex = dk(ctx, "POST", f"/containers/{cid}/exec",
            {"Cmd": ["sh", "-c", cmd], "AttachStdout": True, "AttachStderr": True, "Tty": False})
    conn = connect(endpoint(ctx), 20)
    out, note = b"", ""
    try:
        conn.request("POST", f"{API}/exec/{ex['Id']}/start", json.dumps({"Detach": False, "Tty": False}),
                     {"Content-Type": "application/json"})
        r = conn.getresponse()
        if r.status >= 400:
            raise DockerError(r.status, r.read().decode(errors="replace"))
        try:
            while len(out) < 131072:
                chunk = r.read1(65536)
                if not chunk:
                    break
                out += chunk
        except TimeoutError:
            note = "\n[timed out after 20 s. The command may still be running in the container.]"
        if len(out) >= 131072:
            note = "\n[output cut at 128 KB]"
    finally:
        conn.close()
    info = dk(ctx, "GET", f"/exec/{ex['Id']}/json")
    return {"exit_code": info.get("ExitCode"), "output": demux(out, False)[:60000] + note}


# ---------- compose ----------

STATE_FILE = Path.home() / ".local" / "state" / "dockerdeck" / "projects.json"
SECRET_LINE = re.compile(r"^(\s*-?\s*)(['\"]?[\w.\-]*(?:PASS|SECRET|TOKEN|KEY|PWD|CREDENTIAL|DSN)[\w.\-]*['\"]?)(\s*[:=]\s*)(.+?)\s*$", re.I)
COMPOSE_ACTIONS = {"up": ["up", "-d"], "down": ["down"], "pull": ["pull"], "build": ["build"], "validate": ["config", "-q"]}
JOBS = {}
JOBS_LOCK = threading.Lock()


def load_known():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_known(data):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(STATE_FILE)
    except OSError:
        pass


def compose_projects(ctx):
    """Projects seen on containers. Remembered on disk so a project stays listed after 'down'."""
    live = {}
    for c in dk(ctx, "GET", "/containers/json?all=1"):
        lb = c.get("Labels") or {}
        name = lb.get("com.docker.compose.project")
        if not name:
            continue
        e = live.setdefault(name, {
            "dir": lb.get("com.docker.compose.project.working_dir", ""),
            "files": [f for f in lb.get("com.docker.compose.project.config_files", "").split(",") if f],
            "services": {},
        })
        r = short_row(c)
        e["services"].setdefault(r["service"] or "?", []).append(
            {"name": r["name"], "id": r["id"], "state": r["state"], "health": r["health"], "image": r["image"], "ports": r["ports"]})
    known = load_known()
    mine = known.setdefault(ctx, {})
    changed = False
    for name, e in live.items():
        rec = {"dir": e["dir"], "files": e["files"]}
        if e["files"] and mine.get(name) != rec:
            mine[name] = rec
            changed = True
    if changed:
        save_known(known)
    out = []
    for name in sorted(set(live) | set(mine)):
        e = live.get(name) or {"dir": mine[name]["dir"], "files": mine[name]["files"], "services": {}}
        svcs = [{"service": k, "containers": v} for k, v in sorted(e["services"].items())]
        total = sum(len(x["containers"]) for x in svcs)
        running = sum(1 for x in svcs for c in x["containers"] if c["state"] == "running")
        out.append({"name": name, "dir": e["dir"], "files": e["files"], "services": svcs,
                    "total": total, "running": running, "down": total == 0})
    return out


def project_of(ctx, name):
    for p in compose_projects(ctx):
        if p["name"] == name:
            return p
    raise DockerError(404, f"unknown compose project: {name}")


def mask_secrets(text):
    out = []
    for line in text.splitlines():
        m = SECRET_LINE.match(line)
        # ${VAR} only points to a value. It is safe to show.
        out.append(line if not m or m.group(4).startswith("${") else f"{m.group(1)}{m.group(2)}{m.group(3)}••••••")
    return "\n".join(out)


@route("GET", r"/api/compose")
def r_compose(h, qs, body):
    return compose_projects(ctx_of(qs))


@route("GET", r"/api/compose/([a-z0-9][a-z0-9_-]*)/file")
def r_compose_file(h, qs, body, project):
    info = project_of(ctx_of(qs), project)
    files = []
    for path in info["files"]:  # only paths Docker itself reported. Never a path from the browser.
        f = Path(path)
        try:
            if not f.is_file() or f.stat().st_size > 400_000:
                raise OSError("file is missing or too large")
            files.append({"path": path, "text": mask_secrets(f.read_text(errors="replace"))})
        except OSError as e:
            files.append({"path": path, "error": str(e)})
    return {"files": files, "dir": info["dir"]}


@route("POST", r"/api/compose/([a-z0-9][a-z0-9_-]*)/(up|down|pull|build|validate)")
def r_compose_act(h, qs, body, project, action):
    ctx = ctx_of(qs)
    info = project_of(ctx, project)
    if not info["files"]:
        raise DockerError(409, "compose files for this project are not known")
    with JOBS_LOCK:
        if any(j["project"] == project and not j["done"] for j in JOBS.values()):
            raise DockerError(409, "another job is still running for this project")
        argv = ["docker", "--context", ctx, "compose", "--progress", "plain", "-p", project]
        if info["dir"]:
            argv += ["--project-directory", info["dir"]]
        for f in info["files"]:
            argv += ["-f", f]
        argv += COMPOSE_ACTIONS[action]
        jid = os.urandom(4).hex()
        job = {"title": f"{action} {project}", "project": project, "out": "", "done": False, "code": None, "at": time.time()}
        JOBS[jid] = job
        for old in sorted(JOBS, key=lambda k: JOBS[k]["at"])[:-12]:
            if JOBS[old]["done"]:
                del JOBS[old]

    def work():
        try:
            p = subprocess.Popen(argv, cwd=info["dir"] if Path(info["dir"]).is_dir() else None, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace", env={**os.environ, "NO_COLOR": "1"})
            for line in p.stdout:
                job["out"] = (job["out"] + line)[-100_000:]
            job["code"] = p.wait()
        except OSError as e:
            job["out"] += f"\n{e}"
            job["code"] = -1
        job["done"] = True

    threading.Thread(target=work, daemon=True).start()
    return {"job": jid}


@route("GET", r"/api/jobs/([0-9a-f]{8})")
def r_job(h, qs, body, jid):
    j = JOBS.get(jid)
    if not j:
        raise DockerError(404, "unknown job")
    return {"title": j["title"], "out": j["out"], "done": j["done"], "code": j["code"]}


@route("POST", r"/api/compose/([a-z0-9][a-z0-9_-]*)/forget")
def r_compose_forget(h, qs, body, project):
    ctx = ctx_of(qs)
    if not project_of(ctx, project)["down"]:
        raise DockerError(409, "the project still has containers. Take it down first")
    known = load_known()
    known.get(ctx, {}).pop(project, None)
    save_known(known)
    return {"ok": True}


# ---------- small summary for panel widgets (GNOME extension) ----------

@route("GET", r"/api/summary")
def r_summary(h, qs, body):
    ctx = qs.get("ctx") or next((c["name"] for c in contexts() if c["current"]), "default")
    m = meminfo()
    rows = [short_row(c) for c in dk(ctx, "GET", "/containers/json?all=1")]
    problems = []
    for c in rows:
        code = re.search(r"Exited \((\d+)\)", c["status"])
        code = code.group(1) if code else None
        if c["state"] == "restarting":
            why = "Restarting in a loop"
        elif c["health"] == "unhealthy":
            why = "Health check is failing"
        elif c["state"] == "dead":
            why = "Container is dead"
        elif c["state"] == "exited" and code not in (None, "0", "143"):
            why = "Killed (exit 137), often out of memory" if code == "137" else f"Stopped with an error (exit {code})"
        else:
            continue
        problems.append({"name": c["name"], "id": c["id"], "reason": why})
    return {
        "ctx": ctx,
        "ram_pct": round((m["MemTotal"] - m["MemAvailable"]) / m["MemTotal"] * 100, 1),
        "swap_pct": round((m["SwapTotal"] - m["SwapFree"]) / m["SwapTotal"] * 100, 1) if m["SwapTotal"] else 0,
        "ram_used": m["MemTotal"] - m["MemAvailable"], "ram_total": m["MemTotal"],
        "running": sum(1 for c in rows if c["state"] == "running"), "total": len(rows),
        "problems": problems,
    }


@route("GET", r"/")
def r_index(h, qs, body):
    h.reply(200, INDEX.read_bytes(), "text/html; charset=utf-8")
    return DONE


# ---------- server ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self, method):
        try:
            # The Docker socket is root-level. Only talk to local pages.
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in LOCAL_HOSTS:
                return self.reply(403, {"detail": "local access only"})
            # Custom header blocks cross-site form posts.
            if method != "GET" and self.headers.get("X-DockerDeck") != "1":
                return self.reply(403, {"detail": "missing header"})
            u = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            for m, rx, fn in ROUTES:
                match = m == method and rx.fullmatch(u.path)
                if match:
                    res = fn(self, qs, body, *match.groups())
                    return None if res is DONE else self.reply(200, res)
            self.reply(404, {"detail": "not found"})
        except DockerError as e:
            self.reply(e.status if 400 <= e.status < 600 else 500, {"detail": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.reply(500, {"detail": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8787))
    print(f"DockerDeck on http://127.0.0.1:{port}", flush=True)
    Server(("127.0.0.1", port), Handler).serve_forever()
