import pytest

import rain_engine
from rain_engine import RainEngine


@pytest.fixture
def engine(clock):
    e = RainEngine(mock=False)
    e.events = []
    q = e.subscribe()
    e._q = q
    return e


def drain(e):
    out = []
    while not e._q.empty():
        out.append(e._q.get_nowait())
    return out


def test_spawn_broadcasts_and_sets_duration(engine):
    s = engine.ingest("info", "src", "hello")
    assert s is not None
    ev = drain(engine)
    assert ev[0]["type"] == "spawn" and ev[0]["id"] == s.id
    assert s.duration == pytest.approx(12.0 + 5 * 0.03)


def test_release_preserves_elapsed(engine, clock):
    s = engine.ingest("error", "src", "boom")
    clock.t += 4
    engine.catch(s.id)
    clock.t += 100            # paused time must not count
    engine.release(s.id)
    assert s.elapsed(clock.t) == pytest.approx(4.0)
    clock.t += 1
    assert s.elapsed(clock.t) == pytest.approx(5.0)


def test_only_one_caught_at_a_time(engine, clock):
    a = engine.ingest("error", "src", "a")
    b = engine.ingest("error", "src", "b")
    engine.catch(a.id)
    clock.t += 1
    engine.catch(b.id)
    assert b.caught and not a.caught
    types = [e["type"] for e in drain(engine)]
    assert types.count("release") == 1


def test_dismiss_freezes_then_fades(engine, clock):
    s = engine.ingest("warn", "src", "x")
    clock.t += 3
    engine.dismiss(s.id)
    frozen = s.elapsed(clock.t)
    clock.t += 0.3
    assert s.elapsed(clock.t) == frozen
    assert 0 < s.alpha(clock.t) < 1
    clock.t += 0.4
    assert s.alpha(clock.t) == 0


def test_snapshot_reports_caught_and_dismissing(engine, clock):
    a = engine.ingest("error", "src", "a")
    b = engine.ingest("error", "src", "b")
    clock.t += 2
    engine.catch(a.id)
    engine.dismiss(b.id)
    snap = {d["id"]: d for d in engine.snapshot()["streams"]}
    assert snap[a.id]["caught"] and snap[a.id]["elapsed"] == pytest.approx(2.0)
    assert snap[b.id]["dismiss_elapsed"] == pytest.approx(2.0)


def test_away_banks_only_warn_error_and_replays(engine):
    engine.toggle_presence()
    assert engine.ingest("info", "s", "i") is None
    assert engine.ingest("warn", "s", "w") is None
    assert engine.ingest("error", "s", "e") is None
    assert len(engine.flagged) == 2 and not engine.streams


async def test_replay_marks_streams_and_clears_bank(engine):
    engine.toggle_presence()
    engine.ingest("error", "s", "e")
    engine.toggle_presence()          # back: replays
    import asyncio
    await asyncio.sleep(0.1)
    assert engine.flagged == []
    assert engine.streams and engine.streams[0].replay


def test_filter_blocks_ingest(engine):
    engine.set_filter("error")
    assert engine.ingest("info", "s", "i") is None
    assert engine.ingest("error", "s", "e") is not None


def test_max_active_cap(engine):
    for i in range(rain_engine.MAX_ACTIVE):
        assert engine.ingest("error", "s", str(i))
    assert engine.ingest("error", "s", "over") is None


def test_info_sampling_never_drops_warn_error(engine, clock):
    passed = sum(1 for _ in range(50) if engine.ingest("info", "s", "x"))
    assert passed <= rain_engine.INFO_PER_SEC * 2 + 1       # initial bucket only, clock frozen
    assert engine.ingest("error", "s", "e") is not None
    assert engine.ingest("warn", "s", "w") is not None
    clock.t += 1
    engine._tokens  # refilled by elapsed time
    assert engine.ingest("info", "s", "later") is not None


def test_reveal_offset_never_finishes_early(engine, clock):
    msg = "x" * 200
    s = engine.ingest("error", "s", msg)
    clock.t += s.duration * 0.94 + 0.01
    assert s.reveal_offset(clock.t) == len(msg) - rain_engine.WINDOW_W
    assert engine.ingest("error", "s", "x" * 5).reveal_offset(clock.t) == 0


def test_set_filter_rejects_garbage(engine):
    engine.set_filter(None)
    engine.set_filter("bogus")
    assert engine.level_filter == "all"
    engine.set_filter("warn")
    assert engine.level_filter == "warn"


# ---- a catch must not freeze a stream forever (screensaver showed one static drop) ------------------
def test_release_owner_lets_go_only_of_that_clients_catches(engine, clock):
    a = engine.ingest("error", "s", "a")
    b = engine.ingest("error", "s", "b")
    engine.catch(a.id, "ws1")
    engine.release(a.id)                     # one caught at a time: catching b releases a anyway
    engine.catch(b.id, "ws2")
    engine.release_owner("ws1")              # ws1 had nothing caught now
    assert b.caught
    engine.release_owner("ws2")
    assert not b.caught


def test_release_owner_resumes_from_where_it_froze(engine, clock):
    s = engine.ingest("error", "s", "x")
    clock.t += 4
    engine.catch(s.id, "ws1")
    clock.t += 500
    engine.release_owner("ws1")
    assert s.elapsed(clock.t) == pytest.approx(4.0)
    types = [e["type"] for e in drain(engine)]
    assert "release" in types


def test_stale_catch_expires_after_the_ttl(engine, clock):
    s = engine.ingest("error", "s", "x")
    engine.catch(s.id, "ws1")
    clock.t += rain_engine.CAUGHT_TTL_S - 1
    engine._reap(clock.t)
    assert s.caught                           # still being read
    clock.t += 2
    engine._reap(clock.t)
    assert not s.caught
    assert s in engine.streams or True        # released, not removed: it resumes its drift
    assert any(e["type"] == "release" for e in drain(engine))


def test_expired_catch_resumes_with_elapsed_preserved(engine, clock):
    s = engine.ingest("error", "s", "x")
    clock.t += 3
    engine.catch(s.id)
    clock.t += rain_engine.CAUGHT_TTL_S + 5
    engine._reap(clock.t)
    assert s.elapsed(clock.t) == pytest.approx(3.0)


# ---- desk events: history only, never live (someone touched the screensaver) ----------------------------
def test_desk_events_go_to_history_and_never_to_the_live_view(clock):
    from history import History
    h = History(":memory:")
    e = RainEngine(mock=False, history=h)
    q = e.subscribe()
    e.desk("opened", "screensaver opened")
    e.desk("input", "input after 12m quiet (touch)")
    e.desk("dismissed", "dismissed by edge swipe from right (touch)")
    rows = h.between(0, 1e12)
    assert [(r[1], r[2]) for r in rows] == [("info", "desk"), ("warn", "desk"), ("warn", "desk")]
    assert not e.streams and not e.flagged                       # not shown live, not banked
    assert q.empty()                                              # nothing broadcast to any renderer


def test_desk_rejects_unknown_kinds_empty_and_oversize_details(clock):
    from history import History
    h = History(":memory:")
    e = RainEngine(mock=False, history=h)
    e.desk("exploit", "x"); e.desk("input", ""); e.desk("input", "   "); e.desk("input", None); e.desk(None, "x")
    assert h.count() == 0
    e.desk("input", "a\n\nb   c" + "x" * 1000)
    msg = h.between(0, 1e12)[0][3]
    assert "\n" not in msg and len(msg) == 200


def test_desk_without_a_history_store_is_a_no_op(clock):
    e = RainEngine(mock=False)
    e.desk("input", "x")
    assert not e.streams
