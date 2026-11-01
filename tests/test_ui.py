"""Interface-batch tests (DESIGN 4.6, 5.5): readability, screensaver interaction."""
import io
import time

import pytest
from PIL import Image

from test_browser import Server, browser, page_for  # noqa: F401  (browser is a fixture)


@pytest.fixture
def server(tmp_path):
    s = Server(tmp_path).start()
    yield s
    s.stop()


def pause_mid_lane(pg, frac=0.5):
    pg.evaluate("""f => document.querySelectorAll('.stream').forEach(el => el.getAnimations().forEach(a => {
        a.pause(); a.currentTime = a.effect.getTiming().duration * f; }))""", frac)


def test_atmosphere_is_beneath_text(server, browser):
    pg = page_for(browser, server.url)
    app = lambda pseudo, prop: pg.evaluate(f"getComputedStyle(document.getElementById('app'), '{pseudo}').{prop}")
    assert app("::after", "content") in ("none", "normal")                 # no overlay on top any more
    assert app("::before", "mixBlendMode") == "normal"
    assert int(app("::before", "zIndex")) < int(pg.evaluate("getComputedStyle(document.getElementById('lanes')).zIndex"))


def test_short_message_is_not_faded_and_text_is_bright(server, browser):
    pg = page_for(browser, server.url)
    time.sleep(0.5)
    server.emit("info", "t", "block TCP 45.33.32.156 -> :5432")
    pg.wait_for_selector(".stream")
    time.sleep(0.3)
    pause_mid_lane(pg)
    wrap = pg.locator(".stream .msg-wrap").first
    assert wrap.evaluate("e => getComputedStyle(e).maskImage") == "none"      # fits, so no right-edge fade
    img = Image.open(io.BytesIO(pg.locator(".stream .msg").first.screenshot())).convert("RGB")
    brightest = max(max(px) for px in img.getdata())
    assert brightest >= 0.9 * 0xB2, brightest          # #58b264 text reaches near full brightness mid-lane


def test_long_message_still_gets_the_fade(server, browser):
    pg = page_for(browser, server.url)
    time.sleep(0.5)
    server.emit("info", "t", "x" * 300)
    pg.wait_for_selector(".stream")
    assert pg.locator(".stream .msg-wrap.overflow").count() == 1


# ---- B1/B2: interactive screensaver, edge-swipe exit ----------------------------------------
def left(pg):
    try:
        return pg.is_closed() or pg.title() == "ALM-EXIT"
    except Exception:
        return True


def swipe(pg, start, end, steps=8):
    pg.mouse.move(*start)
    pg.mouse.down()
    pg.mouse.move(*end, steps=steps)
    pg.mouse.up()
    time.sleep(0.2)


def saver(browser, server):
    pg = page_for(browser, server.url + "?screensaver=1")
    time.sleep(0.5)
    return pg


W, H = 1400, 800


@pytest.mark.parametrize("name,start,end", [
    ("left", (3, 400), (120, 405)),
    ("right", (W - 3, 400), (W - 120, 405)),
    ("top", (700, 3), (705, 120)),
    ("bottom", (700, H - 3), (705, H - 120)),
])
def test_inward_swipe_from_each_edge_leaves(server, browser, name, start, end):
    pg = saver(browser, server)
    swipe(pg, start, end)
    assert left(pg), name


@pytest.mark.parametrize("name,start,end", [
    ("from centre", (700, 400), (820, 400)),
    ("short of threshold", (3, 400), (40, 400)),
    ("along the edge", (3, 200), (3, 600)),
    ("outward from edge", (W - 3, 400), (W + 0, 400)),
    ("plain click at an edge", (3, 400), (3, 400)),
])
def test_other_gestures_do_not_leave(server, browser, name, start, end):
    pg = saver(browser, server)
    swipe(pg, start, end)
    assert not left(pg), name


def test_screensaver_streams_are_interactive(server, browser):
    pg = saver(browser, server)
    server.emit("error", "t", "catch me while the screensaver is up")
    pg.wait_for_selector(".stream")
    assert pg.evaluate("getComputedStyle(document.querySelector('.stream')).pointerEvents") != "none"
    assert not pg.locator("header.chrome").is_visible()
    pause_mid_lane(pg)                 # a drifting target is never "stable" to Playwright
    pg.locator(".stream").first.click()
    pg.wait_for_selector("#detailPanel:not(.hidden)")
    pg.mouse.move(300, 300); pg.mouse.move(900, 500)           # plain movement does not leave
    assert not left(pg)
    pg.keyboard.press("Escape")                                 # Escape first backs out of the catch
    pg.wait_for_function("document.getElementById('detailPanel').classList.contains('hidden')")
    assert not left(pg)
    pg.keyboard.press("Escape")                                 # then leaves
    time.sleep(0.2)
    assert left(pg)


def test_cursor_hides_when_calm_and_returns_on_movement(server, browser):
    pg = saver(browser, server)
    time.sleep(3.3)
    assert pg.evaluate("getComputedStyle(document.body).cursor") == "none"
    pg.mouse.move(500, 400); pg.mouse.move(520, 410)
    assert pg.evaluate("getComputedStyle(document.body).cursor") != "none"


def test_stream_hit_area_is_full_lane_height(server, browser):
    pg = page_for(browser, server.url)
    time.sleep(0.4)
    server.emit("error", "t", "tall target")
    pg.wait_for_selector(".stream")
    box = pg.evaluate("(() => { const s=document.querySelector('.stream').getBoundingClientRect(),"
                      " l=document.querySelector('.lane').getBoundingClientRect(); return [s.height, l.height]; })()")
    assert box[0] >= box[1] - 1


# ---- real touch input (Chromebook touchscreen), via CDP touch events -----------------------------
def touch_pg(browser_obj, url):
    ctx = browser_obj.new_context(viewport={"width": W, "height": H}, has_touch=True)
    pg = ctx.new_page()
    pg.goto(url)
    time.sleep(0.6)
    return pg, ctx.new_cdp_session(pg)


def touch_drag(cdp, start, end, steps=8):
    def send(kind, pt):
        cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [] if kind == "touchEnd" else
                                              [{"x": pt[0], "y": pt[1], "id": 1}]})
    send("touchStart", start)
    for i in range(1, steps + 1):
        send("touchMove", (start[0] + (end[0] - start[0]) * i / steps, start[1] + (end[1] - start[1]) * i / steps))
        time.sleep(0.02)
    send("touchEnd", end)
    time.sleep(0.25)


@pytest.mark.parametrize("name,start,end", [
    ("tap in the middle", (700, 400), (700, 400)),
    ("tap near an edge", (3, 400), (3, 400)),
    ("drag in the middle", (500, 400), (900, 420)),
    ("drag along the left edge", (3, 200), (3, 600)),
    ("short edge drag", (3, 400), (40, 400)),
])
def test_touch_that_is_not_an_edge_swipe_does_not_leave(server, browser, name, start, end):
    pg, cdp = touch_pg(browser, server.url + "?screensaver=1")
    server.emit("error", "t", "a stream to touch")
    pg.wait_for_selector(".stream")
    touch_drag(cdp, start, end)
    assert not left(pg), name


@pytest.mark.parametrize("name,start,end", [
    ("left", (3, 400), (140, 405)), ("right", (W - 3, 400), (W - 140, 405)),
    ("top", (700, 3), (705, 140)), ("bottom", (700, H - 3), (705, H - 140)),
])
def test_touch_edge_swipe_leaves(server, browser, name, start, end):
    pg, cdp = touch_pg(browser, server.url + "?screensaver=1")
    touch_drag(cdp, start, end)
    assert left(pg), name


def test_tapping_a_stream_with_touch_catches_it_and_does_not_leave(server, browser):
    pg, cdp = touch_pg(browser, server.url + "?screensaver=1")
    server.emit("error", "t", "tap me")
    pg.wait_for_selector(".stream")
    pause_mid_lane(pg)
    box = pg.locator(".stream").first.bounding_box()
    pg.touchscreen.tap(box["x"] + 40, box["y"] + box["height"] / 2)
    pg.wait_for_selector("#detailPanel:not(.hidden)")
    assert not left(pg)


# ---- the reported bug: a drop caught in one screensaver stayed frozen in the next ------------------------
def caught_in_state(server):
    import json
    import urllib.request
    d = json.load(urllib.request.urlopen(server.url + "api/state"))
    return [s for s in d["streams"] if s["caught"]]


def test_closing_the_window_that_caught_a_drop_releases_it(server, browser):
    pg = saver(browser, server)
    server.emit("error", "t", "caught then abandoned")
    pg.wait_for_selector(".stream")
    pause_mid_lane(pg)
    pg.locator(".stream").first.click()
    pg.wait_for_selector("#detailPanel:not(.hidden)")
    assert len(caught_in_state(server)) == 1
    pg.context.close()                                      # the window goes away without releasing
    for _ in range(30):
        if not caught_in_state(server):
            break
        time.sleep(0.1)
    assert caught_in_state(server) == []

    nxt = saver(browser, server)                            # the next screensaver starts clean
    time.sleep(0.8)
    assert nxt.locator(".stream.caught").count() == 0


def test_leaving_the_screensaver_with_a_catch_open_does_not_strand_it(server, browser):
    pg = saver(browser, server)
    server.emit("error", "t", "swipe away while reading")
    pg.wait_for_selector(".stream")
    pause_mid_lane(pg)
    pg.locator(".stream").first.click()
    pg.wait_for_selector("#detailPanel:not(.hidden)")
    swipe(pg, (W - 3, 600), (W - 140, 605))
    assert left(pg)
    try:
        pg.context.close()
    except Exception:
        pass
    for _ in range(30):
        if not caught_in_state(server):
            break
        time.sleep(0.1)
    assert caught_in_state(server) == []
