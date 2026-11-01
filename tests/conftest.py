import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import time as _time
import types

import pytest


class Clock:
    """Fake monotonic clock. Installed as a shim for rain_engine's `time` name only; patching the
    real time.monotonic would freeze asyncio's event loop too."""

    def __init__(self):
        self.t = 1000.0

    EPOCH0 = 1_800_000_000.0      # arbitrary fixed wall time at t == 1000

    def __call__(self):
        return self.t

    def wall(self):
        return self.EPOCH0 + (self.t - 1000.0)


@pytest.fixture
def clock(monkeypatch):
    import rain_engine
    c = Clock()
    shim = types.SimpleNamespace(monotonic=c, strftime=_time.strftime, time=c.wall, localtime=_time.localtime)
    monkeypatch.setattr(rain_engine, "time", shim)
    return c
