"""Spot one address hammering us: N suspicious events from the same IP inside a window becomes one
loud entry, instead of hoping the individual lines (mostly sampled-away info) catch the eye."""
from __future__ import annotations

import ipaddress
import re
from collections import defaultdict, deque
from typing import Optional

IP = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
PORT = re.compile(r"-> :(\d+)")
# sources whose warn/error lines are probing signals; ufw blocks count at any level
PROBE_SOURCES = ("sshd", "caddy", "f2b:")


def _public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_global and not a.is_multicast


class BurstDetector:
    def __init__(self, threshold: int = 8, window_s: float = 60.0, cooldown_s: float = 300.0):
        self.threshold, self.window_s, self.cooldown_s = threshold, window_s, cooldown_s
        self._hits: dict[str, deque] = defaultdict(deque)      # ip -> (t, port|None, source)
        self._last_alert: dict[str, tuple[float, int]] = {}    # ip -> (t, hit count at alert)

    def observe(self, now: float, level: str, source: str, message: str) -> Optional[tuple[str, str, str]]:
        """Returns (level, source, message) for a synthetic alert when an IP crosses the threshold."""
        if not (source == "ufw" or (level in ("warn", "error") and source.startswith(PROBE_SOURCES))):
            return None
        m = IP.search(message)
        if not m or not _public(m.group(1)):
            return None
        ip = m.group(1)
        port = PORT.search(message)
        q = self._hits[ip]
        q.append((now, port.group(1) if port else None, source))
        while q and now - q[0][0] > self.window_s:
            q.popleft()
        if len(self._hits) > 2000:                              # bound memory under a wide scan
            for k in [k for k, v in self._hits.items() if not v or now - v[-1][0] > self.window_s]:
                del self._hits[k]
        n = len(q)
        last = self._last_alert.get(ip)
        # alert at the threshold, again only after the cooldown or if the pace has quadrupled since
        if n < self.threshold or (last and now - last[0] < self.cooldown_s and n < last[1] * 4):
            return None
        self._last_alert[ip] = (now, n)
        ports = {p for _, p, _ in q if p}
        srcs = sorted({s for _, _, s in q})
        what = f"{len(ports)} ports, " if len(ports) > 1 else ""
        return "error", "alm", f"{ip} hit us {n}x in {int(self.window_s)}s ({what}{'+'.join(srcs)})"
