"""The demo data (demo.py): invented, explainable, deterministic, and safe to put in screenshots."""
import asyncio
import ipaddress
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import demo  # noqa: E402
import explain  # noqa: E402
from history import History  # noqa: E402
from rain_engine import RainEngine  # noqa: E402

NOW = 1_800_000_000.0
DOC = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")]
IPS = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\b(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}\b", re.I)


def test_seed_writes_a_day_of_history_oldest_first_and_is_deterministic():
    a, b = History(":memory:"), History(":memory:")
    n = demo.seed(a, NOW)
    assert demo.seed(b, NOW) == n and n > 3000
    rows = a.between(NOW - 86400, NOW)
    assert rows == b.between(NOW - 86400, NOW)
    assert rows[0][0] < NOW - 23 * 3600 and rows[-1][0] > NOW - 60
    assert {r[1] for r in rows} == {"info", "warn", "error"}


def test_every_address_is_a_documentation_address():
    h = History(":memory:")
    demo.seed(h, NOW)
    for _, _, _, msg in h.between(0, NOW + 1):
        for m in IPS.findall(msg):
            assert any(ipaddress.ip_address(m) in net for net in DOC), msg


def test_every_demo_entry_has_a_shipped_explanation():
    h = History(":memory:")
    explain.load_catalogue(h)
    demo.seed(h, NOW)
    missing = {explain.signature(s, m)[0] for _, _, s, m in h.between(0, NOW + 1)
               if not explain.lookup(h, s, m)["found"]}
    assert not missing, sorted(missing)[:5]


async def test_the_live_feed_ingests_entries():
    e = RainEngine(mock=False, history=History(":memory:"))
    t = asyncio.create_task(demo.feed(e, pace=50))
    await asyncio.sleep(1.0)
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    assert e.history.count() > 5
