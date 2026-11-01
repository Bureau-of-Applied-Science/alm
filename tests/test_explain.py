"""Signatures and explanation lookup (explain.py)."""
import json
from pathlib import Path

import pytest

from explain import fill, lookup, signature
from history import History
from rain_engine import RainEngine


def test_signature_collapses_addresses_numbers_and_users():
    a = signature("ufw", "block TCP 147.185.133.145 -> :802")
    b = signature("ufw", "block TCP 8.8.4.4 -> :22222")
    assert a[0] == b[0] == "ufw|block TCP <ip> -> :<n>"
    assert a[1] == {"ip": "147.185.133.145", "n": "802"}
    sig, params = signature("sshd", "Invalid user admin from 1.2.3.4 port 5555")
    assert sig == "sshd|Invalid user <user> from <ip> port <n>" and params["user"] == "admin"
    assert signature("sshd", "Received disconnect from 1.2.3.4 port 62584:11: Bye Bye [preauth]")[0] == \
        "sshd|Received disconnect from <ip> port <n>:<n>: Bye Bye [preauth]"       # port:code is not an IPv6 address


def test_caddy_sentences_keep_the_verb_and_status_but_not_the_visitor_or_path():
    sig, params = signature("caddy", "Safari on iPhone probed /.env (404) from 1.2.3.4")
    assert sig == "caddy|<who> probed <path> (404) from <ip>"
    assert params == {"who": "Safari on iPhone", "path": "/.env", "ip": "1.2.3.4", "status": "404"}
    assert signature("caddy", "curl viewed / from 9.9.9.9")[0] == "caddy|<who> viewed <path> from <ip>"


def test_sudo_commands_share_one_signature():
    sig, params = signature("sudo", "  ubuntu : PWD=/home/ubuntu ; USER=root ; COMMAND=/usr/bin/true")
    assert sig == "sudo|<user> ran a command as <as>" and params["cmd"] == "/usr/bin/true"


def test_fill_leaves_a_question_mark_for_an_unknown_name():
    assert fill("{ip} on {n} and {nope}", {"ip": "1.2.3.4", "n": "22"}) == "1.2.3.4 on 22 and ?"


def test_lookup_fills_the_stored_text_and_reports_a_miss():
    h = History(":memory:")
    sig, _ = signature("ufw", "block TCP 1.1.1.1 -> :1")
    h.put_explanation(sig, "ufw", "Firewall", "Blocked port {n}", "From {ip}.", "Because.")
    got = lookup(h, "ufw", "block TCP 5.6.7.8 -> :443")
    assert got["found"] and got["title"] == "Blocked port 443" and got["what"] == "From 5.6.7.8."
    miss = lookup(h, "ufw", "something never seen")
    assert miss["found"] is False and miss["sig"].startswith("ufw|")
    assert lookup(None, "ufw", "block TCP 1.1.1.1 -> :1")["found"] is False


def test_the_explanations_table_survives_pruning(tmp_path):
    h = History(tmp_path / "h.db", keep_s=10)
    h.put_explanation("s|x", "s", "Cat", "T", "W", "Y")
    h.add(1, "info", "s", "old"); h.prune(10_000)
    assert h.count() == 0 and h.explanation("s|x")["title"] == "T"


def test_engine_explains_a_caught_stream():
    h = History(":memory:")
    h.put_explanation("mock|hello <n>", "mock", "Test", "Hi {n}", "w", "y")
    e = RainEngine(history=h)
    e.ingest("info", "mock", "hello 42")
    s = e.streams[-1]
    assert e.explain(s)["title"] == "Hi 42"


KNOWN = json.loads((Path(__file__).parent.parent / "knowledge" / "explanations.json").read_text())


def test_the_shipped_explanations_are_complete_and_their_placeholders_resolvable():
    seen = set()
    for r in KNOWN:
        assert all(r.get(k) for k in ("sig", "source", "category", "title", "what", "why")), r["sig"]
        assert r["sig"] not in seen and r["sig"].startswith(r["source"] + "|")
        seen.add(r["sig"])
        assert "—" not in r["what"] + r["why"] + r["title"]


def test_caddy_service_lines_share_a_signature_across_subjects():
    a = signature("caddy", "tls.renew: renewing certificate · sc.example.com")
    b = signature("caddy", "tls.renew: renewing certificate · other.example.org")
    assert a[0] == b[0] == "caddy|tls.renew: renewing certificate" and a[1] == {"subject": "sc.example.com"}
