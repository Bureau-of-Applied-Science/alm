"""
Headless log-rain engine.

Owns the simulated log source, the lifecycle of each entry (spawn, catch,
release, dismiss, exit), and presence/away banking - independent of any
particular renderer. The terminal UI and the web UI are both thin clients
over this: they read state and call the same catch()/release()/dismiss()
methods, and they both hear about every change (from either side) via
subscribe().
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Optional, Union

from burst import BurstDetector
from history import History
from playback import WINDOWS, Playback

# --------------------------------------------------------------- mock data --

SOURCES = [
    "auth-svc", "network-daemon", "disk-monitor", "api-gateway", "sched-worker",
    "kernel", "backup-agent", "cache-node", "ingest-pipe", "session-mgr",
]
MESSAGES = {
    "info": [
        "connection established, handshake complete",
        "health check passed, latency 12ms",
        "cache warmed, 4821 keys loaded",
        "rotated credentials for service account",
        "scheduled job completed in 0.8s",
        "new peer joined cluster, rebalancing shards across 3 nodes to maintain even distribution",
    ],
    "debug": [
        "gc pause 3ms, heap at 61%",
        "poll returned empty, backing off 200ms",
        "cache miss on key user:5521, falling through to origin",
        "thread pool size adjusted to 12",
    ],
    "warn": [
        "retry 2/5 after timeout, backing off",
        "disk usage at 82%, approaching threshold",
        "response time degraded, p95 above 400ms",
        "certificate expires in 9 days, renewal not yet scheduled",
        "queue depth climbing, consumer lag now 3400 messages behind producer on partition 2",
    ],
    "error": [
        "connection refused by upstream, marking node unhealthy",
        "unhandled exception in request handler, returning 500",
        "write failed after 3 retries, entry dropped",
        "segmentation fault in worker process, restarting supervisor",
        "panic: index out of range [12] with length 8 - handler.go:44 -> dispatch.go:112 -> main.go:37",
    ],
}
LEVEL_WEIGHTS = [("info", 0.55), ("debug", 0.18), ("warn", 0.19), ("error", 0.08)]

WINDOW_W = 44             # capped "reading window" width entries reveal through, in characters
DISMISS_FADE_S = 0.6
FRESH_GLOW_S = 0.45
CAUGHT_TTL_S = 120.0        # a catch nobody has touched this long is released (it would otherwise freeze forever)
CATCHUP_MIN_S = 5.0       # a live pause shorter than this just resumes (warn/error replay); longer gets a catch-up replay
MAX_ACTIVE = 30            # safety cap on concurrently-alive entries
INFO_PER_SEC = 2.0         # ingest sampling: info/debug beyond this rate are dropped; warn/error always pass

# Entry lifetime (duration) is the sole shared knob controlling traversal
# speed for every renderer (speed = (viewport + footprint) / duration, see
# DESIGN.md 4.2). A terminal column is a much coarser visual unit than a
# browser pixel, so a duration tuned only against message length left the
# terminal renderer feeling like it was jumping rather than gliding. Base and
# per-character values below are picked to keep a typical terminal width
# (~120-160 cols) in a comfortably readable range (roughly 12-18 cols/sec).
ENTRY_BASE_DURATION_S = 12.0
ENTRY_LENGTH_COEF_S = 0.03


def pick_level() -> str:
    r = random.random()
    acc = 0.0
    for lvl, w in LEVEL_WEIGHTS:
        acc += w
        if r <= acc:
            return lvl
    return "info"


def make_entry() -> tuple[str, str, str, str]:
    level = pick_level()
    message = random.choice(MESSAGES[level])
    source = random.choice(SOURCES)
    ts = time.strftime("%H:%M:%S")
    return level, source, message, ts


@dataclass
class Stream:
    id: int
    level: str
    source: str
    message: str
    ts: str
    spawn_time: float          # time.monotonic() at spawn (shifted on release to preserve elapsed)
    duration: float
    replay: bool = False
    caught: bool = False
    caught_elapsed: float = 0.0
    caught_by: Optional[str] = None      # which client caught it, so its disconnect can release it
    caught_at: float = 0.0
    dismiss_start: Optional[float] = None
    frozen_elapsed: float = 0.0
    held_elapsed: Optional[float] = None  # set while the timeline is paused: the stream stands still, its clock too

    def elapsed(self, now: float) -> float:
        if self.caught:
            return self.caught_elapsed
        if self.dismiss_start is not None:
            return self.frozen_elapsed
        if self.held_elapsed is not None:
            return self.held_elapsed
        return now - self.spawn_time

    def reveal_offset(self, now: float) -> int:
        max_off = max(0, len(self.message) - WINDOW_W)
        if max_off <= 0:
            return 0
        frac = min(1.0, self.elapsed(now) / (self.duration * 0.94))
        return int(frac * max_off)

    def alpha(self, now: float) -> float:
        if self.dismiss_start is not None:
            t = (now - self.dismiss_start) / DISMISS_FADE_S
            return max(0.0, 1.0 - t)
        return 1.0

    def is_fresh(self, now: float) -> bool:
        return (now - self.spawn_time) < FRESH_GLOW_S and self.dismiss_start is None

    def to_dict(self, now: Optional[float] = None) -> dict:
        now = now if now is not None else time.monotonic()
        return {
            "id": self.id, "level": self.level, "source": self.source,
            "message": self.message, "ts": self.ts, "duration": self.duration,
            "replay": self.replay, "caught": self.caught,
            "elapsed": self.elapsed(now),
            "dismiss_elapsed": (self.frozen_elapsed if self.dismiss_start is not None else None),
        }


class RainEngine:
    """The single source of truth. Runs its own spawn/reap loops as asyncio
    tasks; any number of subscribers (terminal, web sockets) can watch it and
    steer it concurrently."""

    def __init__(self, mock: bool = True, history: Optional["History"] = None) -> None:
        self.mock = mock
        self.history = history
        self._last_prune = 0.0
        self.playback: Optional[Playback] = None
        self._playback_task: Optional[asyncio.Task] = None
        self._held = False                       # streams on screen are held (timeline paused, or live paused)
        self.live_paused = False                 # live view paused by the user: new entries bank like they do while away
        self._paused_wall = 0.0                  # wall time the live pause began: where the catch-up replay starts
        self._away_since: Optional[float] = None
        self.burst = BurstDetector()
        self.local_bursts = True       # False when a collector on the watched host raises the alerts
        self._tokens = INFO_PER_SEC     # token bucket for info/debug ingest
        self._tokens_at = time.monotonic()
        self.streams: list[Stream] = []
        self.next_id = 0
        self.present = True
        self.level_filter = "all"
        self.flagged: list[tuple[str, str, str, str]] = []
        self._subscribers: list[asyncio.Queue] = []
        self._spawn_task: Optional[asyncio.Task] = None
        self._reap_task: Optional[asyncio.Task] = None

    # -- pub/sub ---------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _broadcast(self, event: dict) -> None:
        for q in list(self._subscribers):
            q.put_nowait(event)

    # -- lifecycle ---------------------------------------------------------
    @property
    def started(self) -> bool:
        return self._spawn_task is not None

    async def start(self) -> None:
        if self.started:
            return
        self._spawn_task = asyncio.create_task(self._spawn_loop() if self.mock else asyncio.sleep(float("inf")))
        self._reap_task = asyncio.create_task(self._reap_loop())

    def stop(self) -> None:
        if self.history:
            self.history.flush()
        if self._spawn_task:
            self._spawn_task.cancel()
        if self._reap_task:
            self._reap_task.cancel()

    async def _spawn_loop(self) -> None:
        while True:
            if self.live_paused:
                delay = 0.5
            elif self.present:
                level, source, message, ts = make_entry()
                passes = self.level_filter == "all" or self.level_filter == level
                if passes and len(self.streams) < MAX_ACTIVE:
                    self._add_stream(level, source, message, ts)
                delay = random.uniform(0.45, 1.3)
            else:
                level, source, message, ts = make_entry()
                if level in ("warn", "error"):
                    self.flagged.append((level, source, message, ts))
                    self._broadcast({"type": "flagged_count", "count": len(self.flagged)})
                delay = random.uniform(0.9, 2.2)
            await asyncio.sleep(delay)

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            self._reap(time.monotonic())

    def _reap(self, now: float) -> None:
        if self.history:
            self.history.flush()
            wall = time.time()
            if wall - self._last_prune > 3600:
                self._last_prune = wall
                self.history.prune(wall)
        keep = []
        for s in list(self.streams):
            if s.caught and now - s.caught_at > CAUGHT_TTL_S:
                self.release(s.id)               # nobody is reading it any more
            if s.caught:
                keep.append(s)
            elif s.dismiss_start is not None:
                if s.alpha(now) > 0.0:
                    keep.append(s)
                else:
                    self._broadcast({"type": "exit", "id": s.id})
            elif s.elapsed(now) < s.duration:
                keep.append(s)
            else:
                self._broadcast({"type": "exit", "id": s.id})
        self.streams = keep

    def _add_stream(self, level: str, source: str, message: str, ts: str, replay: bool = False) -> Stream:
        self.next_id += 1
        duration = (ENTRY_BASE_DURATION_S + min(len(message), 110) * ENTRY_LENGTH_COEF_S) * (1.15 if replay else 1.0)
        s = Stream(
            id=self.next_id, level=level, source=source, message=message, ts=ts,
            spawn_time=time.monotonic(), duration=duration, replay=replay,
        )
        if self._held:
            s.held_elapsed = 0.0
        self.streams.append(s)
        self._broadcast({
            "type": "spawn", "id": s.id, "level": s.level, "source": s.source,
            "message": s.message, "ts": s.ts, "duration": s.duration, "replay": s.replay,
        })
        return s

    @staticmethod
    def fmt_ts(epoch: float, today_only: bool = True) -> str:
        t = time.localtime(epoch)
        if not today_only and time.strftime("%Y%j", t) != time.strftime("%Y%j", time.localtime(time.time())):
            return time.strftime("%a %H:%M:%S", t)
        return time.strftime("%H:%M:%S", t)

    def _epoch_and_label(self, ts: Union[str, float, None]) -> tuple[float, str]:
        """Sources give a real epoch (float), a display string, or nothing; history needs an epoch."""
        if isinstance(ts, (int, float)):
            return float(ts), self.fmt_ts(float(ts))
        wall = time.time()
        return wall, ts or self.fmt_ts(wall)

    def record(self, level: str, source: str, message: str, ts: Union[str, float, None] = None,
               quiet: bool = False) -> None:
        """History only: no display, no burst detection. Used for sync. `quiet` marks a folded Caddy asset;
        the engine does not need it (sinks that store it, like the collector, do)."""
        if self.history:
            self.history.add(self._epoch_and_label(ts)[0], level, source, message)

    DESK_KINDS = {"opened": "info", "input": "warn", "dismissed": "warn"}

    def desk(self, kind: str, detail: str) -> None:
        """Someone interacted with the screensaver. Recorded to history only, so it shows up in timeline
        playback but never as a live drop (no display, no burst detection, no TUI)."""
        level = self.DESK_KINDS.get(kind)
        if level is None or not isinstance(detail, str) or not detail.strip():
            return
        self.record(level, "desk", " ".join(detail.split())[:200])

    def ingest(self, level: str, source: str, message: str, ts: Union[str, float, None] = None) -> Optional[Stream]:
        """Real-data entry point; same gating as the mock spawner."""
        epoch, ts = self._epoch_and_label(ts)
        if self.history:
            self.history.add(epoch, level, source, message)     # before any gating: playback needs the evidence
        alert = (self.burst.observe(time.monotonic(), level, source, message)   # before sampling can drop evidence
                 if self.local_bursts else None)
        if alert:
            self.ingest(*alert)
        if level not in ("warn", "error"):
            now = time.monotonic()
            self._tokens = min(INFO_PER_SEC * 2, self._tokens + (now - self._tokens_at) * INFO_PER_SEC)
            self._tokens_at = now
            if self._tokens < 1.0:
                return None
            self._tokens -= 1.0
        if not self.present or self.playback is not None or self.live_paused:
            if level in ("warn", "error"):                     # bank: away, paused, or a timeline is playing ("N live since")
                self.flagged.append((level, source, message, ts))
                self._broadcast({"type": "flagged_count", "count": len(self.flagged), "reason": self._flag_reason()})
            return None
        if self.level_filter != "all" and self.level_filter != level:
            return None
        if len(self.streams) >= MAX_ACTIVE:
            return None
        return self._add_stream(level, source, message, ts)

    def _flag_reason(self) -> str:
        if self.playback is not None and self.present:
            return "playback"
        return "paused" if self.live_paused and self.present else "away"

    def pause_live(self) -> bool:
        """Hold the live view: streams on screen stand still, new warn/error entries bank, the rest is only recorded."""
        if self.live_paused or self.playback is not None or not self.present:
            return False
        self.live_paused = True
        self._paused_wall = time.time()
        self._hold_streams(time.monotonic())
        self._broadcast({"type": "live_pause", "paused": True})
        return True

    def resume_live(self, replay: bool = True) -> None:
        if not self.live_paused:
            return
        self.live_paused = False
        self._unhold_streams(time.monotonic())
        self._broadcast({"type": "live_pause", "paused": False})
        if replay and not self._start_catchup() and self.flagged:
            self.replay_flagged()                              # too short to be worth a replay: just what warned

    def _start_catchup(self) -> bool:
        """Resume after a live pause: replay everything that arrived meanwhile as a short timeline ending at now."""
        wall = time.time()
        if self.history is None or self.playback is not None or wall - self._paused_wall < CATCHUP_MIN_S:
            return False
        rows = self.history.between(self._paused_wall, wall)
        self.flagged.clear()                                   # they are in the replay; do not show them twice
        self._broadcast({"type": "flagged_count", "count": 0})
        pb = Playback(rows, self._paused_wall, wall, time.monotonic())
        pb.kind = "catchup"
        self._begin_playback(pb)
        return True

    def replay_flagged(self) -> None:
        queue = self.flagged[:]
        self.flagged.clear()
        self._broadcast({"type": "flagged_count", "count": 0})

        async def run() -> None:
            for level, source, message, ts in queue:
                if len(self.streams) < MAX_ACTIVE:
                    self._add_stream(level, source, message, ts, replay=True)
                await asyncio.sleep(0.55)

        asyncio.create_task(run())

    # -- actions, symmetric regardless of which client calls them ----------
    def find(self, stream_id: int) -> Optional[Stream]:
        return next((s for s in self.streams if s.id == stream_id), None)

    def explain(self, s: Stream) -> dict:
        """What this entry is and why it is there (explain.py); `found` is False until someone has written it."""
        from explain import lookup
        return lookup(self.history, s.source, s.message)

    def catch(self, stream_id: int, owner: Optional[str] = None) -> Optional[Stream]:
        s = self.find(stream_id)
        if not s or s.caught:
            return s
        for other in self.streams:    # one caught at a time (DESIGN.md 5.1)
            if other.caught:
                self.release(other.id)
        now = time.monotonic()
        s.caught_elapsed = s.elapsed(now)
        s.caught = True
        s.caught_by, s.caught_at = owner, now
        self._broadcast({"type": "catch", "id": s.id, "elapsed": s.caught_elapsed})
        return s

    def release(self, stream_id: int) -> None:
        s = self.find(stream_id)
        if not s or not s.caught:
            return
        now = time.monotonic()
        s.spawn_time = now - s.caught_elapsed
        s.caught = False
        if self._held:
            s.held_elapsed = s.caught_elapsed       # released while the timeline is paused: stay put until it resumes
        self._broadcast({"type": "release", "id": s.id, "elapsed": s.caught_elapsed})

    def release_owner(self, owner: str) -> None:
        """A client went away: let go of whatever it had caught."""
        for s in list(self.streams):
            if s.caught and s.caught_by == owner:
                self.release(s.id)

    def dismiss(self, stream_id: int) -> None:
        s = self.find(stream_id)
        if not s:
            return
        now = time.monotonic()
        s.frozen_elapsed = s.caught_elapsed if s.caught else s.elapsed(now)
        s.dismiss_start = now
        s.caught = False
        self._broadcast({"type": "dismiss", "id": s.id, "elapsed": s.frozen_elapsed})

    def toggle_presence(self) -> bool:
        self.resume_live(replay=False)
        self.present = not self.present
        self._broadcast({"type": "presence", "present": self.present})
        wall = time.time()
        if not self.present:
            self._away_since = wall
        else:
            left, self._away_since = self._away_since, None
            if self.history and left is not None:
                # one mechanism, two entry points (DESIGN 5.6): away-return is "warn/error since I left"
                self.flagged.clear()
                self._broadcast({"type": "flagged_count", "count": 0})
                self.start_playback(min(max(wall - left, 60.0), 86400.0), flagged_only=True)
            elif self.flagged:
                self.replay_flagged()
        return self.present

    # -- timeline playback (DESIGN 5.6) ------------------------------------
    def _playback_event(self, state: str) -> dict:
        pb = self.playback
        assert pb is not None
        return {"type": "playback", "state": state, "window_s": pb.window_s, "v_start": pb.v_start,
                "v_end": pb.v_end, "rate": pb.rate, "virtual_now": pb.virtual_now(time.monotonic()),
                "paused": pb.paused, "skipped": pb.skipped, "flagged_only": pb.flagged_only, "kind": pb.kind}

    def start_playback(self, window_s: float, flagged_only: bool = False) -> bool:
        if self.history is None or window_s <= 0:
            return False
        self.resume_live(replay=False)         # a timeline takes over; what banked stays banked until it ends
        if self.playback is not None:
            self.stop_playback(replay_live=False)
        wall = time.time()
        rows = self.history.between(wall - window_s, wall, flagged_only)
        self._begin_playback(Playback(rows, wall - window_s, wall, time.monotonic(), flagged_only=flagged_only))
        return True

    def _begin_playback(self, pb: Playback) -> None:
        self.playback = pb
        self._broadcast(self._playback_event("start"))
        if self._playback_task is None or self._playback_task.done():
            try:
                self._playback_task = asyncio.get_running_loop().create_task(self._playback_loop())
            except RuntimeError:                               # no loop (unit tests drive _playback_tick by hand)
                self._playback_task = None

    def _hold_streams(self, now: float) -> None:
        self._held = True
        for s in self.streams:
            if not s.caught and s.dismiss_start is None and s.held_elapsed is None:
                s.held_elapsed = s.elapsed(now)

    def _unhold_streams(self, now: float) -> None:
        self._held = False
        for s in self.streams:
            if s.held_elapsed is not None:
                s.spawn_time, s.held_elapsed = now - s.held_elapsed, None

    def pause_playback(self) -> None:
        if self.playback and not self.playback.paused:
            self.playback.pause(time.monotonic())
            self._hold_streams(time.monotonic())
            self._broadcast(self._playback_event("pause"))

    def resume_playback(self) -> None:
        if self.playback and self.playback.paused:
            self.playback.resume(time.monotonic())
            self._unhold_streams(time.monotonic())
            self._broadcast(self._playback_event("resume"))

    def stop_playback(self, replay_live: bool = True, state: str = "stop") -> None:
        if self.playback is None:
            return
        evt = self._playback_event(state)
        self.playback = None
        self._unhold_streams(time.monotonic())
        self._broadcast(evt)
        if replay_live and self.flagged:
            self.replay_flagged()                              # warn/error that arrived while the timeline played

    def _playback_tick(self) -> None:
        pb = self.playback
        if pb is None:
            return
        now = time.monotonic()
        for epoch, level, source, message in pb.step(now):
            if self.level_filter != "all" and self.level_filter != level:
                continue
            if len(self.streams) >= MAX_ACTIVE:
                pb.note_dropped()
                continue
            self._add_stream(level, source, message, self.fmt_ts(epoch, today_only=False), replay=True)
        if pb.done:
            self.stop_playback(state="end")

    async def _playback_loop(self) -> None:
        last_stats = 0.0
        while self.playback is not None:
            await asyncio.sleep(0.25)
            self._playback_tick()
            pb = self.playback
            now = time.monotonic()
            if pb is not None and now - last_stats >= 1.0:
                last_stats = now
                self._broadcast({"type": "playback_stats", "skipped": pb.skipped, "shown": pb.shown})

    def set_filter(self, level: str) -> None:
        if level not in ("all", "info", "debug", "warn", "error"):
            return
        self.level_filter = level
        self._broadcast({"type": "filter", "level": level})

    def snapshot(self) -> dict:
        now = time.monotonic()
        return {
            "type": "snapshot",
            "present": self.present,
            "filter": self.level_filter,
            "flagged_count": len(self.flagged),
            "flagged_reason": self._flag_reason(),
            "live_paused": self.live_paused,
            "playback": self._playback_event("snapshot") if self.playback is not None else None,
            "streams": [s.to_dict(now) for s in self.streams],
        }
