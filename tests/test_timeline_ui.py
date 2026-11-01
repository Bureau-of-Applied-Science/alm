"""Timeline UI against a real server with a seeded history database (DESIGN 5.6)."""
import time
from datetime import datetime, timedelta

import pytest

from history import History
from test_browser import Server, browser, page_for  # noqa: F401


def key(pg, sel):
    """Pause and Live are also behind the toggle."""
    if not pg.locator("#tlControls.open").count():
        pg.click("#tlLabel")
    pg.click(sel)


def pick(pg, win):
    """The window keys live behind the faint timeline toggle."""
    if not pg.locator("#tlControls.open").count():
        pg.click("#tlLabel")
    pg.click(f'[data-win="{win}"]')


@pytest.fixture
def server(tmp_path):
    db = tmp_path / "h.db"
    h = History(db)
    now = time.time()
    for secs_ago, lvl, msg in [(290, "error", "seeded error A"), (285, "warn", "seeded warn B"),
                               (200, "error", "seeded error C"), (60, "error", "seeded error D")]:
        h.add(now - secs_ago, lvl, "seed", msg)
    h.close()
    s = Server(tmp_path, db=db).start()
    yield s
    s.stop()


def clock_text(pg):
    return pg.inner_text("#tlTime")


def test_playback_clock_streams_and_return_to_live(server, browser):
    pg = page_for(browser, server.url)
    time.sleep(0.6)
    assert not pg.locator("#tlClock").is_visible()
    pick(pg, "5m")
    pg.wait_for_selector("#tlClock:not(.hidden)")
    expected = (datetime.now() - timedelta(minutes=5)).strftime("%H:%M")
    first = clock_text(pg)
    assert abs((datetime.strptime(first, "%H:%M") - datetime.strptime(expected, "%H:%M")).total_seconds()) <= 120, (first, expected)
    assert "-5m" in pg.inner_text("#tlMeta").lower()
    assert pg.locator('[data-win="5m"].tl-on').count() == 1

    pg.wait_for_function("[...document.querySelectorAll('.stream')].some(s => s.textContent.includes('seeded error A'))", timeout=20000)
    s = pg.locator(".stream.replay").first
    assert s.count() == 1 and "REPLAY" in s.evaluate("e => getComputedStyle(e, '::before').content").upper()
    assert pg.evaluate("document.querySelector('.stream.replay .head').textContent").strip() == "▍ERROR"

    # the lit strip steps forward with the virtual clock
    r1 = pg.evaluate("document.getElementById('tlRing').getAttribute('stroke-dashoffset')")
    time.sleep(2)
    r2 = pg.evaluate("document.getElementById('tlRing').getAttribute('stroke-dashoffset')")
    assert float(r2) < float(r1)

    key(pg, "#tlStop")
    pg.wait_for_selector("#tlClock.hidden", state="attached", timeout=60000)      # after the replays have left
    assert pg.locator("#tlPause").is_hidden()


def test_pause_freezes_the_clock_and_resume_continues(server, browser):
    pg = page_for(browser, server.url)
    time.sleep(0.6)
    pick(pg, "5m")
    pg.wait_for_selector("#tlClock:not(.hidden)")
    time.sleep(1.2)
    key(pg, "#tlPause")
    pg.wait_for_function("document.getElementById('tlMeta').textContent.toLowerCase().includes('paused')")
    ring = lambda: pg.evaluate("document.getElementById('tlRing').getAttribute('stroke-dashoffset')")
    a = ring(); time.sleep(1.5); b = ring()
    assert a == b
    key(pg, "#tlPause")
    pg.wait_for_function("!document.getElementById('tlMeta').textContent.toLowerCase().includes('paused')")
    time.sleep(1.2)
    assert float(ring()) < float(b)


def test_live_entries_banked_during_playback_then_replayed_on_return(server, browser):
    pg = page_for(browser, server.url)
    time.sleep(0.6)
    pick(pg, "5m")
    pg.wait_for_selector("#tlClock:not(.hidden)")
    server.emit("info", "live", "hidden live info")
    server.emit("error", "live", "banked live error")
    pg.wait_for_function("document.getElementById('flaggedBadge').textContent.includes('live since')", timeout=10000)
    assert "1 live since" in pg.inner_text("#flaggedBadge")
    assert not pg.evaluate("[...document.querySelectorAll('.stream')].some(s => s.textContent.includes('hidden live info'))")
    pg.click("#flaggedBadge")                                    # back to live; the banked error replays
    pg.wait_for_function("[...document.querySelectorAll('.stream.replay')].some(s => s.textContent.includes('banked live error'))",
                         timeout=10000)
    assert pg.locator("#tlClock").is_visible()                   # still up: replays are on screen


def test_client_connecting_mid_playback_shows_the_clock(server, browser):
    a = page_for(browser, server.url)
    time.sleep(0.6)
    pick(a, "5m")
    a.wait_for_selector("#tlClock:not(.hidden)")
    time.sleep(1.5)
    b = page_for(browser, server.url)
    b.wait_for_selector("#tlClock:not(.hidden)")
    assert "-5m" in b.inner_text("#tlMeta").lower()
    key(a, "#tlStop")
    b.wait_for_selector("#tlClock.hidden", state="attached", timeout=60000)   # stop propagates; the clock leaves with the replays


def test_timeline_buttons_do_not_disturb_the_level_filter(server, browser):
    pg = page_for(browser, server.url)
    time.sleep(0.6)
    pick(pg, "30m")
    time.sleep(0.5)
    assert pg.locator('.chip[data-level="all"].active').count() == 1
    server.emit("info", "t", "still visible after clicking the timeline")


def test_screensaver_toggle_is_faint_lower_right_opens_on_tap_and_starts_playback(server, browser):
    pg = page_for(browser, server.url + "?screensaver=1")
    time.sleep(0.8)
    opacity = lambda: float(pg.evaluate("getComputedStyle(document.getElementById('tlControls')).opacity"))
    assert opacity() < 0.3
    assert not pg.locator('[data-win="5m"]').is_visible()
    box = pg.locator("#tlLabel").bounding_box()
    vw, vh = pg.viewport_size["width"], pg.viewport_size["height"]
    assert box["x"] > vw / 2 and box["y"] > vh / 2              # lower right, where the right thumb rests
    pg.click("#tlLabel")
    pg.wait_for_function("document.getElementById('tlControls').classList.contains('open')")
    time.sleep(0.4)
    assert opacity() == 1.0 and pg.locator('[data-win="5m"]').is_visible()
    pg.click('[data-win="5m"]')
    pg.wait_for_selector("#tlClock:not(.hidden)")
    pg.wait_for_function("!document.getElementById('tlControls').classList.contains('open')")   # choosing a window folds it away
    assert not pg.locator("header.chrome").is_visible()
    # clear of the 24px exit-swipe zones so it never steals that gesture
    pg.click("#tlLabel")
    for el in ("#tlLabel", '[data-win="24h"]'):
        b = pg.locator(el).bounding_box()
        assert b["y"] + b["height"] < vh - 24 and b["x"] + b["width"] < vw - 24 and b["y"] > 24


def test_timeline_keys_fold_away_on_their_own(server, browser):
    pg = page_for(browser, server.url)
    time.sleep(0.6)
    pg.click("#tlLabel")
    assert pg.locator('[data-win="5m"]').is_visible()
    pg.wait_for_function("!document.getElementById('tlControls').classList.contains('open')", timeout=9000)


def test_snapshot_without_history_has_no_timeline(tmp_path, browser):
    s = Server(tmp_path).start()                                  # --no-history
    try:
        pg = page_for(browser, s.url)
        time.sleep(0.6)
        pick(pg, "5m")                               # engine refuses: no store
        time.sleep(0.6)
        assert pg.locator("#tlClock").is_hidden()
    finally:
        s.stop()


# ---- desk events end to end ---------------------------------------------------------------------------------
def desk_rows(server):
    time.sleep(0.6)                                                  # history writes are flushed every 250ms
    h = History(server.db)
    rows = [r for r in h.between(0, 1e12) if r[2] == "desk"]
    h.close()
    return rows


def test_screensaver_reports_open_input_and_dismissal_but_none_show_live(server, browser):
    live = page_for(browser, server.url)                              # an ordinary live view watching throughout
    time.sleep(0.6)
    ss = page_for(browser, server.url + "?screensaver=1&deskquiet=500")
    time.sleep(2.6)                                                   # past the 2s synthetic-input grace period
    ss.mouse.move(300, 300); ss.mouse.move(420, 340)                  # someone touches it
    time.sleep(0.8)
    ss.mouse.move(1399, 400); ss.mouse.down(); ss.mouse.move(1260, 405, steps=8); ss.mouse.up()   # then swipes it away
    msgs = [r[3] for r in desk_rows(server)]
    assert any(m == "screensaver opened" for m in msgs), msgs
    assert any(m.startswith("input after") and "(mouse)" in m for m in msgs), msgs
    assert any(m.startswith("dismissed by edge swipe from right") for m in msgs), msgs
    time.sleep(0.5)
    assert live.evaluate("[...document.querySelectorAll('.stream')].some(s => s.textContent.includes('desk'))") is False
    assert "desk" not in live.inner_text("#lanes")


def test_input_right_after_opening_is_not_reported(server, browser):
    ss = page_for(browser, server.url + "?screensaver=1&deskquiet=100")
    time.sleep(0.5)
    ss.mouse.move(300, 300); ss.mouse.move(500, 380)                  # inside the 2s grace: a new window's own events
    msgs = [r[3] for r in desk_rows(server)]
    assert msgs == ["screensaver opened"], msgs


def test_a_normal_page_never_reports_desk_events(server, browser):
    pg = page_for(browser, server.url)
    time.sleep(2.6)
    pg.mouse.move(300, 300); pg.mouse.move(500, 380)
    assert desk_rows(server) == []


def test_desk_events_show_up_in_the_timeline_playback(server, browser):
    ss = page_for(browser, server.url + "?screensaver=1")
    time.sleep(0.8)
    ss.context.close()
    time.sleep(0.6)
    pg = page_for(browser, server.url)
    time.sleep(0.6)
    pick(pg, "5m")
    pg.wait_for_function("[...document.querySelectorAll('.stream.replay')].some(s => s.textContent.includes('screensaver opened'))",
                         timeout=40000)
    assert pg.locator(".stream.replay .src", has_text="desk").count() >= 1


def test_clock_stays_for_as_long_as_replays_are_on_screen(server, browser):
    pg = page_for(browser, server.url)
    time.sleep(0.6)
    pick(pg, "5m")
    pg.wait_for_function("document.querySelectorAll('.stream.replay').length > 0", timeout=20000)
    key(pg, "#tlStop")                                            # playback is over, the replays are not
    time.sleep(2.5)
    assert pg.evaluate("document.querySelectorAll('.stream.replay').length") > 0
    assert pg.locator("#tlClock").is_visible() and "ENDED" in pg.inner_text("#tlMeta")
    assert pg.evaluate("document.getElementById('lampEnd').classList.contains('on')")
    pg.wait_for_function("document.querySelectorAll('.stream.replay').length === 0", timeout=60000)
    pg.wait_for_selector("#tlClock.hidden", state="attached", timeout=10000)


def test_pause_eases_the_streams_to_a_halt_and_resume_eases_them_back(server, browser):
    pg = page_for(browser, server.url)
    time.sleep(0.6)
    pick(pg, "5m")
    pg.wait_for_function("document.querySelectorAll('.stream.replay').length > 0", timeout=20000)
    left = lambda: pg.evaluate("(() => { const e = document.querySelector('.stream.replay'); return e ? e.getBoundingClientRect().left : null })()")
    x0 = left(); time.sleep(0.4); x1 = left()
    assert x1 < x0 - 3                                            # moving
    key(pg, "#tlPause")
    xs = []
    for _ in range(6):
        xs.append(left()); time.sleep(0.25)
    steps = [a - b for a, b in zip(xs, xs[1:])]
    assert steps[0] > steps[2] >= steps[-1] - 0.5, steps          # slowing, not stopping dead
    time.sleep(1.0)
    a = left(); time.sleep(0.8); b = left()
    assert abs(a - b) < 0.5, (a, b)                               # halted
    key(pg, "#tlPause")                                           # resume
    time.sleep(1.2)
    assert left() < b - 5


def test_live_pause_holds_the_screen_and_resume_brings_back_what_warned(server, browser):
    pg = page_for(browser, server.url)
    time.sleep(0.6)
    server.emit("info", "live", "drifting along")
    pg.wait_for_function("[...document.querySelectorAll('.stream')].some(s => s.textContent.includes('drifting along'))", timeout=10000)
    left = lambda: pg.evaluate("(() => { const e = [...document.querySelectorAll('.stream')].find(s => s.textContent.includes('drifting along')); return e ? e.getBoundingClientRect().left : null })()")
    x0 = left(); time.sleep(0.4); assert left() < x0 - 2
    key(pg, "#tlPause")
    time.sleep(1.5)
    a = left(); time.sleep(0.8); b = left()
    assert abs(a - b) < 0.5, (a, b)                                           # held
    assert "paused" in pg.inner_text("#statusText").lower()
    server.emit("info", "live", "should not show")
    server.emit("error", "live", "warned while paused")
    pg.wait_for_function("document.getElementById('flaggedBadge').textContent.includes('held back')", timeout=10000)
    assert not pg.evaluate("[...document.querySelectorAll('.stream')].some(s => s.textContent.includes('should not show'))")
    key(pg, "#tlPause")                                                       # resume
    pg.wait_for_function("[...document.querySelectorAll('.stream.replay')].some(s => s.textContent.includes('warned while paused'))", timeout=10000)
    time.sleep(0.8)
    assert left() is None or left() < b - 3                                   # moving again
    assert "streaming" in pg.inner_text("#statusText").lower()


def test_resume_after_a_pause_catches_up_with_a_countdown_then_the_clock_glitches_away(server, browser):
    pg = page_for(browser, server.url)
    time.sleep(0.6)
    key(pg, "#tlPause")
    server.emit("info", "live", "arrived during the pause")
    server.emit("error", "live", "also arrived during the pause")
    time.sleep(6)                                                    # long enough for a catch-up replay
    key(pg, "#tlPause")                                              # resume
    pg.wait_for_selector("#tlClock:not(.hidden)", timeout=10000)
    assert "catch-up" in pg.inner_text("#tlClock").lower()
    pg.wait_for_function("document.getElementById('tlMeta').textContent.includes('LIVE IN')", timeout=10000)
    pg.wait_for_function("[...document.querySelectorAll('.stream.replay')].some(s => s.textContent.includes('arrived during the pause'))",
                         timeout=30000)
    pg.wait_for_function("document.getElementById('tlMeta').textContent.includes('LIVE')  && document.getElementById('lampEnd').classList.contains('on')", timeout=40000)
    pg.wait_for_selector("#tlClock.glitch", state="attached", timeout=60000)      # leaves with a glitch, once the replays are gone
    pg.wait_for_selector("#tlClock.hidden", state="attached", timeout=5000)
    assert not pg.evaluate("document.getElementById('tlClock').classList.contains('glitch')")
