"""
ALM terminal UI - a thin renderer over RainEngine.

All state (spawning, catching, dismissing, presence) lives in the engine.
This file only turns engine state into character cells and turns clicks
into engine method calls. Anything happening via the web UI concurrently
shows up here automatically, and vice versa.
"""
from __future__ import annotations

import time
from typing import Optional

from rich.color import Color
from rich.markup import escape
from rich.segment import Segment
from rich.style import Style

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Static
from textual import events

from rain_engine import RainEngine, Stream, WINDOW_W

# ---------------------------------------------------------------- palette --

BG = (11, 16, 13)
INK_PRIMARY = (88, 178, 100)
INK_DIM = (107, 125, 108)
INK_FAINT = (75, 90, 74)
ACCENT_INFO = (67, 162, 81)
ACCENT_WARN = (242, 167, 59)
ACCENT_ERROR = (234, 91, 52)
ACCENT_DEBUG = (111, 138, 118)
ACCENT_REPLAY = (232, 184, 74)

LEVEL_COLOR = {"info": ACCENT_INFO, "warn": ACCENT_WARN, "error": ACCENT_ERROR, "debug": ACCENT_DEBUG}
MSG_COLOR = {"info": INK_PRIMARY, "warn": (246, 207, 138), "error": (245, 169, 138), "debug": INK_FAINT}

HEADER_W = 6           # "\u258c" + 5-char level field
SRC_W = 12
TOTAL_W = HEADER_W + 1 + SRC_W + 1 + WINDOW_W
OVERLAP_AT = 0.72        # local rendering lane becomes free once occupant is this far through


def blend(rgb: tuple[int, int, int], onto: tuple[int, int, int], alpha: float) -> tuple[int, int, int]:
    alpha = max(0.0, min(1.0, alpha))
    return tuple(int(onto[i] + (rgb[i] - onto[i]) * alpha) for i in range(3))  # type: ignore


def edge_alpha(col: int, width: int) -> float:
    edge = max(4, int(width * 0.12))
    if col < edge:
        return max(0.0, col / edge)
    if col > width - edge - 1:
        return max(0.0, (width - 1 - col) / edge)
    return 1.0


def head_col(s: Stream, now: float, width: int) -> float:
    travel = width + TOTAL_W
    speed = travel / s.duration
    return width - speed * s.elapsed(now)


def chip_chars(s: Stream, now: float) -> list[tuple[str, tuple[int, int, int]]]:
    tag_color = ACCENT_REPLAY if s.replay else LEVEL_COLOR[s.level]
    header = f"\u258c{s.level.upper():<5}"
    src = f"{s.source:<{SRC_W}}"[:SRC_W]
    off = s.reveal_offset(now)
    window = s.message[off: off + WINDOW_W].ljust(WINDOW_W)
    msg_color = MSG_COLOR[s.level]

    chars: list[tuple[str, tuple[int, int, int]]] = []
    for ch in header:
        chars.append((ch, tag_color))
    chars.append((" ", INK_DIM))
    for ch in src:
        chars.append((ch, INK_DIM))
    chars.append((" ", INK_DIM))
    for ch in window:
        chars.append((ch, msg_color))
    return chars


# -------------------------------------------------------------- rain view --

class RainView(Widget):
    can_focus = True

    def __init__(self, engine: RainEngine, **kw):
        super().__init__(**kw)
        self.engine = engine
        self.display_lane: dict[int, int] = {}          # stream id -> local row, for THIS renderer only
        self.lane_next_available: dict[int, float] = {}
        self.caught_stream_id: Optional[int] = None

    def on_mount(self) -> None:
        self.set_interval(1 / 30, self._tick)
        self.run_worker(self._ensure_started(), exclusive=False)

    async def _ensure_started(self) -> None:
        if not self.engine.started:
            await self.engine.start()

    def _lane_count(self) -> int:
        return max(1, self.size.height)

    def _sync_lanes(self) -> None:
        """Assign each currently-visible engine stream a local display row,
        the same overlap-tolerant way the browser client does independently."""
        now = time.monotonic()
        live_ids = set()
        for s in self.engine.streams:
            live_ids.add(s.id)
            if s.id not in self.display_lane:
                lane_count = self._lane_count()
                candidates = [i for i in range(lane_count) if self.lane_next_available.get(i, 0.0) <= now]
                lane = candidates[s.id % len(candidates)] if candidates else (s.id % lane_count)
                self.display_lane[s.id] = lane
                self.lane_next_available[lane] = now + s.duration * OVERLAP_AT
        for sid in list(self.display_lane):
            if sid not in live_ids:
                del self.display_lane[sid]

    def _tick(self) -> None:
        self._sync_lanes()
        self._update_status()
        self.refresh()

    def _update_status(self) -> None:
        try:
            status = self.app.query_one("#status", Static)
        except Exception:
            return
        e = self.engine
        if e.playback is not None:
            from playback import clock_label
            live = f"  ·  {len(e.flagged)} live since" if e.flagged else ""
            status.update(clock_label(e.playback, time.monotonic(), time.strftime("%Y%j"))
                          + live + "  ·  [p] pause  [t] next window  [x] live")
        elif e.present and e.live_paused:
            status.update(f"paused - {len(e.streams)} held  ·  {len(e.flagged)} held back  ·  press [p] to resume")
        elif e.present:
            status.update(f"streaming - {len(e.streams)} active  ·  filter: {e.level_filter}")
        else:
            status.update(f"away - {len(e.flagged)} flagged  ·  press [a] to return and replay")

    # -- input ---------------------------------------------------------
    def on_click(self, event: events.Click) -> None:
        if self.caught_stream_id is not None:
            return
        now = time.monotonic()
        width = self.size.width
        lane_streams = sorted(
            (s for s in self.engine.streams
             if self.display_lane.get(s.id) == event.y and s.dismiss_start is None),
            key=lambda s: -s.id,
        )
        for s in lane_streams:
            start = int(round(head_col(s, now, width)))
            if start <= event.x < start + TOTAL_W:
                self._catch(s)
                return

    def _catch(self, s: Stream) -> None:
        self.engine.catch(s.id, "tui")
        self.caught_stream_id = s.id
        self.app.push_screen(DetailScreen(s, self.engine.explain(s)), self._on_detail_closed)

    def _on_detail_closed(self, result: Optional[str]) -> None:
        sid = self.caught_stream_id
        self.caught_stream_id = None
        if sid is None:
            return
        self.engine.release(sid)

    # -- rendering -------------------------------------------------------
    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if width <= 0:
            return Strip.blank(0)
        now = time.monotonic()

        cells: list[tuple[str, tuple[int, int, int], bool]] = [(" ", BG, False) for _ in range(width)]
        lane_streams = sorted(
            (s for s in self.engine.streams if self.display_lane.get(s.id) == y),
            key=lambda s: s.id,
        )

        for s in lane_streams:
            alpha = s.alpha(now)
            if alpha <= 0.02:
                continue
            col = int(round(head_col(s, now, width)))
            chip = chip_chars(s, now)
            fresh = s.is_fresh(now)
            for i, (ch, rgb) in enumerate(chip):
                c = col + i
                if 0 <= c < width:
                    ea = 1.0 if s.caught else edge_alpha(c, width)
                    final_alpha = alpha * ea
                    if final_alpha <= 0.02:
                        continue
                    if fresh:
                        rgb = tuple(min(255, int(v * 1.5)) for v in rgb)  # type: ignore
                    cells[c] = (ch, blend(rgb, BG, final_alpha), s.caught)

        segments: list[Segment] = []
        buf = ""
        cur_color: Optional[tuple[int, int, int]] = None
        cur_bold: Optional[bool] = None
        for ch, color, bold in cells:
            if color == cur_color and bold == cur_bold:
                buf += ch
            else:
                if buf:
                    segments.append(Segment(buf, Style(color=Color.from_rgb(*cur_color), bold=cur_bold)))  # type: ignore
                buf, cur_color, cur_bold = ch, color, bold
        if buf:
            segments.append(Segment(buf, Style(color=Color.from_rgb(*cur_color), bold=cur_bold)))  # type: ignore
        return Strip(segments)


# ---------------------------------------------------------------- detail --

class DetailScreen(ModalScreen[str]):
    DEFAULT_CSS = """
    DetailScreen { align: center middle; }
    #detail-box {
        width: 64; height: auto; padding: 1 2;
        background: rgb(14,21,16);
        border: round rgb(244,230,196);
    }
    #detail-meta { color: rgb(107,125,108); margin-bottom: 1; }
    #detail-msg { margin-bottom: 1; }
    #detail-hint { color: rgb(75,90,74); margin-top: 1; }
    #detail-title { margin-top: 1; color: rgb(224,149,31); }
    .detail-expl { margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "release", "Release")]

    def __init__(self, stream: Stream, explanation: Optional[dict] = None):
        super().__init__()
        self.stream = stream
        self.explanation = explanation or {"found": False}

    def compose(self) -> ComposeResult:
        s = self.stream
        with Vertical(id="detail-box"):
            yield Static(f"[b]{s.level.upper()}[/b]")
            yield Static(f"{s.ts}  ·  {s.source}", id="detail-meta")
            yield Static(s.message, id="detail-msg")
            x = self.explanation
            if x.get("found"):
                yield Static(f"[b]{escape(x['category']).upper()}[/b]  {escape(x['title'])}", id="detail-title")
                yield Static(f"[dim]WHAT[/dim]  {escape(x['what'])}", classes="detail-expl")
                yield Static(f"[dim]WHY[/dim]   {escape(x['why'])}", classes="detail-expl")
            else:
                yield Static("[dim]not catalogued yet: nobody has written down what this kind of entry is[/dim]", id="detail-title")
            yield Static("click outside this box, or press esc, to let it drift on", id="detail-hint")

    def action_release(self) -> None:
        self.dismiss("release")

    def on_click(self, event: events.Click) -> None:
        box = self.query_one("#detail-box")
        if box.region.contains(event.screen_x, event.screen_y):
            return
        self.dismiss("release")


# ------------------------------------------------------------------- app --

class LogRainApp(App):
    CSS = """
    Screen { background: rgb(11,16,13); }
    #topbar { dock: top; height: 1; padding: 0 1; color: rgb(107,125,108); }
    #status { dock: bottom; height: 1; padding: 0 1; color: rgb(107,125,108); }
    RainView { height: 1fr; }
    """
    BINDINGS = [
        Binding("a", "toggle_presence", "away/present"),
        Binding("1", "filter_all", "all"),
        Binding("2", "filter_info", "info"),
        Binding("3", "filter_warn", "warn"),
        Binding("4", "filter_error", "error"),
        Binding("t", "timeline", "timeline"),
        Binding("p", "pause_timeline", "pause"),
        Binding("x", "live", "live"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, engine: RainEngine, **kw):
        super().__init__(**kw)
        self.engine = engine

    def compose(self) -> ComposeResult:
        yield Static("ALM — ambient log monitor   [a] away/present   [1-4] filter   [t] timeline   [q] quit", id="topbar")
        yield RainView(self.engine, id="rain")
        yield Static("streaming", id="status")

    def action_toggle_presence(self) -> None:
        self.engine.toggle_presence()

    def action_timeline(self) -> None:
        """Cycle live -> 5m -> 30m -> 1h -> 6h -> 12h -> 24h -> live."""
        from playback import next_window
        e = self.engine
        if e.history is None:
            self.notify("no history store: start with a real source and without --no-history", severity="warning")
            return
        nxt = next_window(e.playback.window_s if e.playback and not e.playback.flagged_only else None)
        if nxt is None:
            e.stop_playback()
        else:
            e.start_playback(nxt)

    def action_pause_timeline(self) -> None:
        pb = self.engine.playback
        if pb is not None:
            self.engine.resume_playback() if pb.paused else self.engine.pause_playback()
        elif self.engine.live_paused:
            self.engine.resume_live()
        else:
            self.engine.pause_live()

    def action_live(self) -> None:
        self.engine.stop_playback()

    def action_filter_all(self) -> None:
        self.engine.set_filter("all")

    def action_filter_info(self) -> None:
        self.engine.set_filter("info")

    def action_filter_warn(self) -> None:
        self.engine.set_filter("warn")

    def action_filter_error(self) -> None:
        self.engine.set_filter("error")


if __name__ == "__main__":
    engine = RainEngine()
    LogRainApp(engine).run(mouse=True)
