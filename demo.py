"""Synthetic data for trying ALM without any server: `main.py --demo`.

Everything here is invented. Addresses come from the documentation ranges (RFC 5737 and RFC 3849), hosts are
example.com, users are made up, and the random generator is seeded, so the same run looks the same (which is
what the README screenshots rely on). Entries use the same shapes the real parsers produce, so the inspect
view's explanations and the timeline digest behave as they do on real logs.

`seed(history, now)` writes a day of history, with a few incidents, so replay has something to play.
`feed(engine)` then drips live entries, with an occasional probe wave, until cancelled.
"""
from __future__ import annotations

import asyncio
import random
from typing import Iterator

V4 = ("192.0.2.", "198.51.100.", "203.0.113.")
DOC_V6 = "2001:0db8:{:04x}:0000:0000:0000:0000:{:04x}"   # written out in full, as the kernel firewall log does
PORTS = (22, 23, 80, 443, 445, 1433, 2375, 3306, 3389, 5060, 5432, 5900, 6379, 8080, 8443, 9200, 27017)
USERS = ("admin", "root", "ubuntu", "test", "oracle", "git", "postgres", "user", "pi", "deploy")
VISITORS = ("Safari on iPhone", "Chrome on Windows", "Firefox on Linux", "Chrome on Android", "Safari on Mac",
            "Edge on Windows", "Chrome on Mac")
PAGES = ("/", "/", "/pricing", "/docs", "/blog/launch-notes", "/about", "/status")
BOTS = ("Googlebot", "bingbot", "GPTBot", "AhrefsBot", "curl", "python-requests")
PROBE_PATHS = ("/.env", "/wp-login.php", "/xmlrpc.php", "/.git/config", "/phpmyadmin/", "/admin/config.php",
               "/cgi-bin/luci", "/actuator/health", "/.aws/credentials")
ASSETS = ("/assets/app.css", "/assets/app.js", "/fonts/inter.woff2", "/favicon.ico", "/img/hero.webp")
Entry = tuple[str, str, str]                    # level, source, message


def ip4(rng: random.Random) -> str:
    return rng.choice(V4) + str(rng.randint(2, 250))


def ip6(rng: random.Random) -> str:
    return DOC_V6.format(rng.randint(0x100, 0xfff), rng.randint(1, 0xfff))


def any_ip(rng: random.Random) -> str:
    return ip6(rng) if rng.random() < 0.08 else ip4(rng)


# -- one entry of each kind, shaped like the real parsers' output -------------------------------------------

def firewall(rng: random.Random, ip: str | None = None) -> Entry:
    proto = rng.choices(("TCP", "UDP"), (0.9, 0.1))[0]
    return "info", "ufw", f"block {proto} {ip or any_ip(rng)} -> :{rng.choice(PORTS)}"


def ssh_probe(rng: random.Random, ip: str | None = None) -> list[Entry]:
    ip = ip or ip4(rng)
    user, port = rng.choice(USERS), rng.randint(30000, 65000)
    return [("warn", "sshd", f"Invalid user {user} from {ip} port {port}"),
            ("warn", "sshd", f"Connection closed by invalid user {user} {ip} port {port} [preauth]")]


def web_view(rng: random.Random) -> Entry:
    who, ip = rng.choice(VISITORS), any_ip(rng)
    r = rng.random()
    if r < 0.7:
        return "info", "caddy", f"{who} viewed {rng.choice(PAGES)} from {ip}"
    if r < 0.85:
        return "info", "caddy", f"{rng.choice(BOTS)} viewed {rng.choice(PAGES)} from {ip}"
    return "info", "caddy", f"{who} was redirected at / (308) from {ip}"


def web_probe(rng: random.Random, ip: str | None = None) -> Entry:
    who = rng.choice(BOTS + VISITORS)
    return "warn", "caddy", f"{who} probed {rng.choice(PROBE_PATHS)} (404) from {ip or ip4(rng)}"


def web_error(rng: random.Random) -> Entry:
    code = rng.choice((500, 502, 503, 504))
    verb = rng.choice(("sent POST to /api/checkout", "requested /api/orders", "requested /api/items"))
    return "error", "caddy", f"{rng.choice(VISITORS)} {verb} ({code}) from {ip4(rng)}"


def admin_session(rng: random.Random) -> list[Entry]:
    ip, port, n = "192.0.2." + str(rng.randint(10, 20)), rng.randint(40000, 60000), rng.randint(1500, 1900)
    return [("info", "sshd", f"Accepted publickey for ubuntu from {ip} port {port} ssh2: RSA SHA256:demo"),
            ("info", "sshd", "pam_unix(sshd:session): session opened for user ubuntu(uid=1000) by (uid=0)"),
            ("info", "systemd-logind", f"New session {n} of user ubuntu."),
            ("info", "systemd", f"Started Session {n} of User ubuntu."),
            ("info", "sshd", f"Received disconnect from {ip} port {port}:11: disconnected by user"),
            ("info", "sshd", "pam_unix(sshd:session): session closed for user ubuntu"),
            ("info", "systemd-logind", f"Removed session {n}.")]


def cron(rng: random.Random) -> list[Entry]:
    return [("info", "CRON", "pam_unix(cron:session): session opened for user root(uid=0) by (uid=0)"),
            ("info", "CRON", "(root) CMD (   cd / && run-parts --report /etc/cron.hourly)"),
            ("info", "CRON", "pam_unix(cron:session): session closed for user root")]


def ban_cycle(rng: random.Random, ip: str) -> list[Entry]:
    return ([("warn", "f2b:sshd", f"found {ip}")] * 3) + [("error", "f2b:sshd", f"ban {ip}")]


# -- a day of history ---------------------------------------------------------------------------------------

def day(rng: random.Random, now: float, hours: float = 24.0) -> Iterator[tuple[float, Entry]]:
    """Yield (time, entry) for the last `hours`, oldest first, with three incidents baked in."""
    start = now - hours * 3600
    t = start
    while t < now - 5:
        t += rng.expovariate(1 / 14.0)                       # a baseline entry about every 14 s
        r = rng.random()
        if r < 0.42:
            yield t, firewall(rng)
        elif r < 0.67:
            yield t, web_view(rng)
        elif r < 0.78:
            for k, e in enumerate(ssh_probe(rng)):
                yield t + k * 0.4, e
        elif r < 0.84:
            yield t, web_probe(rng)
        elif r < 0.86:
            ip = ip4(rng)
            for k, e in enumerate(ban_cycle(rng, ip)):
                yield t + k * 9, e
        elif r < 0.875:
            for k, e in enumerate(admin_session(rng)):
                yield t + k * 2.5, e
    for h in range(int(hours)):                              # hourly cron
        for k, e in enumerate(cron(rng)):
            yield start + h * 3600 + 120 + k, e
    # incident 1, ~6 h ago: one host hammers ssh and the firewall
    t0, bad = now - 6 * 3600, "203.0.113.77"
    for i in range(14):
        yield t0 + i * 3.5, (firewall(rng, bad) if i % 2 else ssh_probe(rng, bad)[0])
    yield t0 + 52, ("error", "alm", f"{bad} hit us 14x in 60s (3 ports, sshd+ufw)")
    for k, e in enumerate(ban_cycle(rng, bad)):
        yield t0 + 58 + k * 6, e
    # incident 2, ~2 h ago: the app falls over for a few minutes
    t1 = now - 2 * 3600
    for i in range(34):
        yield t1 + i * 6.5, web_error(rng) if i % 5 else ("warn", "caddy", f"Chrome on Windows requested /api/orders (504) from {ip4(rng)}")
    yield t1 + 230, ("info", "caddy", f"Safari on iPhone sent POST to /api/checkout from {ip4(rng)}")
    # incident 3, ~20 min ago: a probe wave, so the 30-minute replay has something to show
    t2, wave = now - 20 * 60, "198.51.100.23"
    for i, p in enumerate(PROBE_PATHS):
        yield t2 + i * 2.2, ("warn", "caddy", f"curl probed {p} (404) from {wave}")
    yield t2 + 30, ("error", "alm", f"{wave} hit us 9x in 60s (caddy)")
    for k, e in enumerate(ban_cycle(rng, wave)):
        yield t2 + 40 + k * 7, e


def seed(history, now: float, seed_value: int = 7, hours: float = 24.0) -> int:
    rng = random.Random(seed_value)
    rows, seen = [], set()
    for t, (level, source, message) in sorted(day(rng, now, hours), key=lambda x: x[0]):
        if t >= now - 3:
            continue
        while (t, source, message) in seen:
            t += 0.001
        seen.add((t, source, message))
        rows.append((t, level, source, message))
    history.add_many(rows)
    history.flush()
    return len(rows)


# -- the live feed ------------------------------------------------------------------------------------------

async def feed(engine, seed_value: int = 11, pace: float = 1.0) -> None:
    """Drip live entries. `pace` above 1 is faster. A probe wave about every minute keeps warn and error in view."""
    rng = random.Random(seed_value)
    since_wave = 0.0
    while True:
        r = rng.random()
        if r < 0.40:
            batch = [firewall(rng)]
        elif r < 0.68:
            batch = [web_view(rng)]
        elif r < 0.80:
            batch = ssh_probe(rng)
        elif r < 0.86:
            batch = [web_probe(rng)]
        elif r < 0.89:
            batch = admin_session(rng)[:3]
        elif r < 0.92:
            batch = [web_error(rng)]
        else:
            batch = cron(rng)[:2]
        for level, source, message in batch:
            engine.ingest(level, source, message)
            await asyncio.sleep(0.15 / pace)
        gap = rng.uniform(0.5, 1.6) / pace
        since_wave += gap
        if since_wave > 55 / pace:
            since_wave = 0.0
            ip = ip4(rng)
            for e in ssh_probe(rng, ip) + [web_probe(rng, ip)] + ban_cycle(rng, ip)[2:]:
                engine.ingest(*e)
                await asyncio.sleep(0.4 / pace)
        await asyncio.sleep(gap)
