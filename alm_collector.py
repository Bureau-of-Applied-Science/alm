#!/usr/bin/env python3
"""ALM collector: runs on the watched host and keeps the record, so a screen (the Chromebook) is only a
node that syncs and reviews.

    alm_collector.py run    [--db PATH] [--file PATH:PARSER[:sudo]]... [--no-journal] [--keep-days N]
    alm_collector.py stream [--db PATH] [--after CURSOR] [--since EPOCH] [--once]
    alm_collector.py status [--db PATH]

`run` follows the host's journal and log files with the same parsers ALM uses (tail.py), stores every
entry in SQLite with a sequence number, and raises burst alerts (burst.py) as ordinary `alm` entries, so
they exist even while no screen is watching. `stream` is what a client runs over ssh: it prints every
entry after a cursor as one JSON line, then follows. A cursor is `<store id>:<seq>`; a client that
presents a cursor from a different store (the file was reset) is restarted from `--since`.

Needs only Python 3.10+ and the standard library, plus tail.py and burst.py from this repo beside it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sqlite3
import sys
import time
import uuid

import tail
from burst import BurstDetector

DEFAULT_DB = os.environ.get("ALM_COLLECTOR_DB", "~/.local/share/alm-collector/collector.db")
MAX_MESSAGE = 4000
HEARTBEAT_S = 15.0


class Store:
    """The collector's database. Also the `history` the followers write their cursors into."""

    def __init__(self, path: str, keep_s: float = 14 * 86400, max_rows: int = 3_000_000) -> None:
        self.keep_s, self.max_rows = keep_s, max_rows
        if path != ":memory:":
            path = os.path.expanduser(path)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS entries(seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "t REAL NOT NULL, level TEXT NOT NULL, source TEXT NOT NULL, message TEXT NOT NULL, "
                         "quiet INTEGER NOT NULL DEFAULT 0)")
        self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_entries ON entries(t, source, message)")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_entries_t ON entries(t)")
        self._db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        self._db.execute("INSERT OR IGNORE INTO meta VALUES('id', ?)", (uuid.uuid4().hex[:12],))
        self._db.commit()
        self.id = self._db.execute("SELECT v FROM meta WHERE k='id'").fetchone()[0]
        self._buf: list[tuple] = []
        self._cur: dict[str, str | None] = {}

    # -- writing (buffered; flush() commits entries and cursors together) --------------------------------------
    def add(self, t: float, level: str, source: str, message: str, quiet: bool = False) -> None:
        self._buf.append((float(t), level, source, message[:MAX_MESSAGE], 1 if quiet else 0))

    def get_cursor(self, key: str) -> str | None:
        if key in self._cur:
            return self._cur[key]
        row = self._db.execute("SELECT v FROM meta WHERE k=?", ("cur:" + key,)).fetchone()
        return row[0] if row else None

    def set_cursor(self, key: str, value: str | None) -> None:
        self._cur[key] = value

    def flush(self) -> int:
        buf, self._buf = self._buf, []
        cur, self._cur = self._cur, {}
        if not buf and not cur:
            return 0
        if buf:
            self._db.executemany("INSERT OR IGNORE INTO entries(t,level,source,message,quiet) VALUES (?,?,?,?,?)", buf)
        for k, v in cur.items():
            if v is None:
                self._db.execute("DELETE FROM meta WHERE k=?", ("cur:" + k,))
            else:
                self._db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("cur:" + k, v))
        self._db.commit()
        return len(buf)

    def prune(self, now: float) -> int:
        self.flush()
        n = self._db.execute("DELETE FROM entries WHERE t < ?", (now - self.keep_s,)).rowcount
        excess = self._db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] - self.max_rows
        if excess > 0:
            n += self._db.execute("DELETE FROM entries WHERE seq IN (SELECT seq FROM entries ORDER BY t LIMIT ?)",
                                  (excess,)).rowcount
        self._db.commit()
        return n

    # -- reading -----------------------------------------------------------------------------------------------
    def after(self, seq: int, limit: int = 5000) -> list[tuple]:
        return self._db.execute("SELECT seq, t, level, source, message, quiet FROM entries WHERE seq > ? "
                                "ORDER BY seq LIMIT ?", (seq, limit)).fetchall()

    def seq_before(self, since: float) -> int:
        """The last seq whose entry is older than `since` (so streaming `after` it starts at `since`)."""
        row = self._db.execute("SELECT MAX(seq) FROM entries WHERE t < ?", (since,)).fetchone()
        return row[0] or 0

    def touch(self) -> None:
        """Liveness stamp. `stream` reports its age so a client can tell a dead daemon from a quiet host."""
        self._db.execute("INSERT OR REPLACE INTO meta VALUES('alive', ?)", (repr(time.time()),))
        self._db.commit()

    def alive_age(self) -> float | None:
        row = self._db.execute("SELECT v FROM meta WHERE k='alive'").fetchone()
        return max(0.0, time.time() - float(row[0])) if row else None

    def stats(self) -> dict:
        n, lo, hi, top = self._db.execute("SELECT COUNT(*), MIN(t), MAX(t), MAX(seq) FROM entries").fetchone()
        return {"id": self.id, "entries": n, "oldest": lo, "newest": hi, "seq": top or 0, "alive_age": self.alive_age()}


class Sink:
    """Stands in for the engine that tail.follow_command feeds: stores everything, raises burst alerts."""

    def __init__(self, store: Store) -> None:
        self.history = store
        self.burst = BurstDetector()

    def record(self, level: str, source: str, message: str, ts=None, quiet: bool = False) -> None:
        self.history.add(ts if isinstance(ts, (int, float)) else time.time(), level, source, message, quiet)

    def ingest(self, level: str, source: str, message: str, ts=None) -> None:
        self.record(level, source, message, ts)
        alert = self.burst.observe(time.monotonic(), level, source, message)
        if alert:
            self.record(*alert)


async def run(args) -> None:
    store = Store(args.db, args.keep_days * 86400, args.max_rows)
    sink = Sink(store)
    tasks = []
    if not args.no_journal:
        build = tail.journal_argv(args.backfill, False)
        tasks.append(tail.follow_command(sink, build, tail.parse_journal, "journal", key="journal",
                                         cursor_of=tail.journal_cursor))
    for spec in args.file:
        # PATH:PARSER[:sudo]; paths containing ':' are unsupported, as in --ssh-file
        path, parser, *rest = spec.split(":")
        if parser not in tail.FILE_PARSERS:
            raise SystemExit(f"--file: unknown parser {parser!r}, choose from {sorted(tail.FILE_PARSERS)}")
        tasks.append(_follow_file(sink, path, parser, "sudo" in rest))

    async def housekeeping() -> None:
        last_prune = 0.0
        while True:
            await asyncio.sleep(1.0)
            store.flush()
            store.touch()
            if time.time() - last_prune > 3600:
                store.prune(time.time())
                last_prune = time.time()

    print(f"alm-collector: store {store.id}, db {os.path.expanduser(args.db)}, "
          f"{len(tasks)} source(s)", file=sys.stderr, flush=True)
    main = asyncio.gather(housekeeping(), *tasks)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):       # cancel so every follower reaps its tail/journalctl child
        loop.add_signal_handler(sig, main.cancel)
    try:
        await main
    except asyncio.CancelledError:
        pass
    finally:
        await asyncio.sleep(0.5)                       # let the shielded child reaps finish
        store.flush()


def _follow_file(sink: Sink, path: str, parser: str, sudo: bool):
    p = tail.FILE_PARSERS[parser]()
    if hasattr(p, "offset"):                      # fail2ban stamps this host's local time
        p.offset = float(time.localtime().tm_gmtoff)

    def build(cur):
        n = tail.FILE_SYNC_LINES if p.timed else 0
        return [*(["sudo", "-n"] if sudo else []), "tail", "-F", "-n", str(n), path]
    return tail.follow_command(sink, build, p, path, key=f"file:{path}", cursor_of=tail.time_cursor, resume="time")


def stream(args) -> None:
    """Print entries after the cursor as JSON lines, then follow. Heartbeats show the link is alive."""
    path = os.path.expanduser(args.db)
    if not os.path.exists(path):
        print(json.dumps({"hb": time.time(), "error": "no store yet", "age": None}), flush=True)
    store = Store(args.db)            # opening creates it if absent; harmless, and the daemon then fills it
    seq = None
    if args.after and ":" in args.after:
        sid, _, n = args.after.partition(":")
        if sid == store.id and n.isdigit():
            seq = int(n)
    if seq is None:
        seq = store.seq_before(args.since) if args.since else 0
    last_beat = 0.0
    while True:
        rows = store.after(seq)
        for s, t, level, source, message, quiet in rows:
            seq = s
            print(json.dumps({"c": f"{store.id}:{s}", "t": t, "l": level, "o": source, "m": message, "q": quiet},
                             separators=(",", ":")), flush=True)
        if len(rows) < 5000:
            if args.once:
                return
            if time.time() - last_beat >= HEARTBEAT_S:
                print(json.dumps({"hb": time.time(), "c": f"{store.id}:{seq}", "age": store.alive_age()}), flush=True)
                last_beat = time.time()
            time.sleep(1.0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "stream", "status"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=DEFAULT_DB)
    r = sub.choices["run"]
    r.add_argument("--file", action="append", default=[], metavar="PATH:PARSER[:sudo]")
    r.add_argument("--no-journal", action="store_true")
    r.add_argument("--backfill", type=float, default=48, metavar="HOURS",
                   help="journal window on the very first run (default 48)")
    r.add_argument("--keep-days", type=float, default=14)
    r.add_argument("--max-rows", type=int, default=3_000_000)
    s = sub.choices["stream"]
    s.add_argument("--after", default="")
    s.add_argument("--since", type=float, default=0.0)
    s.add_argument("--once", action="store_true", help="exit after the backlog (tests)")
    args = ap.parse_args()
    try:
        if args.cmd == "run":
            asyncio.run(run(args))
        elif args.cmd == "stream":
            stream(args)
        else:
            print(json.dumps(Store(args.db).stats()))
    except (KeyboardInterrupt, BrokenPipeError):
        pass


if __name__ == "__main__":
    main()
