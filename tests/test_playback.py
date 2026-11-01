import pytest

from history import History
from playback import BUDGET_PER_S, WINDOWS, Playback, pick_even, playback_duration, signature
from rain_engine import RainEngine


# ---- pacing table -----------------------------------------------------------------------------
def test_durations_match_the_design_table():
    assert [playback_duration(w) for w in WINDOWS.values()] == [30, 60, 90, 150, 180, 240]


def test_duration_interpolates_and_is_bounded():
    assert playback_duration(2700) == pytest.approx(75)                    # between 30m and 1h
    assert playback_duration(60) == 15                                     # floor
    assert playback_duration(10 * 86400) == 240                            # ceiling


def test_pick_even_spreads_and_keeps_order():
    assert pick_even(list(range(10)), 5) == [0, 2, 4, 6, 8]
    assert pick_even([1, 2], 5) == [1, 2] and pick_even([1, 2], 0) == []


# ---- the virtual clock and entry timing -----------------------------------------------------
def rows_at(*specs):
    return [(t, lvl, "s", f"{lvl}@{t}") for t, lvl in specs]


def test_entry_enters_when_the_clock_reaches_its_timestamp():
    # 5m window, 30s playback => 10x. Entry 150 virtual seconds in must appear at real second 15.
    pb = Playback(rows_at((1150.0, "error")), v_start=1000, v_end=1300, real_start=0.0)
    assert pb.rate == pytest.approx(10)
    assert pb.step(14.9) == [] and pb.virtual_now(14.9) == pytest.approx(1149)
    got = pb.step(15.1)
    assert [r[3] for r in got] == ["error@1150.0"]


def test_clock_minute_matches_entry_minute():
    base = 1_800_000_000 - (1_800_000_000 % 3600)                          # an exact hour boundary
    t_entry = base + 7 * 60 + 30                                           # hh:07:30
    pb = Playback(rows_at((t_entry, "error")), v_start=base, v_end=base + 3600, real_start=0.0)   # 1h => 90s
    for real in [x * 0.25 for x in range(1, 361)]:
        got = pb.step(real)
        if got:
            assert int((pb.virtual_now(real) - base) // 60) == 7           # clock reads hh:07 when it enters
            return
    pytest.fail("entry never came due")


def test_pause_freezes_virtual_time_and_resume_continues_exactly():
    pb = Playback([], v_start=0, v_end=300, real_start=0.0)                 # 10x
    pb.step(5.0)
    pb.pause(5.0)
    assert pb.virtual_now(99.0) == pytest.approx(50)
    pb.resume(99.0)
    assert pb.virtual_now(99.0) == pytest.approx(50)
    assert pb.virtual_now(100.0) == pytest.approx(60)


def test_done_only_after_the_window_is_swept():
    pb = Playback(rows_at((10.0, "info")), v_start=0, v_end=300, real_start=0.0)
    pb.step(5.0)
    assert not pb.done
    pb.step(31.0)
    assert pb.done


# ---- budget and priority ------------------------------------------------------------------------
def test_errors_never_dropped_even_in_a_flood():
    rows = rows_at(*[(i * 0.01, "error") for i in range(200)])
    pb = Playback(rows, v_start=0, v_end=10, real_start=0.0, duration_s=1.0)
    got = pb.step(1.0)
    assert len(got) == 200 and pb.skipped == 0


def test_warns_outrank_info_and_budget_is_respected():
    rows = (rows_at(*[(i * 0.01, "info") for i in range(100)])
            + rows_at(*[(2 + i * 0.01, "warn") for i in range(20)]))
    rows.sort()
    pb = Playback(rows, v_start=0, v_end=10, real_start=0.0, duration_s=1.0)
    got = pb.step(1.0)                                                      # everything due in one step
    kinds = [r[1] for r in got]
    assert len(got) <= BUDGET_PER_S * 2 + 1                                # one bucket's worth
    assert kinds.count("warn") >= kinds.count("info")                      # priority
    assert pb.skipped == 120 - len(got)


def test_low_priority_is_sampled_evenly_across_the_window_not_the_first_n():
    rows = rows_at(*[(i * 0.1, "info") for i in range(1000)])               # 100 virtual s, one entry per 0.1s
    pb = Playback(rows, v_start=0, v_end=100, real_start=0.0, duration_s=10.0)   # 10 buckets, cap 2 each
    got = pb.step(10.0)
    assert len(got) == 20
    per_bucket = [sum(1 for r in got if b * 10 <= r[0] < (b + 1) * 10) for b in range(10)]
    assert per_bucket == [2] * 10


# ---- engine integration ------------------------------------------------------------------------------
@pytest.fixture
def eng(clock):
    h = History(":memory:")
    e = RainEngine(mock=False, history=h)
    e._q = e.subscribe()
    return e


def events(e):
    out = []
    while not e._q.empty():
        out.append(e._q.get_nowait())
    return out


def seed(e, clock, minutes_ago_levels):
    """minutes_ago_levels: [(minutes_ago, level, msg)] into history at fixed wall times."""
    for m, lvl, msg in minutes_ago_levels:
        e.history.add(clock.wall() - m * 60, lvl, "src", msg)


def test_start_emits_event_and_snapshot_carries_playback(eng, clock):
    assert eng.start_playback(WINDOWS["5m"])
    ev = [x for x in events(eng) if x["type"] == "playback"][0]
    assert ev["state"] == "start" and ev["window_s"] == 300 and ev["rate"] == pytest.approx(10)
    assert ev["v_end"] - ev["v_start"] == 300
    snap = eng.snapshot()["playback"]
    assert snap["window_s"] == 300 and snap["paused"] is False


def test_no_history_means_no_timeline(clock):
    assert RainEngine(mock=False).start_playback(300) is False


def test_playback_spawns_replay_streams_in_order_with_original_timestamps(eng, clock):
    seed(eng, clock, [(4, "error", "first"), (2, "error", "second"), (1, "error", "third")])
    eng.start_playback(300)
    events(eng)
    for _ in range(200):
        clock.t += 0.25
        eng._playback_tick()
        if eng.playback is None:
            break
    spawns = [x for x in events(eng) if x["type"] == "spawn"]
    assert [s["message"] for s in spawns] == ["first", "second", "third"]
    assert all(s["replay"] for s in spawns)


def test_playback_ends_with_an_end_event_and_returns_to_live(eng, clock):
    seed(eng, clock, [(2, "error", "x")])
    eng.start_playback(300)
    clock.t += 31
    eng._playback_tick()
    assert eng.playback is None
    assert [x["state"] for x in events(eng) if x["type"] == "playback"][-1] == "end"
    assert eng.ingest("error", "s", "live again") is not None


def test_live_info_hidden_live_errors_banked_then_replayed_on_stop(eng, clock):
    eng.start_playback(300)
    events(eng)
    assert eng.ingest("info", "s", "hidden live info") is None
    assert eng.ingest("error", "s", "banked live error") is None
    fc = [x for x in events(eng) if x["type"] == "flagged_count"]
    assert fc and fc[-1]["count"] == 1 and fc[-1]["reason"] == "playback"
    eng.stop_playback(replay_live=False)
    assert eng.playback is None and len(eng.flagged) == 1


async def test_banked_live_entries_replay_after_stop(eng, clock):
    eng.start_playback(300)
    eng.ingest("error", "s", "banked live error")
    eng.stop_playback()
    import asyncio
    await asyncio.sleep(0.1)
    assert [s.message for s in eng.streams] == ["banked live error"] and eng.streams[0].replay
    assert eng.flagged == []


def test_history_still_stores_live_entries_during_playback(eng, clock):
    eng.start_playback(300)
    eng.ingest("info", "s", "stored though hidden")
    assert any(r[3] == "stored though hidden" for r in eng.history.between(0, 1e12))


def test_filter_is_respected_during_playback(eng, clock):
    seed(eng, clock, [(3, "info", "i"), (2, "error", "e")])
    eng.set_filter("error")
    eng.start_playback(300)
    for _ in range(200):
        clock.t += 0.25
        eng._playback_tick()
        if eng.playback is None:
            break
    assert {s["message"] for s in events(eng) if s["type"] == "spawn"} == {"e"}


def test_max_active_is_respected_and_counted_as_skipped(eng, clock):
    import rain_engine
    seed(eng, clock, [(2, "error", f"e{i}") for i in range(rain_engine.MAX_ACTIVE + 10)])
    eng.start_playback(300)
    clock.t += 31
    eng._playback_tick()
    assert len(eng.streams) <= rain_engine.MAX_ACTIVE


def test_pause_stops_spawning_and_resume_continues(eng, clock):
    seed(eng, clock, [(4, "error", "a"), (1, "error", "b")])
    eng.start_playback(300)
    clock.t += 1; eng._playback_tick()
    eng.pause_playback()
    n = len(eng.streams)
    clock.t += 100; eng._playback_tick()
    assert len(eng.streams) == n
    eng.resume_playback()
    clock.t += 31; eng._playback_tick()
    assert {s.message for s in eng.streams} == {"a", "b"}


def test_away_return_is_warn_error_playback_since_leaving(eng, clock):
    eng.toggle_presence()                                  # leave
    clock.t += 600
    eng.ingest("info", "s", "boring while away")
    eng.ingest("error", "s", "important while away")
    events(eng)
    eng.toggle_presence()                                  # return
    pb = eng.playback
    assert pb is not None and pb.flagged_only
    assert pb.window_s == pytest.approx(600, abs=1)
    assert [r[3] for r in pb._plan] == ["important while away"]
    assert eng.flagged == []


# ---- helpers shared with the TUI -----------------------------------------------------------------
def test_next_window_cycles_through_all_then_live():
    from playback import next_window
    seq, cur = [], None
    while True:
        cur = next_window(cur)
        if cur is None:
            break
        seq.append(cur)
    assert seq == [300, 1800, 3600, 21600, 43200, 86400]
    assert next_window(2700) is None                       # an arbitrary (away) window just returns to live


def test_clock_label_shows_window_skipped_pause_and_day_tick():
    import time as t
    from playback import clock_label
    now_epoch = t.time()
    pb = Playback([], v_start=now_epoch - 300, v_end=now_epoch, real_start=0.0)
    pb.shown = 12
    for _ in range(12):
        pb.note_dropped()
    s = clock_label(pb, 0.0, t.strftime("%Y%j"))
    assert s.startswith("◷ ") and "-5m" in s and "+12" in s and "paused" not in s
    pb.pause(0.0)
    assert clock_label(pb, 0.0, t.strftime("%Y%j")).endswith("paused")
    old = Playback([], v_start=now_epoch - 3 * 86400, v_end=now_epoch - 3 * 86400 + 300, real_start=0.0)
    assert t.strftime("%a", t.localtime(old.v_start)) in clock_label(old, 0.0, t.strftime("%Y%j"))     # weekday tick
    assert "since you left" in clock_label(Playback([], 0, 300, 0.0, flagged_only=True), 0.0, "x")


# ---- digest fairness (the live-demo finding): one noisy source must not take every slot ----------------
def counts(plan, idx):
    out = {}
    for r in plan:
        out[r[idx]] = out.get(r[idx], 0) + 1
    return out


def realistic_day():
    """Shaped like the measured myserver 24h: a flood of sshd warns, plenty of ufw/caddy info, a few errors."""
    rows = []
    span = 86400.0
    for i in range(2650):
        rows.append((i * span / 2650, "warn", "sshd", f"probe {i}"))
    for i in range(4480):
        rows.append((i * span / 4480 + 3, "info", "ufw", f"block {i}"))
    for i in range(700):
        rows.append((i * span / 700 + 7, "info", "caddy", f"GET {i} -> 200"))
    for i in range(120):
        rows.append((i * span / 120 + 11, "warn", "caddy", f"GET {i} -> 404"))
    for i in range(104):
        rows.append((i * span / 104 + 13, "error", "sshd", f"kex {i}"))
    return sorted(rows)


def test_sshd_flood_does_not_monopolise_a_24h_digest():
    pb = Playback(realistic_day(), v_start=0, v_end=86400, real_start=0.0)
    by_source = counts(pb._plan, 2)
    assert by_source["sshd"] < 0.6 * pb.planned                              # was ~100% before the fix
    assert by_source["ufw"] > 0.10 * pb.planned and by_source["caddy"] > 0.10 * pb.planned
    assert counts(pb._plan, 1)["error"] == 104                              # every error survives
    assert counts(pb._plan, 1).get("warn", 0) > 0 and counts(pb._plan, 1).get("info", 0) > 0


def test_signature_masks_digits_and_keeps_the_template():
    sig = lambda src, msg: signature((0.0, "warn", src, msg))
    assert sig("sshd", "Invalid user alice from 1.2.3.4 port 5") == sig("sshd", "Invalid user bob from 9.9.9.9 port 77")
    assert sig("sshd", "Accepted publickey for ubuntu") != sig("sshd", "Invalid user a from 1.2.3.4")
    assert sig("sshd", "Failed password 22") == sig("sshd", "Failed password 9999")
    assert sig("ufw", "x") != sig("sshd", "x")


def test_a_repeated_warn_template_is_noisy_and_a_rare_one_is_not():
    rows = [(i, "warn", "sshd", f"Invalid user u{i} from 1.1.1.{i % 250} port {i}") for i in range(500)]
    rows.append((250.5, "warn", "sshd", "Accepted publickey for ubuntu from 24.0.0.1"))
    pb = Playback(sorted(rows), v_start=0, v_end=500, real_start=0.0, duration_s=50.0)
    assert signature((0.0, "warn", "sshd", "Invalid user a")) in pb.noisy_signatures
    assert signature((0.0, "warn", "sshd", "Accepted publickey for x")) not in pb.noisy_signatures


def test_the_rare_warn_always_makes_the_digest_over_a_flood_of_noisy_ones():
    rows = [(i * 0.1, "warn", "sshd", f"Invalid user u{i} from 1.1.1.1 port {i}") for i in range(2000)]
    rows.append((100.05, "warn", "sshd", "Accepted publickey for ubuntu from 24.0.0.1"))
    pb = Playback(sorted(rows), v_start=0, v_end=200, real_start=0.0, duration_s=20.0)
    assert any("Accepted publickey" in r[3] for r in pb._plan)


def test_noisy_templates_are_demoted_not_erased():
    """The noise stays represented (about one general slot in four), so scans remain visible."""
    rows = [(i * 0.05, "info", "ufw", f"block TCP 1.2.3.{i % 250} -> :{i}") for i in range(4000)]
    rows += [(i * 0.5 + 0.1, "info", "caddy", f"GET host/page{i} -> 200") for i in range(400)]
    pb = Playback(sorted(rows), v_start=0, v_end=200, real_start=0.0, duration_s=20.0)
    c = counts(pb._plan, 2)
    assert c["ufw"] >= 5 and c["caddy"] >= 5


def test_only_noise_still_fills_the_digest():
    rows = [(i * 0.1, "info", "ufw", f"block TCP 1.2.3.4 -> :{i}") for i in range(1000)]
    pb = Playback(rows, v_start=0, v_end=100, real_start=0.0, duration_s=10.0)
    assert pb.planned == 20                                                  # cap 2 x 10 buckets, nothing left empty


def test_every_warn_source_shows_up_and_none_is_permanently_first():
    rows = []
    for b in range(100):
        t = b * 10 + 1
        rows += [(t, "warn", "aaa", "a"), (t + 1, "warn", "bbb", "b"), (t + 2, "warn", "ccc", "c")]
    pb = Playback(rows, v_start=0, v_end=1000, real_start=0.0, duration_s=100.0, budget=1.0)   # 1 slot per bucket
    c = counts(pb._plan, 2)
    assert sum(c.values()) == 100
    assert all(25 <= n <= 40 for n in c.values()), c                       # rotation shares the slot evenly


def test_info_still_gets_a_slot_when_warns_are_abundant():
    rows = []
    for b in range(50):
        rows += [(b * 10 + i * 0.1, "warn", "sshd", "w") for i in range(1, 20)] + [(b * 10 + 5, "info", "ufw", "i")]
    pb = Playback(sorted(rows), v_start=0, v_end=500, real_start=0.0, duration_s=50.0)        # cap 2 per bucket
    c = counts(pb._plan, 1)
    assert c["info"] == 50 and c["warn"] == 50


def test_unused_info_share_flows_back_to_warns():
    rows = [(b * 10 + i * 0.1, "warn", "sshd", "w") for b in range(10) for i in range(1, 6)]
    pb = Playback(rows, v_start=0, v_end=100, real_start=0.0, duration_s=10.0)
    assert counts(pb._plan, 1) == {"warn": 20}                              # no info exists, so warns use both slots


def test_errors_beyond_capacity_still_all_shown():
    rows = [(b * 10 + i * 0.01, "error", "e", "x") for b in range(5) for i in range(30)]
    pb = Playback(rows, v_start=0, v_end=50, real_start=0.0, duration_s=5.0)
    assert pb.planned == 150


def test_skipped_counts_unplanned_passed_entries_and_engine_drops():
    rows = rows_at(*[(i * 0.1, "info") for i in range(1000)])
    pb = Playback(rows, v_start=0, v_end=100, real_start=0.0, duration_s=10.0)
    pb.step(5.0)                                                            # halfway
    passed = sum(1 for r in rows if r[0] <= 50.0)
    assert pb.skipped == passed - pb.shown
    pb.step(10.0)
    assert pb.skipped == 1000 - 20 and pb.done
    pb.note_dropped()
    assert pb.skipped == 1000 - 20 + 1


def test_desk_events_appear_in_playback_but_were_never_live(eng, clock):
    eng.desk("input", "input after 3h quiet (touch)")                 # recorded now, no live drop
    assert not eng.streams
    clock.t += 30
    eng.start_playback(300)
    for _ in range(200):
        clock.t += 0.25
        eng._playback_tick()
        if eng.playback is None:
            break
    spawns = [x for x in events(eng) if x["type"] == "spawn"]
    assert [(x["source"], x["level"], x["replay"]) for x in spawns] == [("desk", "warn", True)]
    assert "input after 3h" in spawns[0]["message"]


def test_pausing_the_timeline_holds_the_streams_on_screen_and_resuming_lets_them_go(eng, clock):
    seed(eng, clock, [(2, "error", "x")])
    eng.start_playback(300)
    events(eng)
    s = eng._add_stream("info", "src", "live-ish", "00:00", replay=True)
    clock.t += 3
    eng.pause_playback()
    held_at = s.elapsed(clock.t)
    assert held_at == pytest.approx(3.0)
    clock.t += s.duration + 50                                    # far past its life
    eng._reap(clock.t)
    assert s in eng.streams and s.elapsed(clock.t) == pytest.approx(3.0)       # nothing expires or advances while held
    late = eng._add_stream("info", "src", "spawned while held", "00:00")
    assert late.elapsed(clock.t) == 0.0
    eng.resume_playback()
    clock.t += 2
    assert s.elapsed(clock.t) == pytest.approx(5.0) and late.elapsed(clock.t) == pytest.approx(2.0)


def test_stopping_while_paused_lets_the_held_streams_go(eng, clock):
    seed(eng, clock, [(2, "error", "x")])
    eng.start_playback(300)
    s = eng._add_stream("info", "src", "m", "00:00", replay=True)
    clock.t += 1
    eng.pause_playback()
    clock.t += 10
    eng.stop_playback()
    clock.t += 2
    assert s.elapsed(clock.t) == pytest.approx(3.0)


def test_live_pause_holds_streams_banks_warnings_and_replays_them_on_resume(eng, clock):
    eng.present = True
    s = eng.ingest("info", "src", "on screen")
    clock.t += 2
    assert eng.pause_live() and eng.live_paused
    clock.t += 500
    eng._reap(clock.t)
    assert s in eng.streams and s.elapsed(clock.t) == pytest.approx(2.0)          # held, not expired
    events(eng)
    assert eng.ingest("info", "src", "quiet") is None                              # recorded, not shown
    assert eng.ingest("error", "src", "boom") is None
    ev = [x for x in events(eng) if x["type"] == "flagged_count"]
    assert ev and ev[-1]["count"] == 1 and ev[-1]["reason"] == "paused"
    assert eng.snapshot()["live_paused"] is True and eng.snapshot()["flagged_reason"] == "paused"
    assert eng.history.count() >= 3                                                # history kept everything
    replayed = []
    eng.replay_flagged = lambda: replayed.append(len(eng.flagged))               # the replay itself needs a running loop
    eng.resume_live()
    assert replayed == []                                                          # 500 s held: a catch-up replay instead
    assert eng.playback is not None and eng.playback.kind == "catchup" and eng.flagged == []
    assert not eng.live_paused and [x["type"] for x in events(eng)].count("live_pause") == 1
    clock.t += 1
    assert s.elapsed(clock.t) == pytest.approx(3.0)


def test_live_pause_refuses_while_away_or_playing_and_a_timeline_clears_it(eng, clock):
    seed(eng, clock, [(2, "error", "x")])
    eng.present = False
    assert eng.pause_live() is False
    eng.present = True
    assert eng.pause_live()
    assert eng.start_playback(300) and not eng.live_paused                       # the timeline takes over
    assert eng.pause_live() is False


def test_resume_after_a_long_pause_replays_what_arrived_as_a_catchup_ending_now(eng, clock):
    eng.present = True
    eng.pause_live()
    paused_at = clock.wall()
    clock.t += 5
    eng.history.add(clock.wall(), "info", "src", "during the pause")
    clock.t += 55
    events(eng)
    eng.resume_live()
    pb = eng.playback
    assert pb is not None and pb.kind == "catchup" and pb.flagged_only is False
    assert pb.v_start == pytest.approx(paused_at) and pb.v_end == pytest.approx(clock.wall())
    start = [x for x in events(eng) if x["type"] == "playback"][0]
    assert start["kind"] == "catchup" and start["state"] == "start"
    eng._playback_tick()
    clock.t += 40
    eng._playback_tick()
    msgs = [x["message"] for x in events(eng) if x["type"] == "spawn"]
    assert "during the pause" in msgs and eng.playback is None                     # ran to the end and returned to live


def test_resume_after_a_short_pause_just_replays_the_warnings(eng, clock):
    eng.present = True
    eng.pause_live()
    clock.t += 2
    eng.ingest("error", "src", "boom")
    replayed = []
    eng.replay_flagged = lambda: replayed.append(1)
    eng.resume_live()
    assert eng.playback is None and replayed == [1]
