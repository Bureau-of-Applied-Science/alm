"""Loads the unpacked extension into real Chromium and drives it through its test hook.
Real chrome.idle transitions and ChromeOS keep-awake behaviour can only be checked on the Chromebook."""
import time

import pytest
from playwright.sync_api import sync_playwright

from test_browser import ROOT, Server

EXT = str(ROOT / "extension")


@pytest.fixture
def ctx(tmp_path):
    with sync_playwright() as p:
        c = p.chromium.launch_persistent_context(
            str(tmp_path / "profile"), channel="chromium", headless=True,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"],
        )
        yield c
        c.close()


def ext_id(ctx):
    """The MV3 service worker occasionally takes long to appear when the machine is busy; wait generously."""
    if ctx.service_workers:
        sw = ctx.service_workers[0]
    else:
        try:
            sw = ctx.wait_for_event("serviceworker", timeout=30000)
        except Exception:
            ctx.new_page().goto("about:blank")            # nudge, then one more chance
            sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=30000)
    return sw.url.split("/")[2], sw


def options_page(ctx):
    eid, _ = ext_id(ctx)
    pg = ctx.new_page()
    pg.goto(f"chrome-extension://{eid}/options.html")
    return pg


def send(pg, msg):
    return pg.evaluate("m => chrome.runtime.sendMessage(m)", msg)


def test_service_worker_registers(ctx):
    eid, sw = ext_id(ctx)
    assert len(eid) == 32


def test_options_persist_and_validate(ctx):
    pg = options_page(ctx)
    pg.fill("#url", "not a url")
    pg.click("#save")
    assert "not a valid URL" in pg.inner_text("#status")
    pg.fill("#url", "http://127.0.0.1:9999")
    pg.fill("#timeoutSec", "3")                      # below minimum -> clamped
    pg.fill("#token", "abc")
    pg.click("#save")
    pg.wait_for_function("document.getElementById('status').textContent === 'Saved.'")
    pg.reload()
    assert pg.input_value("#url") == "http://127.0.0.1:9999"
    assert pg.input_value("#timeoutSec") == "15"
    assert pg.input_value("#token") == "abc"


def saver_pages(ctx):
    return [p for p in ctx.pages if "screensaver=1" in p.url and not p.is_closed()]


def window_count(pg):
    """Chrome's own count of windows. Playwright can keep a stale Page object for a window that is already
    gone in headless mode, so 'is the saver closed' is asked of Chrome, not of ctx.pages."""
    return pg.evaluate("chrome.windows.getAll().then(ws => ws.length)")


def configure(ctx, srv):
    pg = options_page(ctx)
    pg.fill("#url", srv.url)
    pg.fill("#token", "")
    pg.click("#save")
    pg.wait_for_function("document.getElementById('status').textContent === 'Saved.'")
    return pg


def test_idle_opens_saver_and_activity_does_not_close_it(tmp_path, ctx):
    srv = Server(tmp_path).start()
    try:
        pg = configure(ctx, srv)
        assert send(pg, {"type": "test-idle", "state": "idle"})["windowId"] is not None
        assert saver_pages(ctx), [p.url for p in ctx.pages]
        assert "ext=" in saver_pages(ctx)[0].url

        # a second idle must not open a second window
        send(pg, {"type": "test-idle", "state": "idle"})
        assert len(saver_pages(ctx)) == 1

        # the user is interacting with the saver: OS idle goes back to active; it must stay up
        assert send(pg, {"type": "test-idle", "state": "active"})["windowId"] is not None
        time.sleep(0.5)
        assert len(saver_pages(ctx)) == 1

        # the screen locking does close it
        assert send(pg, {"type": "test-idle", "state": "locked"})["windowId"] is None
        time.sleep(0.5)
        assert window_count(pg) == 1, [(e["what"], e["detail"][:40]) for e in events(pg)]      # only our own window left
    finally:
        srv.stop()


def test_edge_swipe_in_the_saver_page_closes_the_window(tmp_path, ctx):
    srv = Server(tmp_path).start()
    try:
        pg = configure(ctx, srv)
        send(pg, {"type": "test-idle", "state": "idle"})
        s = saver_pages(ctx)[0]
        time.sleep(2.0)
        s.evaluate("window.close = () => {}")     # force the extension-message path, not the window.close fallback
        w = s.evaluate("innerWidth")
        try:
            s.mouse.move(w - 3, 300); s.mouse.down(); s.mouse.move(w - 140, 305, steps=8); s.mouse.up()
        except Exception as e:                  # the page closes itself as soon as the swipe crosses the threshold
            assert "closed" in str(e).lower(), e
        for _ in range(50):
            if window_count(pg) == 1:                       # Chrome's own view; ctx.pages can be stale in headless
                break
            time.sleep(0.1)
        assert window_count(pg) == 1
        assert send(pg, {"type": "show"}) is not None       # worker still healthy
        send(pg, {"type": "hide"})
    finally:
        srv.stop()


def test_dismiss_from_a_foreign_window_is_ignored(tmp_path, ctx):
    srv = Server(tmp_path).start()
    try:
        pg = configure(ctx, srv)
        send(pg, {"type": "test-idle", "state": "idle"})
        eid, _ = ext_id(ctx)
        # a page in its OWN window (new_page would open a tab inside the saver window)
        with ctx.expect_page(timeout=10000) as info:
            pg.evaluate("url => chrome.windows.create({ url })", srv.url)
        other = info.value
        other.wait_for_load_state()
        other.evaluate("id => new Promise(r => chrome.runtime.sendMessage(id, {type:'dismiss'}, r))", eid)
        time.sleep(0.5)
        assert len(saver_pages(ctx)) == 1
        send(pg, {"type": "hide"})
    finally:
        srv.stop()


def test_disabled_never_opens(ctx):
    pg = options_page(ctx)
    pg.uncheck("#enabled")
    pg.click("#save")
    pg.wait_for_function("document.getElementById('status').textContent === 'Saved.'")
    assert send(pg, {"type": "test-idle", "state": "idle"})["windowId"] is None


def test_toolbar_toggle_turns_it_off_and_badges_it(tmp_path, ctx):
    srv = Server(tmp_path).start()
    try:
        pg = options_page(ctx)
        _, sw = ext_id(ctx)
        configure(ctx, srv)
        send(pg, {"type": "toggle"})                        # same path as the toolbar button
        pg.wait_for_function("document.getElementById('enabled').checked === false")
        assert sw.evaluate("chrome.action.getBadgeText({})") == "off"
        assert send(pg, {"type": "test-idle", "state": "idle"})["windowId"] is None
        send(pg, {"type": "toggle"})
        pg.wait_for_function("document.getElementById('enabled').checked === true")
        assert sw.evaluate("chrome.action.getBadgeText({})") == ""
        assert send(pg, {"type": "test-idle", "state": "idle"})["windowId"] is not None
        send(pg, {"type": "hide"})
    finally:
        srv.stop()


def test_turning_it_off_closes_a_saver_that_is_up(tmp_path, ctx):
    srv = Server(tmp_path).start()
    try:
        pg = options_page(ctx)
        configure(ctx, srv)
        send(pg, {"type": "test-idle", "state": "idle"})
        pg.evaluate("chrome.storage.sync.set({enabled: false})")
        deadline = time.time() + 8
        while time.time() < deadline and window_count(pg) != 1:    # only the options window remains
            time.sleep(0.2)
        assert window_count(pg) == 1
    finally:
        srv.stop()


def test_options_page_shows_the_extension_version(ctx):
    pg = options_page(ctx)
    assert pg.inner_text("#ver").lower() == "v" + pg.evaluate("chrome.runtime.getManifest().version")
    assert pg.evaluate("chrome.runtime.getManifest().version") != "0.1.0"


# ---- activity log: a surprise dismissal must be explainable from the options page -------------------
def events(pg):
    return pg.evaluate("chrome.storage.local.get('events').then(r => r.events || [])")


def wait_event(pg, what, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        found = [e for e in events(pg) if e["what"].startswith(what)]
        if found:
            return found[-1]
        time.sleep(0.15)
    raise AssertionError(f"no {what!r} event; log: {[e['what'] for e in events(pg)]}")


def test_log_explains_an_edge_swipe_close(tmp_path, ctx):
    srv = Server(tmp_path).start()
    try:
        pg = configure(ctx, srv)
        send(pg, {"type": "test-idle", "state": "idle"})
        assert wait_event(pg, "opened")["detail"] == "test hook"
        s = saver_pages(ctx)[0]
        time.sleep(2.0)
        w = s.evaluate("innerWidth")
        try:
            s.mouse.move(w - 3, 300); s.mouse.down(); s.mouse.move(w - 140, 305, steps=8); s.mouse.up()
        except Exception as e:
            assert "closed" in str(e).lower(), e
        closed = wait_event(pg, "closed by us")
        assert "edge-swipe" in closed["detail"] and '"edge":"right"' in closed["detail"] and "mouse" in closed["detail"]
        assert wait_event(pg, "page: edge-touch")              # the breadcrumb that preceded it
        assert "edge swipe from right" in wait_event(pg, "page: desk dismissed")["detail"]    # readable desk entry too
        assert not [e for e in events(pg) if e["what"].startswith("closed EXTERNALLY")]
    finally:
        srv.stop()


def test_log_says_externally_when_something_else_closes_the_window(tmp_path, ctx):
    srv = Server(tmp_path).start()
    try:
        pg = configure(ctx, srv)
        send(pg, {"type": "test-idle", "state": "idle"})
        s = saver_pages(ctx)[0]
        s.close()                                              # as an OS gesture or the user would
        ev = wait_event(pg, "closed EXTERNALLY")
        assert "without us asking" in ev["detail"]
        assert send(pg, {"type": "show"})["windowId"] is not None     # state recovered: can open again
        send(pg, {"type": "hide"})
    finally:
        srv.stop()


def test_log_records_idle_states_and_lock_close(tmp_path, ctx):
    srv = Server(tmp_path).start()
    try:
        pg = configure(ctx, srv)
        send(pg, {"type": "test-idle", "state": "idle"})
        send(pg, {"type": "test-idle", "state": "locked"})
        assert "screen locked" in wait_event(pg, "closed by us")["detail"] or "locked" in wait_event(pg, "closed by us")["detail"]
    finally:
        srv.stop()


def test_options_page_renders_the_log(ctx):
    pg = options_page(ctx)
    send(pg, {"type": "show"})
    time.sleep(0.5)
    send(pg, {"type": "hide"})
    pg.click("#refresh")
    pg.wait_for_function("document.getElementById('log').textContent.includes('closed by us')")
    pg.click("#clear")
    pg.wait_for_function("document.getElementById('log').textContent.includes('nothing yet')")


def test_logging_and_state_writes_do_not_reopen_a_closed_saver(tmp_path, ctx):
    """Regression: every log write re-ran applySettings, re-arming the idle detector, which could report
    `idle` at once and reopen the saver right after it was closed."""
    srv = Server(tmp_path).start()
    try:
        pg = configure(ctx, srv)
        send(pg, {"type": "test-idle", "state": "idle"})
        send(pg, {"type": "test-idle", "state": "locked"})
        for _ in range(12):                                   # give any spurious reopen time to show itself
            time.sleep(0.25)
            assert window_count(pg) == 1, "the saver came back after being closed; log: " + str([(e["what"], e["detail"][:40]) for e in events(pg)])
        assert [e["what"] for e in events(pg)].count("opened") == 1
    finally:
        srv.stop()
