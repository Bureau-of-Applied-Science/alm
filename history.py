"""Persistent entry history (SQLite) behind timeline playback, DESIGN 5.6.

Every ingested entry is stored with an epoch timestamp, before display sampling, so playback has the
evidence. Writes are buffered and flushed by the engine's reap loop; duplicates (a journal backfill
overlapping what was already stored or followed live) are ignored by a unique index.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterable

MAX_MESSAGE = 4000
Row = tuple[float, str, str, str]       # epoch, level, source, message


class History:
    def __init__(self, path: str | Path = ":memory:", keep_s: float = 7 * 86400, max_rows: int = 5_000_000):
        self.keep_s = keep_s
        self.max_rows = max_rows
        if str(path) != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            path = str(Path(path).expanduser())
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS entries("
                         "t REAL NOT NULL, level TEXT NOT NULL, source TEXT NOT NULL, message TEXT NOT NULL)")
        self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_entries ON entries(t, source, message)")
        self._db.execute("CREATE TABLE IF NOT EXISTS explanations("
                         "sig TEXT PRIMARY KEY, source TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL, "
                         "what TEXT NOT NULL, why TEXT NOT NULL, by TEXT NOT NULL, created REAL NOT NULL)")
        self._db.commit()
        self._buf: list[Row] = []

    def add(self, t: float, level: str, source: str, message: str) -> None:
        self._buf.append((float(t), level, source, message[:MAX_MESSAGE]))

    def add_many(self, rows: Iterable[Row]) -> None:
        self._buf.extend((float(t), l, s, m[:MAX_MESSAGE]) for t, l, s, m in rows)

    def flush(self) -> int:
        with self._lock:
            buf, self._buf = self._buf, []
            if not buf:
                return 0
            self._db.executemany("INSERT OR IGNORE INTO entries VALUES (?,?,?,?)", buf)
            self._db.commit()
            return len(buf)

    def between(self, t0: float, t1: float, flagged_only: bool = False) -> list[Row]:
        self.flush()
        q = "SELECT t, level, source, message FROM entries WHERE t >= ? AND t <= ?"
        if flagged_only:
            q += " AND level IN ('warn','error')"
        with self._lock:
            return self._db.execute(q + " ORDER BY t, rowid", (t0, t1)).fetchall()

    def prune(self, now: float) -> int:
        self.flush()
        with self._lock:
            n = self._db.execute("DELETE FROM entries WHERE t < ?", (now - self.keep_s,)).rowcount
            excess = self._db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] - self.max_rows
            if excess > 0:      # row cap: a flood must not outgrow the disk inside the retention window
                n += self._db.execute("DELETE FROM entries WHERE rowid IN "
                                      "(SELECT rowid FROM entries ORDER BY t LIMIT ?)", (excess,)).rowcount
            self._db.commit()
            return n

    def count(self) -> int:
        self.flush()
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def oldest(self) -> float | None:
        self.flush()
        with self._lock:
            return self._db.execute("SELECT MIN(t) FROM entries").fetchone()[0]

    # -- explanations (explain.py): what a kind of entry is and why it is there. Never pruned. ----------------
    _EXPL_COLS = ("sig", "source", "category", "title", "what", "why", "by", "created")

    def explanation(self, sig: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT " + ",".join(self._EXPL_COLS) + " FROM explanations WHERE sig = ?", (sig,)).fetchone()
        return dict(zip(self._EXPL_COLS, row)) if row else None

    def put_explanation(self, sig: str, source: str, category: str, title: str, what: str, why: str,
                        by: str = "claude", created: float | None = None) -> None:
        import time
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO explanations VALUES (?,?,?,?,?,?,?,?)",
                             (sig, source, category, title, what, why, by, created if created is not None else time.time()))
            self._db.commit()

    def explanations(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT " + ",".join(self._EXPL_COLS) + " FROM explanations ORDER BY source, sig").fetchall()
        return [dict(zip(self._EXPL_COLS, r)) for r in rows]

    def close(self) -> None:
        self.flush()
        self._db.close()
