"""Timeline playback scheduling, DESIGN 5.6. Pure logic: no asyncio, no engine, takes the clock as an argument.

A virtual clock sweeps from v_start to v_end over `duration_s` of real time. Each stored entry becomes due
when the virtual clock reaches its timestamp. Playback is a digest: a per-second budget is spent errors
first (never dropped), then warns, then everything else, each sampled evenly.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Optional

from history import Row

WINDOWS = {"5m": 300, "30m": 1800, "1h": 3600, "6h": 21600, "12h": 43200, "24h": 86400}
# (window seconds, playback seconds), DESIGN 8. Other windows (away-replay) interpolate between these.
DURATIONS = [(300, 30), (1800, 60), (3600, 90), (21600, 150), (43200, 180), (86400, 240)]
BUDGET_PER_S = 2.0
MIN_DURATION_S = 15.0


def playback_duration(window_s: float) -> float:
    if window_s <= DURATIONS[0][0]:
        return max(MIN_DURATION_S, DURATIONS[0][1] * window_s / DURATIONS[0][0])
    for (w0, d0), (w1, d1) in zip(DURATIONS, DURATIONS[1:]):
        if window_s <= w1:
            return d0 + (d1 - d0) * (window_s - w0) / (w1 - w0)
    return float(DURATIONS[-1][1])


def pick_even(items: list, n: int) -> list:
    """n items spread evenly across the list, keeping order."""
    if n <= 0:
        return []
    if n >= len(items):
        return list(items)
    return [items[int(i * len(items) / n)] for i in range(n)]


def _by_source(rows: list[Row]) -> dict[str, list[Row]]:
    groups: dict[str, list[Row]] = {}
    for r in rows:
        groups.setdefault(r[2], []).append(r)
    return groups


def _fair(groups: dict[str, list[Row]], n: int, rot: int) -> list[Row]:
    """n rows shared round-robin across sources (start rotated per bucket so no source is always first),
    each source contributing an evenly spread sample of its own rows."""
    if n <= 0 or not groups:
        return []
    names = sorted(groups)
    k = rot % len(names)
    names = names[k:] + names[:k]                              # rotate the start per bucket: no source is always first
    lists = [pick_even(groups[s], n) for s in names]
    out: list[Row] = []
    i = 0
    while len(out) < n and any(i < len(l) for l in lists):
        for l in lists:
            if i < len(l) and len(out) < n:
                out.append(l[i])
        i += 1
    return out


def signature(row: Row) -> str:
    """A message template: source plus the first two words with digits masked, so every
    `Invalid user <name> from <ip> port N` probe shares one signature and `Accepted publickey` has another."""
    words = re.sub(r"\d+", "#", row[3].lower()).split()[:2]
    return row[2] + " " + " ".join(words)


NOISY_MIN = 20          # a template must repeat at least this often...
NOISY_SHARE = 0.02      # ...and make up this share of the window to count as noise


NOISY_SLOT_EVERY = 4    # one bucket in this many lets noisy templates go first, so they stay represented


def select_bucket(rows: list[Row], cap: int, rot: int, noisy: frozenset = frozenset()) -> list[Row]:
    """The digest for one bucket.

    * Errors are never dropped.
    * What room is left goes half to *priority* warns and half to the general pool; either side takes
      what the other cannot fill.
    * A template that repeats heavily across the window (scanner probes, cron, firewall blocks) is *noisy*.
      Noisy warns are demoted from the priority slot into the general pool, and within the general pool
      non-noisy entries go first, except in one bucket of NOISY_SLOT_EVERY where noisy ones lead, so a
      representative sample of the noise still appears.
    * Every pool is shared fairly across sources.
    """
    is_noisy = lambda r: signature(r) in noisy
    errors = [r for r in rows if r[1] == "error"]
    prio = [r for r in rows if r[1] == "warn" and not is_noisy(r)]
    general = [r for r in rows if r[1] != "error" and not (r[1] == "warn" and not is_noisy(r))]
    calm = _by_source([r for r in general if not is_noisy(r)])
    loud = _by_source([r for r in general if is_noisy(r)])
    prio_g = _by_source(prio)
    room = cap - len(errors)
    picked = list(errors)
    if room > 0:
        wn, rn = len(prio), len(general)
        w = min(wn, (room + 1) // 2 if rn else room)
        r = min(rn, room - w)
        w = min(wn, room - r)
        picked += _fair(prio_g, w, rot)
        tiers = [loud, calm] if rot % NOISY_SLOT_EVERY == 0 else [calm, loud]
        first = _fair(tiers[0], r, rot)
        picked += first + _fair(tiers[1], r - len(first), rot)
    return picked


class Playback:
    def __init__(self, rows: list[Row], v_start: float, v_end: float, real_start: float,
                 duration_s: Optional[float] = None, flagged_only: bool = False, budget: float = BUDGET_PER_S):
        self.v_start, self.v_end = v_start, v_end
        self.window_s = v_end - v_start
        self.duration_s = duration_s if duration_s is not None else playback_duration(self.window_s)
        self.rate = self.window_s / self.duration_s            # virtual seconds per real second
        self.real_start = real_start
        self.flagged_only = flagged_only
        self.kind = "timeline"                                 # or "catchup": the replay that follows a live pause
        self.budget = budget
        self.paused = False
        self._paused_v = v_start
        self.shown = 0
        self._dropped = 0                                      # planned entries the engine had no room to show
        self._last_now = real_start
        # The whole digest is planned up front, one bucket per second of real playback, so fairness holds
        # across the entire window and not just within a single tick.
        self._times = [r[0] for r in rows]                     # every entry, in time order (for skip accounting)
        n = max(1, int(self.duration_s + 0.999))
        cap = max(1, round(budget * self.duration_s / n))
        buckets: list[list[Row]] = [[] for _ in range(n)]
        for r in rows:
            buckets[min(n - 1, max(0, int((r[0] - v_start) / self.window_s * n)))].append(r)
        freq = Counter(signature(r) for r in rows)
        limit = max(NOISY_MIN, NOISY_SHARE * len(rows))
        noisy = frozenset(sig for sig, n in freq.items() if n > limit)
        self.noisy_signatures = noisy
        self._plan: list[Row] = []
        for i, b in enumerate(buckets):
            self._plan += sorted(select_bucket(b, cap, i, noisy), key=lambda r: r[0])
        self._i_all = 0
        self._i_plan = 0

    def virtual_now(self, now: float) -> float:
        if self.paused:
            return self._paused_v
        return min(self.v_end, self.v_start + (now - self.real_start) * self.rate)

    def pause(self, now: float) -> None:
        if not self.paused:
            self._paused_v = self.virtual_now(now)
            self.paused = True

    def resume(self, now: float) -> None:
        if self.paused:
            self.real_start = now - (self._paused_v - self.v_start) / self.rate
            self.paused = False

    @property
    def planned(self) -> int:
        return len(self._plan)

    @property
    def skipped(self) -> int:
        """Entries the clock has passed that were not shown: not in the digest, or no room on screen."""
        return (self._i_all - self._i_plan) + self._dropped

    @property
    def done(self) -> bool:
        return (self._i_all >= len(self._times) and self._i_plan >= len(self._plan)
                and self.virtual_now(self._last_now) >= self.v_end)

    def note_dropped(self) -> None:
        """The engine could not display a planned entry (screen full)."""
        self._dropped += 1
        self.shown -= 1

    def step(self, now: float) -> list[Row]:
        """Planned entries that have come due. Call every few hundred ms."""
        self._last_now = now
        if self.paused:
            return []
        vnow = self.virtual_now(now)
        while self._i_all < len(self._times) and self._times[self._i_all] <= vnow:
            self._i_all += 1
        out: list[Row] = []
        while self._i_plan < len(self._plan) and self._plan[self._i_plan][0] <= vnow:
            out.append(self._plan[self._i_plan])
            self._i_plan += 1
        self.shown += len(out)
        return out


def next_window(current_s: Optional[float]) -> Optional[float]:
    """Cycle order for a single key: none -> 5m -> 30m -> ... -> 24h -> none (live)."""
    order = list(WINDOWS.values())
    if current_s is None:
        return float(order[0])
    if current_s in order and order.index(current_s) + 1 < len(order):
        return float(order[order.index(current_s) + 1])
    return None


def window_label(sec: float) -> str:
    for name, s in WINDOWS.items():
        if s == sec:
            return name
    return f"{sec / 3600:.1f}h".replace(".0h", "h") if sec >= 3600 else f"{round(sec / 60)}m"


def clock_label(pb: "Playback", now: float, today: str) -> str:
    """Status-line text for the playback clock, e.g. `◷ Thu 14:07  -6h  +12`.
    `today` is the local date string (strftime %Y%j) so a day tick shows when the head is on another day."""
    import time as _t
    v = pb.virtual_now(now)
    lt = _t.localtime(v)
    stamp = _t.strftime("%H:%M", lt)
    if _t.strftime("%Y%j", lt) != today:
        stamp = _t.strftime("%a ", lt) + stamp
    window = "since you left" if pb.flagged_only else "-" + window_label(pb.window_s)
    extra = f"  +{pb.skipped}" if pb.skipped else ""
    return f"◷ {stamp}  {window}{extra}" + ("  paused" if pb.paused else "")
