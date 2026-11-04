"""Follow a log file and feed new lines into the engine.

Line formats: a JSON object with level/source/message[/ts], or plain text
"[HH:MM:SS] LEVEL source message". Anything else becomes an info line.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import tempfile
import time


LEVELS = {"debug", "info", "warn", "error"}
ALIASES = {"warning": "warn", "err": "error", "fatal": "error", "critical": "error", "trace": "debug"}
PLAIN = re.compile(r"^(?:\[?(?P<ts>\d\d:\d\d:\d\d)\]?\s+)?(?P<level>[A-Za-z]+)\s+(?P<source>[\w:.\-]+)\s+(?P<msg>.*)$")


def parse_line(line: str) -> tuple[str, str, str, str | None]:
    line = line.rstrip("\n")
    if line.startswith("{"):
        try:
            d = json.loads(line)
            lvl = str(d.get("level", "info")).lower()
            return ALIASES.get(lvl, lvl if lvl in LEVELS else "info"), str(d.get("source", "log")), str(d.get("message", line)), d.get("ts")
        except ValueError:
            pass
    m = PLAIN.match(line)
    if m:
        lvl = m["level"].lower()
        lvl = ALIASES.get(lvl, lvl)
        if lvl in LEVELS:
            return lvl, m["source"], m["msg"], m["ts"]
    return "info", "log", line, None


async def tail_file(engine: RainEngine, path: str, from_start: bool = False) -> None:
    pos = 0 if from_start else (os.path.getsize(path) if os.path.exists(path) else 0)
    buf = ""
    while True:
        try:
            size = os.path.getsize(path)
            if size < pos:       # truncated / rotated
                pos = 0
            if size > pos:
                with open(path, "r", errors="replace") as f:
                    f.seek(pos)
                    buf += f.read()
                    pos = f.tell()
                *lines, buf = buf.split("\n")
                for ln in lines:
                    if ln.strip():
                        engine.ingest(*parse_line(ln))
        except FileNotFoundError:
            pos = 0
        await asyncio.sleep(0.2)


# journald PRIORITY (syslog severity) -> level
PRIORITY = {0: "error", 1: "error", 2: "error", 3: "error", 4: "warn", 5: "info", 6: "info", 7: "debug"}


SSHD_BAD = re.compile(r"Failed |Invalid user|authentication failure|Connection closed by (invalid|authenticating)|Unable to negotiate|Bad protocol|kex_exchange")
UFW = re.compile(r"\[UFW (?P<act>\w+)\].*?SRC=(?P<src>\S+).*?PROTO=(?P<proto>\w+)(?:.*?DPT=(?P<dpt>\d+))?")
F2B = re.compile(r"\[(?P<jail>[\w-]+)\] (?P<act>Found|Ban|Unban|Restore Ban) (?P<ip>\S+)")


def parse_ufw(msg: str) -> tuple[str, str, str] | None:
    m = UFW.search(msg)
    if not m:
        return None
    where = f" -> :{m['dpt']}" if m["dpt"] else ""
    return "info", "ufw", f"{m['act'].lower()} {m['proto']} {m['src']}{where}"


def parse_fail2ban(line: str) -> tuple[str, str, str, str | None] | None:
    m = F2B.search(line)
    if not m:
        return None
    level = {"Found": "warn", "Ban": "error", "Restore Ban": "error", "Unban": "info"}[m["act"]]
    # fail2ban stamps server-local time; drop it so the engine stamps arrival in local time like every other source
    return level, f"f2b:{m['jail']}", f"{m['act'].lower()} {m['ip']}", None


BOT_UA = re.compile(r"([A-Za-z][\w.\-]*?(?:bot|crawler|spider|slurp|externalhit|scanner))\b", re.I)
OS_UA = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
         ("Mac OS X", "Mac"), ("CrOS", "ChromeOS"), ("Linux", "Linux"))
PROBE_PATH = re.compile(r"wp-(?:login|admin|content|includes)|xmlrpc|\.env|\.git\b|\.aws|phpmyadmin|\.php\b|cgi-bin|actuator|"
                        r"/admin|/etc/passwd|boot\.ini|\.\./|/\.(?!well-known)|Dockerfile|phpinfo", re.I)
ASSET_EXT = re.compile(r"\.(?:css|js|mjs|map|woff2?|ttf|otf|png|jpe?g|gif|webp|avif|svg|ico)(?:\?|$)", re.I)


def who_caddy(ua: str) -> str:
    """A short human label for a User-Agent: bot name, tool name, or 'Safari on iPhone'."""
    if not ua:
        return "a client with no User-Agent"
    m = BOT_UA.search(ua)
    if m:
        return m[1]
    if not ua.startswith("Mozilla/"):
        return ua.split("/")[0].split(" ")[0][:24] or "a client"
    browser = ("headless Chrome" if "HeadlessChrome" in ua else "Edge" if "Edg/" in ua else "Opera" if "OPR/" in ua else "Firefox" if "Firefox/" in ua else
               "Chrome" if ("Chrome/" in ua or "CriOS/" in ua) else "Safari" if "Safari/" in ua else "")
    os_ = next((label for key, label in OS_UA if key in ua), None)
    return f"{browser} on {os_}" if browser and os_ else browser or f"{os_} browser" if os_ else "a browser"


def _caddy(line: str):
    """Caddy JSON access log as a sentence: `Safari on iPhone viewed / from 1.2.3.4`,
    `GPTBot loaded /fonts/a.woff2 from 1.2.3.4`, `curl probed /.env (404) from 1.2.3.4`.
    5xx error, 4xx warn, else info. The verb comes second so templates group by visitor type, not by IP."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    req = d.get("request")
    if not isinstance(req, dict) or "status" not in d:
        return None
    status = int(d["status"])
    level = "error" if status >= 500 else "warn" if status >= 400 else "info"
    hdr = req.get("headers") if isinstance(req.get("headers"), dict) else {}
    ua = next(iter(hdr.get("User-Agent") or []), "")
    ctype = next(iter((d.get("resp_headers") or {}).get("Content-Type") or []), "")
    method = req.get("method", "?")
    uri = re.sub(r"[^\x20-\x7e]", "?", str(req.get("uri", "")))
    path = uri if len(uri) <= 60 else uri[:59] + "…"
    if PROBE_PATH.search(uri):
        verb = "probed"
    elif method not in ("GET", "HEAD"):
        verb = f"sent {method} to"
    elif status >= 400:
        verb = "requested"
    elif 300 <= status < 400 and status != 304:
        verb = "was redirected at"
    elif ASSET_EXT.search(uri) or ctype.startswith(("font/", "image/", "text/css", "text/javascript", "application/javascript")):
        verb = "loaded"
    else:
        verb = "viewed"
    note = " (cached)" if status == 304 else f" ({status})" if status >= 300 else ""
    ip = req.get("client_ip") or req.get("remote_ip", "")
    ts = float(d["ts"]) if isinstance(d.get("ts"), (int, float)) else None
    return (level, "caddy", f"{who_caddy(ua)} {verb} {path}{note} from {ip}", ts), (ip, ua), verb


SERVICE_SUBJECT_KEYS = ("identifier", "identifiers", "names", "subjects", "server_name", "storage", "remote")
SERVICE_LEVEL = {"debug": "debug", "info": "info", "warn": "warn", "error": "error", "fatal": "error", "panic": "error", "dpanic": "error"}


def caddy_service(text: str):
    """Caddy's own structured log (certificates, ACME, reverse proxy), as a short line:
    `tls.renew: renewing certificate · sc.example.com`. The part after ` · ` is the subject; explain.py leaves it
    out of the signature. Returns (level, message) or None when `text` is not such a record."""
    if not text.startswith("{"):
        return None
    try:
        d = json.loads(text)
    except ValueError:
        return None
    if not isinstance(d, dict) or not d.get("msg") or "level" not in d:
        return None
    msg = re.sub(r"\s+", " ", str(d["msg"])).strip()
    logger = str(d.get("logger") or "").replace("http.handlers.", "")
    head = f"{logger}: {msg}" if logger else msg
    req = d.get("request") if isinstance(d.get("request"), dict) else None
    subject = ""
    if req:
        uri = re.sub(r"[^\x20-\x7e]", "?", str(req.get("uri", "")))
        uri = uri if len(uri) <= 40 else uri[:39] + "…"
        subject = f"{req.get('method', '?')} {req.get('host', '')}{uri} from {req.get('client_ip') or req.get('remote_ip', '')}"
    else:
        for k in SERVICE_SUBJECT_KEYS:
            v = d.get(k)
            if v:
                subject = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
                break
    if d.get("error") and not subject:
        subject = str(d["error"])
    line = head[:110] + (f" · {subject[:80]}" if subject else "")
    return SERVICE_LEVEL.get(str(d.get("level")).lower(), "info"), line


def parse_caddy(line: str) -> tuple[str, str, str, str | None] | None:
    got = _caddy(line)
    return got[0] if got else None


class HistoryOnly(tuple):
    """A parsed record for `follow_command` to store in history without displaying it."""


class CaddyFold:
    """Stateful Caddy parser: the assets a visitor loads right after viewing a page (fonts, css, images)
    are folded into that view, so one visit is one drop instead of ten. Folded lines still go to history,
    so timeline playback keeps the evidence. A visitor's window stays open while their assets keep coming."""
    FOLD_S = 10.0
    MAX_VISITORS = 2000

    def __init__(self) -> None:
        self._viewing: dict[tuple, float] = {}       # (ip, user agent) -> last page view or folded asset

    def __call__(self, line: str):
        got = _caddy(line)
        if not got:
            return None
        rec, key, verb = got
        t = rec[3] if rec[3] is not None else time.time()
        if verb == "loaded" and 0 <= t - self._viewing.get(key, -1e18) <= self.FOLD_S:
            self._viewing[key] = t
            return HistoryOnly(rec)
        if verb == "viewed":
            self._viewing[key] = t
            if len(self._viewing) > self.MAX_VISITORS:
                self._viewing = {k: v for k, v in self._viewing.items() if t - v <= self.FOLD_S}
        return rec


def parse_journal(line: str) -> tuple[str, str, str, float | None] | None:
    """One `journalctl -o json` record -> (level, source, message, ts), or None to skip."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    msg = d.get("MESSAGE")
    if isinstance(msg, list):          # journald encodes non-UTF8 payloads as byte arrays
        msg = bytes(msg).decode("utf-8", "replace")
    if not msg:
        return None
    try:
        level = PRIORITY.get(int(d.get("PRIORITY", 6)), "info")
    except (TypeError, ValueError):
        level = "info"
    source = d.get("SYSLOG_IDENTIFIER") or d.get("_COMM") or "journal"
    ts = None
    us = d.get("__REALTIME_TIMESTAMP")
    if us:
        try:
            ts = int(us) / 1e6
        except (TypeError, ValueError, OverflowError):
            pass
    msg = str(msg)
    if source == "caddy":
        got = _caddy(msg)                    # other sites' access lines reach us through the journal as raw JSON too
        if got:
            level, _, msg, _ = got[0]
        else:
            svc = caddy_service(msg)
            if svc:
                level, msg = svc
    if source == "kernel" and "[UFW " in msg:
        ufw = parse_ufw(msg)
        if ufw:
            level, source, msg = ufw
    elif source == "sshd" and level == "info" and SSHD_BAD.search(msg):
        level = "warn"                 # probing: sshd logs these at info
    return level, str(source), msg, ts




# One shared connection per host (ControlMaster): every follower is a channel on it. The server rate-limits
# new ssh connections (ufw LIMIT, MaxStartups), so a reconnect storm of four fresh logins after a laptop wake
# would get refused and could even earn a fail2ban ban. The master dies with the service (KillMode=control-group).
_CTL_DIR = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
SSH_OPTS = ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", "ControlMaster=auto", "-o", f"ControlPath={_CTL_DIR}/alm-ssh-%C", "-o", "ControlPersist=60"]


async def _reap(proc) -> None:
    """Stop a child we started. Without this an ssh/tail child outlives the engine."""
    if proc is not None and proc.returncode is None:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), 3)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass


LIVE_WINDOW_S = 60.0             # an entry older than this on arrival is sync (history only), not live
FILE_SYNC_LINES = 20000          # remote files have no --since: re-read this many trailing lines and filter by time
CURSOR_RE = re.compile(r'"__CURSOR"\s*:\s*"([^"]+)"')


def journal_cursor(text: str, rec) -> str | None:
    m = CURSOR_RE.search(text)
    return m.group(1) if m else None


def time_cursor(text: str, rec) -> str | None:
    ts = rec[3] if rec and len(rec) > 3 else None
    return repr(float(ts)) if isinstance(ts, (int, float)) else None


async def follow_command(engine: RainEngine, argv, parse, label: str, *, key: str | None = None,
                         cursor_of=None, resume: str = "cursor", prepare=None) -> None:
    """Run a long-lived command, feed each stdout line through `parse` into the engine, and
    restart it with backoff when it dies. Disconnects surface as a warn from source "alm".
    The child is always terminated when this task is cancelled or the engine shuts down.

    Sync: with `key` and a history, the position reached is stored (`cursor_of(text, rec)` -> str) in the same
    flush as the entries, and `argv` may be a callable taking the stored cursor (or None) so every (re)start
    resumes where the last one stopped. Whatever the outage missed arrives first and is older than
    LIVE_WINDOW_S, so it goes to history only; only fresh entries are shown. resume="time" additionally drops
    records at or before the stored epoch (for `tail`, which cannot resume by itself)."""
    delay, announced = 2.0, False
    hist = engine.history if key else None
    while True:
        started = time.monotonic()
        proc = None
        lines = 0
        cur = hist.get_cursor(key) if hist else None
        try:
            if prepare is not None:
                await prepare()
            args = argv(cur) if callable(argv) else argv
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1 << 20)
            assert proc.stdout is not None
            async for raw in proc.stdout:
                lines += 1
                text = raw.decode("utf-8", "replace")
                rec = parse(text)
                ts = rec[3] if rec and len(rec) > 3 else None
                numeric = isinstance(ts, (int, float))
                if hist and resume == "time" and cur and numeric and ts <= float(cur):
                    continue                                   # already have it
                if rec:
                    if isinstance(rec, HistoryOnly):
                        engine.record(*rec, quiet=True)
                    elif numeric and ts < time.time() - LIVE_WINDOW_S:
                        engine.record(*rec)
                    else:
                        engine.ingest(*rec)
                if hist and cursor_of:
                    c = cursor_of(text, rec)
                    if c:
                        hist.set_cursor(key, c)
            await proc.wait()
            if hist and cur and lines == 0 and proc.returncode not in (0, 255, None) and resume == "cursor":
                hist.set_cursor(key, None)       # the journal no longer knows our cursor (vacuumed): start over
        except OSError:
            pass
        finally:
            await asyncio.shield(_reap(proc))
        if time.monotonic() - started > 30:      # it was healthy for a while: reset backoff
            delay, announced = 2.0, False
        if not announced:
            engine.ingest("warn", "alm", f"lost {label}, retrying")
            announced = True
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60.0)


def journal_argv(hours: float, remote: bool):
    """journalctl that resumes after a stored cursor, else loads `hours` back, else starts at now; then follows."""
    def build(cur: str | None) -> list[str]:
        if cur:
            pos = ["--after-cursor", cur]
        elif hours > 0:
            pos = ["--since", f"{hours:g} hours ago"]
        else:
            pos = ["-n", "0"]
        return ["journalctl", "-f", "-o", "json", *pos]
    return build


async def tail_journal(engine: RainEngine, extra_args: list[str] | None = None, backfill_hours: float = 0) -> None:
    """Follow the local journald, resuming from the stored cursor."""
    build = journal_argv(backfill_hours if engine.history else 0, False)
    await follow_command(engine, lambda cur: build(cur) + list(extra_args or []), parse_journal, "local journal",
                         key="journal:local", cursor_of=journal_cursor)


def ssh_argv(host: str, remote_cmd: str) -> list[str]:
    return ["ssh", *SSH_OPTS, host, remote_cmd]


async def tail_ssh_journal(engine: RainEngine, host: str, backfill_hours: float = 0) -> None:
    build = journal_argv(backfill_hours if engine.history else 0, True)
    await follow_command(engine, lambda cur: ssh_argv(host, shlex.join(build(cur))), parse_journal,
                         f"{host} journal", key=f"journal:{host}", cursor_of=journal_cursor)


class Fail2ban:
    """fail2ban stamps server-local time without a zone. With the host's UTC offset (`date +%z` over ssh)
    each line gets a real epoch, which lets the file be re-read after an outage. Without it, lines are
    stamped on arrival, as before, and nothing can be synced."""
    STAMP = re.compile(r"^(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)")

    def __init__(self) -> None:
        self.offset: float | None = None

    @property
    def timed(self) -> bool:
        return self.offset is not None

    def __call__(self, line: str):
        rec = parse_fail2ban(line)
        if rec and self.offset is not None:
            m = self.STAMP.match(line)
            if m:
                import calendar
                ts = calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0)) - self.offset
                rec = (rec[0], rec[1], rec[2], ts)
        return rec

    async def prepare_for(self, host: str) -> None:
        try:
            p = await asyncio.create_subprocess_exec(*ssh_argv(host, "date +%z"), stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 20)
            z = out.decode().strip()
            if re.fullmatch(r"[+-]\d{4}", z):
                self.offset = (1 if z[0] == "+" else -1) * (int(z[1:3]) * 3600 + int(z[3:5]) * 60)
        except (OSError, asyncio.TimeoutError):
            pass


class Plain:
    timed = False

    def __call__(self, line: str):
        return parse_line(line) if line.strip() else None


CaddyFold.timed = True

FILE_PARSERS = {
    # factories: a parser may keep state, and each followed file gets its own
    "caddy": CaddyFold,
    "fail2ban": Fail2ban,
    "plain": Plain,
}


async def tail_ssh_file(engine: RainEngine, host: str, path: str, parser: str, sudo: bool = False) -> None:
    p = FILE_PARSERS[parser]()
    prefix = "sudo -n " if sudo else ""

    def build(cur):
        # timed sources re-read a long tail and let follow_command drop what history already has;
        # the others can only follow from now
        n = FILE_SYNC_LINES if p.timed else 0
        return ssh_argv(host, f"{prefix}tail -F -n {n} {shlex.quote(path)}")
    prep = (lambda: p.prepare_for(host)) if hasattr(p, "prepare_for") else None
    await follow_command(engine, build, p, f"{host}:{path}", key=f"file:{host}:{path}", cursor_of=time_cursor,
                         resume="time", prepare=prep)


# -- collector: a daemon on the watched host that keeps the record (alm_collector.py) --------------------------

COLLECTOR_CMD = "python3 ~/alm-collector/alm_collector.py"
COLLECTOR_CURSOR = re.compile(r'"c":"([^"]+)"')


def parse_collector(line: str):
    """One collector line -> (level, source, message, epoch), HistoryOnly for a folded asset, None for a heartbeat."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict) or "m" not in d:
        return None
    rec = (d.get("l", "info"), str(d.get("o", "?")), str(d["m"]), d.get("t"))
    return HistoryOnly(rec) if d.get("q") else rec


def collector_cursor(text: str, rec) -> str | None:
    m = COLLECTOR_CURSOR.search(text)
    return m.group(1) if m else None


STALE_S = 60.0


class CollectorWatch:
    """parse_collector plus a check on the heartbeats: the stream keeps running when the collector daemon is
    dead (it only reads the store), so a heartbeat whose liveness age is stale means nothing is being collected."""

    def __init__(self, engine, label: str) -> None:
        self.engine, self.label, self.stale = engine, label, False

    def __call__(self, line: str):
        rec = parse_collector(line)
        if rec is None and '"hb"' in line:
            try:
                d = json.loads(line)
            except ValueError:
                return None
            if "age" not in d:               # an older collector that does not report liveness
                return None
            age = d["age"]
            stale = age is None or age > STALE_S
            if stale and not self.stale:
                self.engine.ingest("warn", "alm", f"collector on {self.label} is not collecting")
            elif self.stale and not stale:
                self.engine.ingest("info", "alm", f"collector on {self.label} is collecting again")
            self.stale = stale
        return rec


async def tail_collector(engine: RainEngine, host: str | None, backfill_hours: float = 24,
                         cmd: str = COLLECTOR_CMD) -> None:
    """Sync from a collector: everything it stored after our cursor (or the last `backfill_hours` on the
    first run), then follow. One ssh channel carries the lot. `host` None runs it locally (tests)."""
    watch = CollectorWatch(engine, host or "local")

    def build(cur: str | None) -> list[str]:
        since = max(0.0, time.time() - backfill_hours * 3600) if backfill_hours > 0 else time.time()
        remote = f"{cmd} stream --after {shlex.quote(cur or '')} --since {since:.0f}"
        return ssh_argv(host, remote) if host else ["sh", "-c", "exec " + remote]
    await follow_command(engine, build, watch, f"{host or 'local'} collector",
                         key=f"collector:{host or 'local'}", cursor_of=collector_cursor)
