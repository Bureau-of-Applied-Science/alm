import asyncio
import json

import pytest

from history import History
from rain_engine import RainEngine
from tail import backfill_journal


def test_roundtrip_ordering_and_window():
    h = History(":memory:")
    for t, lvl in [(30, "info"), (10, "error"), (20, "warn"), (40, "info")]:
        h.add(t, lvl, "s", f"m{t}")
    assert [r[0] for r in h.between(0, 100)] == [10, 20, 30, 40]
    assert [r[0] for r in h.between(15, 35)] == [20, 30]
    assert [r[1] for r in h.between(0, 100, flagged_only=True)] == ["error", "warn"]


def test_duplicates_are_ignored():
    h = History(":memory:")
    h.add(5, "info", "s", "same")
    h.add(5, "info", "s", "same")
    h.add(5, "info", "s", "different")
    assert h.count() == 2


def test_prune_respects_retention():
    h = History(":memory:", keep_s=100)
    h.add(1000, "info", "s", "old"); h.add(1950, "info", "s", "recent")
    assert h.prune(now=2000) == 1
    assert [r[3] for r in h.between(0, 9999)] == ["recent"]


def test_prune_row_cap_drops_oldest_first():
    h = History(":memory:", keep_s=1000, max_rows=3)
    for t in (10, 20, 30, 40, 50):
        h.add(t, "info", "s", f"m{t}")
    assert h.prune(60) == 2
    assert [r[0] for r in h.between(0, 100)] == [30, 40, 50]


def test_persists_across_reopen(tmp_path):
    p = tmp_path / "h.db"
    h = History(p); h.add(1, "error", "s", "kept"); h.close()
    assert [r[3] for r in History(p).between(0, 10)] == ["kept"]


def test_oversize_message_is_capped():
    h = History(":memory:")
    h.add(1, "info", "s", "x" * 100_000)
    assert len(h.between(0, 2)[0][3]) == 4000


def test_engine_stores_everything_even_what_sampling_and_presence_drop(clock):
    h = History(":memory:")
    e = RainEngine(mock=False, history=h)
    for i in range(40):                                  # far beyond the info rate limit
        e.ingest("info", "s", f"i{i}")
    e.toggle_presence()                                  # away: nothing displays
    e.ingest("error", "s", "while away")
    e.set_filter("warn")
    e.ingest("info", "s", "filtered out")
    msgs = {r[3] for r in h.between(0, 1e12)}
    assert {"i0", "i39", "while away", "filtered out"} <= msgs


def test_engine_uses_source_epoch_when_given(clock):
    h = History(":memory:")
    e = RainEngine(mock=False, history=h)
    s = e.ingest("error", "s", "stamped", ts=1_700_000_000.0)
    assert h.between(1_699_999_999, 1_700_000_001)[0][3] == "stamped"
    assert s.ts.count(":") == 2                           # display label derived from the epoch


def test_engine_stamps_arrival_when_source_has_no_epoch(clock):
    h = History(":memory:")
    e = RainEngine(mock=False, history=h)
    e.ingest("error", "s", "no stamp")
    e.ingest("error", "s", "label only", ts="12:00:00")
    t = clock.wall()
    assert {r[3] for r in h.between(t - 1, t + 1)} == {"no stamp", "label only"}


def test_record_stores_without_displaying_or_alerting(clock):
    h = History(":memory:")
    e = RainEngine(mock=False, history=h)
    for i in range(20):
        e.record("info", "ufw", f"block TCP 45.33.32.156 -> :{i}", ts=1000 + i)
    assert h.count() == 20 and not e.streams and not e.flagged


async def test_backfill_loads_journal_into_history_only(monkeypatch, tmp_path):
    import tail
    lines = [json.dumps({"MESSAGE": f"old {i}", "PRIORITY": "3", "SYSLOG_IDENTIFIER": "x",
                         "__REALTIME_TIMESTAMP": str(1_700_000_000_000_000 + i * 1_000_000)}) for i in range(5)]
    script = tmp_path / "fake_journalctl.py"
    script.write_text("import sys\nprint(" + repr("\n".join(lines)) + ")\n")

    real = asyncio.create_subprocess_exec
    async def fake_exec(*argv, **kw):                     # stand in for `journalctl ...`
        return await real("python3", str(script), **kw)
    monkeypatch.setattr(tail.asyncio, "create_subprocess_exec", fake_exec)

    h = History(":memory:")
    e = RainEngine(mock=False, history=h)
    assert await backfill_journal(e, 24) == 5
    assert await backfill_journal(e, 24) == 5             # re-running overlaps: duplicates ignored
    assert h.count() == 5 and not e.streams
    assert h.between(1_700_000_000, 1_700_000_001)[0][3] == "old 0"
    assert await backfill_journal(e, 0) == 0              # disabled
