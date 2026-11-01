#!/usr/bin/env python3
"""Maintain the explanations that the detail view shows (see explain.py).

  explain.py todo [--limit N]     signatures in history with no explanation yet, most frequent first
  explain.py import FILE          load explanations from JSON into the history database
  explain.py export FILE          write every stored explanation to JSON (the copy that lives in git)

The database is the live one by default; pass --db to use another. A Claude pass is: run `todo`, write the
JSON for what it lists, `import` it, `export` it back to knowledge/explanations.json and commit.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from explain import signature  # noqa: E402
from history import History  # noqa: E402

FIELDS = ("sig", "source", "category", "title", "what", "why")


def default_db() -> str:
    return os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "alm", "history.db")


def cmd_todo(h: History, limit: int) -> None:
    known = {e["sig"] for e in h.explanations()}
    counts: Counter = Counter()
    example: dict[str, tuple[str, str, str]] = {}
    for level, source, message in h._db.execute("SELECT level, source, message FROM entries"):
        sig, _ = signature(source, message)
        if sig in known:
            continue
        counts[sig] += 1
        example.setdefault(sig, (level, source, message))
    print(f"{len(counts)} unexplained signatures, {sum(counts.values())} entries")
    for sig, n in counts.most_common(limit):
        level, source, message = example[sig]
        print(json.dumps({"n": n, "sig": sig, "level": level, "example": message[:200]}, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=default_db())
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("todo"); t.add_argument("--limit", type=int, default=200)
    sub.add_parser("import").add_argument("file")
    sub.add_parser("export").add_argument("file")
    a = ap.parse_args()
    h = History(a.db)
    if a.cmd == "todo":
        cmd_todo(h, a.limit)
    elif a.cmd == "import":
        rows = json.load(open(a.file))
        for r in rows:
            missing = [f for f in FIELDS if not r.get(f)]
            if missing:
                sys.exit(f"{r.get('sig', r)}: missing {missing}")
            h.put_explanation(r["sig"], r["source"], r["category"], r["title"], r["what"], r["why"], r.get("by", "claude"))
        print(f"imported {len(rows)}")
    else:
        rows = [{k: e[k] for k in (*FIELDS, "by")} for e in h.explanations()]
        json.dump(rows, open(a.file, "w"), indent=1, ensure_ascii=False); open(a.file, "a").write("\n")
        print(f"exported {len(rows)}")
    h.close()


if __name__ == "__main__":
    main()
