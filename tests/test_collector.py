"""The collector (alm_collector.py) and the client side that syncs from it."""
import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import alm_collector as ac  # noqa: E402
import tail  # noqa: E402
from history import History  # noqa: E402
from rain_engine import RainEngine  # noqa: E402


def caddy_line(ts, uri="/", ip="8.8.8.8", status=200):
    return json.dumps({"ts": ts, "status": status, "request": {"method": "GET", "uri": uri, "client_ip": ip,
                       "headers": {"User-Agent": ["curl/8"]}}})


def test_store_sequences_entries_and_keeps_cursors_with_them(tmp_path):
    s = ac.Store(str(tmp_path / "c.db"))
    s.add(100, "info", "x", "a")
    s.add(101, "warn", "x", "b", quiet=True)
    s.add(100, "info", "x", "a")                       # duplicate: ignored
    s.set_cursor("k", "v1")
    assert s.get_cursor("k") == "v1"                   # visible before the flush
    s.flush()
    assert [(r[0], r[4], r[5]) for r in s.after(0)] == [(1, "a", 0), (2, "b", 1)]
    assert s.after(1)[0][4] == "b" and s.seq_before(101) == 1 and s.seq_before(50) == 0
    s2 = ac.Store(str(tmp_path / "c.db"))
    assert s2.id == s.id and s2.get_cursor("k") == "v1"


def test_store_prunes_by_age_and_row_cap(tmp_path):
    s = ac.Store(str(tmp_path / "c.db"), keep_s=100, max_rows=3)
    for i in range(6):
        s.add(1000 + i, "info", "x", f"m{i}")
    s.add(1, "info", "x", "ancient")
    s.prune(1100)
    assert [r[4] for r in s.after(0)] == ["m3", "m4", "m5"]


def test_sink_raises_a_burst_alert_as_an_entry(tmp_path):
    s = ac.Store(":memory:")
    sink = ac.Sink(s)
    for i in range(10):
        sink.ingest("info", "ufw", f"block TCP 45.33.32.156 -> :{20 + i}", time.time())
    s.flush()
    alerts = [r for r in s.after(0) if r[3] == "alm"]
    assert len(alerts) == 1 and alerts[0][2] == "error" and "45.33.32.156" in alerts[0][4]


def run_stream(db, *extra):
    out = subprocess.run([sys.executable, os.path.join(ROOT, "alm_collector.py"), "stream", "--db", str(db),
                          "--once", *extra], capture_output=True, text=True, timeout=30)
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def test_stream_resumes_after_a_cursor_and_restarts_when_the_store_changed(tmp_path):
    db = tmp_path / "c.db"
    s = ac.Store(str(db))
    for i in range(4):
        s.add(1000 + i, "info", "x", f"m{i}", quiet=(i == 1))
    s.flush()
    rows = run_stream(db)
    assert [r["m"] for r in rows] == ["m0", "m1", "m2", "m3"] and rows[1]["q"] == 1
    assert [r["m"] for r in run_stream(db, "--after", rows[1]["c"])] == ["m2", "m3"]
    assert [r["m"] for r in run_stream(db, "--after", "other:99", "--since", "1002")] == ["m2", "m3"]


def test_parse_collector_lines():
    ok = tail.parse_collector('{"c":"a:1","t":5.0,"l":"warn","o":"sshd","m":"hi","q":0}')
    assert ok == ("warn", "sshd", "hi", 5.0) and not isinstance(ok, tail.HistoryOnly)
    assert isinstance(tail.parse_collector('{"c":"a:2","t":5.0,"l":"info","o":"caddy","m":"x","q":1}'), tail.HistoryOnly)
    assert tail.parse_collector('{"hb": 1.0}') is None and tail.parse_collector("garbage") is None
    assert tail.collector_cursor('{"c":"a:7","t":1}', None) == "a:7"


async def test_end_to_end_collector_follows_a_file_and_the_client_syncs_it(tmp_path):
    log = tmp_path / "access.log"
    now = time.time()
    log.write_text(caddy_line(now - 7200, "/old") + "\n" + caddy_line(now - 3600, "/wp-login.php", status=404) + "\n")
    db = tmp_path / "c.db"
    env = {**os.environ, "ALM_COLLECTOR_DB": str(db)}
    daemon = await asyncio.create_subprocess_exec(
        sys.executable, os.path.join(ROOT, "alm_collector.py"), "run", "--no-journal", "--file", f"{log}:caddy",
        env=env, stderr=asyncio.subprocess.DEVNULL)
    engine = RainEngine(mock=False, history=History(":memory:"))
    cmd = f"{sys.executable} {os.path.join(ROOT, 'alm_collector.py')}"
    old_env = os.environ.get("ALM_COLLECTOR_DB")
    os.environ["ALM_COLLECTOR_DB"] = str(db)
    task = None
    try:
        async def wait(pred, n=300):
            for _ in range(n):
                if pred():
                    return True
                await asyncio.sleep(0.1)
            return False
        assert await wait(lambda: db.exists() and len(run_stream(db)) >= 2)
        task = asyncio.create_task(tail.tail_collector(engine, None, 24, cmd))
        assert await wait(lambda: engine.history.count() >= 2)
        # the backlog is history, not rain
        assert not [s for s in engine.streams if "wp-login" in s.message]
        # a new line written while both run arrives live
        with open(log, "a") as f:
            f.write(caddy_line(time.time(), "/fresh") + "\n")
        assert await wait(lambda: any("/fresh" in s.message for s in engine.streams))
        assert engine.history.get_cursor("collector:local")
    finally:
        for t in (task,):
            if t:
                t.cancel()
                await asyncio.gather(t, return_exceptions=True)
        daemon.terminate()
        await daemon.wait()
        left = subprocess.run(["pgrep", "-f", f"tail -F -n .* {log}"], capture_output=True, text=True).stdout.strip()
        assert not left, "the collector left its tail child behind"
        await asyncio.sleep(0.3)
        if old_env is None:
            os.environ.pop("ALM_COLLECTOR_DB", None)
        else:
            os.environ["ALM_COLLECTOR_DB"] = old_env


def test_collector_watch_warns_once_when_the_daemon_stops_and_again_when_it_returns():
    seen = []

    class E:
        def ingest(self, level, source, msg, ts=None):
            seen.append((level, msg))
    w = tail.CollectorWatch(E(), "myserver")
    hb = lambda age: json.dumps({"hb": 1.0, "c": "a:1", "age": age})
    for age in (2.0, 90.0, 120.0, 3.0):
        assert w(hb(age)) is None
    assert seen == [("warn", "collector on myserver is not collecting"),
                    ("info", "collector on myserver is collecting again")]
    assert w('{"c":"a:2","t":1.0,"l":"info","o":"x","m":"hi","q":0}')[2] == "hi"


def test_store_liveness_age(tmp_path):
    s = ac.Store(str(tmp_path / "c.db"))
    assert s.alive_age() is None
    s.touch()
    assert 0 <= s.alive_age() < 2
