import time

from rain_engine import RainEngine
from rain_tui import LogRainApp, RainView, head_col


async def test_tui_click_catch_and_escape_release():
    engine = RainEngine(mock=False)
    await engine.start()
    app = LogRainApp(engine)
    async with app.run_test(size=(160, 40)) as pilot:
        s = engine.ingest("error", "kernel", "tui smoke message")
        await pilot.pause(1.0)
        view = app.query_one(RainView)
        lane = view.display_lane[s.id]
        x = int(head_col(s, time.monotonic(), view.size.width)) + 3     # inside the head/source block
        assert 0 <= x < view.size.width, "stream should be on screen by now"
        await pilot.click(RainView, offset=(x, lane))
        await pilot.pause(0.3)
        assert s.caught and view.caught_stream_id == s.id
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert not s.caught and view.caught_stream_id is None
    engine.stop()


async def test_tui_timeline_keys_cycle_pause_and_return_to_live():
    from history import History
    h = History(":memory:")
    engine = RainEngine(mock=False, history=h)
    await engine.start()
    app = LogRainApp(engine)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("t")
        await pilot.pause(0.3)
        assert engine.playback and engine.playback.window_s == 300
        status = str(app.query_one("#status").render())
        assert "◷" in status and "-5m" in status

        await pilot.press("t")
        await pilot.pause(0.2)
        assert engine.playback.window_s == 1800            # next window

        await pilot.press("p")
        await pilot.pause(0.2)
        assert engine.playback.paused and "paused" in str(app.query_one("#status").render())
        await pilot.press("p")
        await pilot.pause(0.2)
        assert not engine.playback.paused

        await pilot.press("x")
        await pilot.pause(0.2)
        assert engine.playback is None
        assert "streaming" in str(app.query_one("#status").render())
    engine.stop()


async def test_tui_timeline_without_history_does_not_crash():
    engine = RainEngine(mock=False)
    await engine.start()
    app = LogRainApp(engine)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("t")
        await pilot.pause(0.2)
        assert engine.playback is None
    engine.stop()
