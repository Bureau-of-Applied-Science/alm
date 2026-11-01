import pytest

from burst import BurstDetector
from rain_engine import RainEngine


def ufw(ip, port):
    return ("info", "ufw", f"block TCP {ip} -> :{port}")


def feed(d, t0, n, ip="45.33.32.156", gap=1.0, port=lambda i: 22):
    out = []
    for i in range(n):
        r = d.observe(t0 + i * gap, *ufw(ip, port(i)))
        if r:
            out.append((i, r))
    return out


def test_alert_at_threshold_once():
    alerts = feed(BurstDetector(threshold=8), 0, 20)
    assert [i for i, _ in alerts] == [7]
    assert alerts[0][1][0] == "error" and "45.33.32.156 hit us 8x" in alerts[0][1][2]


def test_port_scan_message_counts_ports():
    (_, (_, _, msg)), = feed(BurstDetector(threshold=8), 0, 8, port=lambda i: 1000 + i)
    assert "8 ports" in msg and "ufw" in msg


def test_slow_trickle_never_alerts():
    assert feed(BurstDetector(threshold=8, window_s=60), 0, 30, gap=30) == []


def test_realerts_after_cooldown_and_on_escalation():
    d = BurstDetector(threshold=8, window_s=60, cooldown_s=300)
    assert len(feed(d, 0, 8)) == 1                       # first alert
    assert feed(d, 8, 10, gap=1) == []                   # same burst continuing: quiet
    assert len(feed(d, 1000, 8)) == 1                    # after cooldown: again


def test_ignores_private_and_non_probe_lines():
    d = BurstDetector(threshold=3)
    for ip in ("192.168.1.5", "10.0.0.2", "100.64.1.1", "127.0.0.1"):
        assert feed(d, 0, 10, ip=ip) == []
    for i in range(10):
        assert d.observe(i, "info", "sshd", "Accepted publickey for ubuntu from 45.33.32.158") is None
        assert d.observe(i, "info", "caddy", "GET x -> 200 45.33.32.158") is None


def test_sshd_and_f2b_warn_count_together():
    d = BurstDetector(threshold=4)
    seq = [("warn", "sshd", "Invalid user a from 45.33.32.157 port 1"),
           ("warn", "f2b:sshd", "found 45.33.32.157"),
           ("warn", "sshd", "Invalid user b from 45.33.32.157 port 2"),
           ("warn", "f2b:sshd", "found 45.33.32.157")]
    res = [d.observe(i, *r) for i, r in enumerate(seq)]
    assert res[:3] == [None] * 3
    assert "f2b:sshd+sshd" in res[3][2]


def test_engine_emits_alert_even_when_info_is_sampled_away():
    e = RainEngine(mock=False)
    for i in range(12):
        e.ingest("info", "ufw", f"block TCP 45.33.32.156 -> :{2000 + i}")
    msgs = [s.message for s in e.streams]
    assert any("45.33.32.156 hit us" in m for m in msgs)
    assert sum(1 for s in e.streams if s.source == "ufw") < 12      # sampling really did drop some
