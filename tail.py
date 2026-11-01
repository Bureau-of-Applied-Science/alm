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
import time

from rain_engine import RainEngine

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




SSH_OPTS = ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]


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


async def follow_command(engine: RainEngine, argv: list[str], parse, label: str) -> None:
    """Run a long-lived command, feed each stdout line through `parse` into the engine, and
    restart it with backoff when it dies. Disconnects surface as a warn from source "alm".
    The child is always terminated when this task is cancelled or the engine shuts down."""
    delay, announced = 2.0, False
    while True:
        started = time.monotonic()
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1 << 20)
            assert proc.stdout is not None
            async for raw in proc.stdout:
                rec = parse(raw.decode("utf-8", "replace"))
                if isinstance(rec, HistoryOnly):
                    engine.record(*rec)
                elif rec:
                    engine.ingest(*rec)
            await proc.wait()
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


async def tail_journal(engine: RainEngine, extra_args: list[str] | None = None) -> None:
    """Follow the local journald from now on."""
    await follow_command(engine, ["journalctl", "-f", "-n", "0", "-o", "json", *(extra_args or [])],
                         parse_journal, "local journal")


def ssh_argv(host: str, remote_cmd: str) -> list[str]:
    return ["ssh", *SSH_OPTS, host, remote_cmd]


async def tail_ssh_journal(engine: RainEngine, host: str) -> None:
    await follow_command(engine, ssh_argv(host, "journalctl -f -n 0 -o json"), parse_journal, f"{host} journal")


FILE_PARSERS = {
    # factories: a parser may keep state, and each followed file gets its own
    "caddy": CaddyFold,
    "fail2ban": lambda: parse_fail2ban,
    "plain": lambda: (lambda line: parse_line(line) if line.strip() else None),
}


async def tail_ssh_file(engine: RainEngine, host: str, path: str, parser: str, sudo: bool = False) -> None:
    cmd = f"{'sudo -n ' if sudo else ''}tail -F -n 0 {shlex.quote(path)}"
    await follow_command(engine, ssh_argv(host, cmd), FILE_PARSERS[parser](), f"{host}:{path}")


async def backfill_journal(engine: RainEngine, hours: float, host: str | None = None) -> int:
    """Load the last `hours` of a journal (local, or over ssh) into history so a timeline has something
    to play right after a restart. History only: nothing is displayed and no burst alerts fire."""
    if hours <= 0 or engine.history is None:
        return 0
    since = f"{hours:g} hours ago"
    argv = (ssh_argv(host, f"journalctl --since {shlex.quote(since)} -o json --no-pager")
            if host else ["journalctl", "--since", since, "-o", "json", "--no-pager"])
    n = 0
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1 << 20)
        assert proc.stdout is not None
        async for raw in proc.stdout:
            rec = parse_journal(raw.decode("utf-8", "replace"))
            if rec:
                engine.record(*rec)
                n += 1
        await proc.wait()
    except OSError:
        pass
    finally:
        await asyncio.shield(_reap(proc))
    return n
