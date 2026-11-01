"""
Runs the terminal UI and the web API side by side, both reading and steering
the same RainEngine. Whatever you catch/dismiss/toggle in one view is visible
and actionable from the other, live.

Usage:
    python3 main.py                  # terminal UI + web UI on :8000
    python3 main.py --no-web         # terminal UI only
    python3 main.py --web-only       # just the web API/UI, no terminal
    python3 main.py --port 8080
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import uvicorn

from rain_engine import RainEngine
from history import History
from tail import (FILE_PARSERS, backfill_journal, tail_file, tail_journal, tail_ssh_file,
                  tail_ssh_journal)
from rain_tui import LogRainApp
from rain_web import create_app


def default_db() -> str:
    """~/.local/share/alm/history.db; keeps using the pre-rename spot/ file until it is moved."""
    base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    new, old = (os.path.join(base, d, "history.db") for d in ("alm", "spot"))
    return old if os.path.exists(old) and not os.path.exists(new) else new


async def run(host: str, port: int, use_web: bool, use_tui: bool, tails: list[str],
              journal: bool, token: str | None, ssh_hosts: list[str], ssh_files: list[str],
              history: History | None = None, backfill_hours: float = 0) -> None:
    real = bool(tails or journal or ssh_hosts or ssh_files)
    engine = RainEngine(mock=not real, history=history if real else None)
    await engine.start()
    for path in tails:
        asyncio.create_task(tail_file(engine, path))
    if journal:
        asyncio.create_task(tail_journal(engine))
        asyncio.create_task(backfill_journal(engine, backfill_hours))
    for remote in ssh_hosts:     # preset: remote journal (sshd, kernel firewall, services) + fail2ban
        asyncio.create_task(tail_ssh_journal(engine, remote))
        asyncio.create_task(backfill_journal(engine, backfill_hours, remote))
        asyncio.create_task(tail_ssh_file(engine, remote, "/var/log/fail2ban.log", "fail2ban"))
    for spec in ssh_files:       # host:path:parser[:sudo]
        remote, path, parser, *rest = spec.split(":")
        if parser not in FILE_PARSERS:
            raise SystemExit(f"--ssh-file: unknown parser {parser!r}, choose from {sorted(FILE_PARSERS)}")
        asyncio.create_task(tail_ssh_file(engine, remote, path, parser, sudo="sudo" in rest))

    server_task = None
    if use_web:
        api = create_app(engine, token)
        config = uvicorn.Config(api, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        server_task = asyncio.create_task(server.serve())
        if not use_tui:
            print(f"ALM web UI at http://{host}:{port}  (Ctrl+C to quit)")

    if use_tui:
        app = LogRainApp(engine)
        await app.run_async()
        if server_task:
            server.should_exit = True
            await server_task
    elif server_task:
        await server_task


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-web", action="store_true", help="terminal UI only, no web server")
    parser.add_argument("--web-only", action="store_true", help="no terminal UI, just serve the web app")
    parser.add_argument("--tail", metavar="FILE", action="append", default=[],
                        help="ingest from a log file instead of mock data (repeatable)")
    parser.add_argument("--ssh", metavar="HOST", action="append", default=[],
                        help="follow a remote machine over ssh: its journal and fail2ban log (repeatable)")
    parser.add_argument("--ssh-file", metavar="HOST:PATH:PARSER[:sudo]", action="append", default=[],
                        help="follow one remote file; parser is caddy, fail2ban or plain")
    parser.add_argument("--db", default=default_db(), help="history database (default: %(default)s)")
    parser.add_argument("--no-history", action="store_true", help="do not store history (no timeline)")
    parser.add_argument("--keep-days", type=float, default=7, help="history retention in days (default 7)")
    parser.add_argument("--max-rows", type=int, default=5_000_000,
                        help="history row cap, oldest dropped first (default 5000000, roughly 1.5 GB)")
    parser.add_argument("--backfill", type=float, default=24, metavar="HOURS",
                        help="hours of journal to load into history at start (default 24, 0 disables)")
    parser.add_argument("--journal", action="store_true", help="ingest from journald (journalctl -f)")
    parser.add_argument("--token", default=os.environ.get("ALM_TOKEN") or os.environ.get("SPOT_TOKEN"),
                        help="shared secret required on /ws and /api/state (or ALM_TOKEN)")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
        print("WARNING: listening on a non-loopback address without --token; anyone who can reach "
              "this port can read your logs and steer ALM.", file=sys.stderr)

    asyncio.run(run(args.host, args.port, use_web=not args.no_web, use_tui=not args.web_only,
                    tails=args.tail, journal=args.journal, token=args.token,
                    ssh_hosts=args.ssh, ssh_files=args.ssh_file,
                    history=None if args.no_history else History(args.db, args.keep_days * 86400, args.max_rows),
                    backfill_hours=args.backfill))
