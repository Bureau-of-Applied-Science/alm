#!/usr/bin/env python3
"""Diagnose an ALM install: python, dependencies, settings, the service, the web page, and any collector.

    .venv/bin/python tools/doctor.py        (or ./install.sh --check)

Prints one line per check ("ok" or "!!") and exits 1 if anything failed. Read-only: it starts nothing
and changes nothing. Settings come from ~/.config/alm/alm.env, overridden by the environment.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILED = 0


def report(ok: bool, text: str) -> bool:
    global FAILED
    FAILED += 0 if ok else 1
    print(("ok  " if ok else "!!  ") + text)
    return ok


def note(text: str) -> None:
    print("    " + text)


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


def run(argv: list[str], timeout: float = 25) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 255, str(e)


def check_collector(host: str, cmd: str) -> None:
    code, out = run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "true"])
    if not report(code == 0, f"ssh to {host} with key authentication"):
        note(f"fix: ssh-copy-id {host}   (BatchMode never prompts, so a password login does not count)")
        return
    code, out = run(["ssh", "-T", "-o", "BatchMode=yes", host, f"{cmd} status"])
    if not report(code == 0 and out.startswith("{"), f"collector is installed on {host}"):
        note(f"fix: deploy/install-collector.sh {host}")
        return
    try:
        st = json.loads(out.splitlines()[-1])
    except ValueError:
        report(False, f"collector on {host} answered something unreadable: {out[:80]}")
        return
    age = st.get("alive_age")
    if age is None:
        report(False, f"collector on {host} reports no liveness (old version or the daemon never ran)")
        note(f"fix: deploy/install-collector.sh {host}   (redeploys and restarts it)")
    else:
        report(age < 60, f"collector daemon on {host} is running (last beat {age:.0f}s ago)")
    newest = st.get("newest")
    if newest:
        lag = time.time() - newest
        report(lag < 3600, f"collector has {st.get('entries', 0)} entries, newest {lag:.0f}s old")
    else:
        report(False, f"collector on {host} has stored nothing yet")


def main() -> int:
    v = sys.version_info
    report(v >= (3, 10), f"Python {v.major}.{v.minor}.{v.micro} (3.10 or newer needed)")
    missing = []
    for mod in ("textual", "fastapi", "uvicorn", "websockets"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    report(not missing, "dependencies import" if not missing else f"missing: {', '.join(missing)} (run ./install.sh)")

    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    env_path = cfg / "alm" / "alm.env"
    env = read_env(env_path)
    env.update({k: os.environ[k] for k in ("ALM_COLLECTOR", "ALM_ARGS", "ALM_TOKEN", "ALM_COLLECTOR_CMD") if k in os.environ})
    if env_path.exists():
        mode = stat.S_IMODE(env_path.stat().st_mode)
        secret_exposed = "ALM_TOKEN" in env and mode & 0o077
        report(not secret_exposed, f"settings file {env_path}" + (" holds a token but is readable by others (chmod 600)" if secret_exposed else ""))
    else:
        note(f"no settings file ({env_path}); fine for the demo, ./install.sh --service creates it")

    code, out = run(["systemctl", "--user", "is-active", "alm"], 10)
    unit = out.strip()
    if code == 255:
        note("systemd not available here; run main.py by hand")
    elif unit == "active":
        report(True, "service alm is active")
    else:
        note(f"service alm is {unit or 'not installed'} (./install.sh --service installs it)")

    url = "http://127.0.0.1:8000/api/state" + (f"?token={env['ALM_TOKEN']}" if env.get("ALM_TOKEN") else "")
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            snap = json.load(r)
        report(True, f"web page answers on http://localhost:8000/ ({len(snap.get('streams', []))} drops on screen)")
    except urllib.error.HTTPError as e:
        report(e.code != 401, "web page answers on :8000" + (" but rejects the token" if e.code == 401 else f" with HTTP {e.code}"))
    except (OSError, ValueError):
        note("nothing answers on http://localhost:8000/ (not running, or another port)")

    hosts = [h.strip() for h in env.get("ALM_COLLECTOR", "").split(",") if h.strip()]
    cmd = env.get("ALM_COLLECTOR_CMD") or "python3 ~/alm-collector/alm_collector.py"
    if not hosts:
        note("no ALM_COLLECTOR set: running on demo data or direct sources")
    for host in hosts:
        check_collector(host, cmd)
    print()
    print("all good" if not FAILED else f"{FAILED} problem(s) found")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
