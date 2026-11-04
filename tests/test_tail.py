import asyncio
import json

import pytest

from rain_engine import RainEngine
from tail import parse_journal, parse_line, tail_file


def test_parse_plain():
    assert parse_line("[12:00:01] ERROR pm2:web db down") == ("error", "pm2:web", "db down", "12:00:01")


def test_parse_json_and_aliases():
    line = json.dumps({"level": "WARNING", "source": "x", "message": "m"})
    assert parse_line(line)[:3] == ("warn", "x", "m")


def test_parse_garbage_is_info():
    assert parse_line("whatever this is")[:3] == ("info", "log", "whatever this is")
    assert parse_line("{broken json")[0] == "info"
    assert parse_line("NOTALEVEL foo bar baz")[0] == "info"


def test_parse_journal():
    rec = json.dumps({"MESSAGE": "disk failing", "PRIORITY": "3", "SYSLOG_IDENTIFIER": "kernel", "__REALTIME_TIMESTAMP": "1700000000000000"})
    level, src, msg, ts = parse_journal(rec)
    assert (level, src, msg) == ("error", "kernel", "disk failing") and ts == 1700000000.0
    assert parse_journal(json.dumps({"MESSAGE": list(b"bytes ok"), "PRIORITY": "7"}))[::2][0] == "debug"
    assert parse_journal(json.dumps({"PRIORITY": "3"})) is None
    assert parse_journal("not json") is None


async def test_tail_file_follows_appends_and_truncation(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("[01:00:00] ERROR old line before start\n")
    e = RainEngine(mock=False)
    t = asyncio.create_task(tail_file(e, str(p)))
    await asyncio.sleep(0.4)
    assert not e.streams                       # starts at end of file
    with open(p, "a") as f:
        f.write("[01:00:01] ERROR s first\n[01:00:02] ERROR s sec")   # second line incomplete
    await asyncio.sleep(0.5)
    assert [s.message for s in e.streams] == ["first"]
    with open(p, "a") as f:
        f.write("ond\n")
    await asyncio.sleep(0.5)
    assert [s.message for s in e.streams] == ["first", "second"]
    p.write_text("[01:00:03] ERROR s after truncate\n")
    await asyncio.sleep(0.5)
    assert e.streams[-1].message == "after truncate"
    t.cancel()


# ---- remote-source parsers, using line shapes sampled from a real Ubuntu server ----
from tail import CaddyFold, HistoryOnly, follow_command, parse_caddy, parse_fail2ban, parse_ufw  # noqa: E402

UFW_LINE = ("[1014917.941508] [UFW BLOCK] IN=ens3 OUT= MAC=fa:16 SRC=45.33.32.156 DST=1.1.1.1 LEN=60 "
            "PROTO=TCP SPT=26200 DPT=5432 WINDOW=38898 RES=0x00 SYN URGP=0")


def test_parse_ufw():
    assert parse_ufw(UFW_LINE) == ("info", "ufw", "block TCP 45.33.32.156 -> :5432")
    assert parse_ufw("[UFW BLOCK] SRC=1.2.3.4 PROTO=ICMP")[2] == "block ICMP 1.2.3.4"
    assert parse_ufw("unrelated kernel message") is None


def test_kernel_ufw_in_journal_becomes_ufw_source():
    rec = json.dumps({"MESSAGE": UFW_LINE, "SYSLOG_IDENTIFIER": "kernel", "PRIORITY": "6"})
    assert parse_journal(rec)[:3] == ("info", "ufw", "block TCP 45.33.32.156 -> :5432")


def test_sshd_probing_is_bumped_to_warn_but_logins_are_not():
    def lvl(msg):
        return parse_journal(json.dumps({"MESSAGE": msg, "SYSLOG_IDENTIFIER": "sshd", "PRIORITY": "6"}))[0]
    assert lvl("Invalid user admin from 1.2.3.4 port 5555") == "warn"
    assert lvl("Failed password for root from 1.2.3.4 port 5555 ssh2") == "warn"
    assert lvl("Accepted publickey for ubuntu from 10.0.0.2 port 4 ssh2") == "info"


def test_parse_fail2ban():
    found = "2026-10-29 23:40:24,056 fail2ban.filter         [3731]: INFO    [sshd] Found 45.33.32.157 - 2026-10-29 23:40:23"
    ban = "2026-10-29 23:41:00,000 fail2ban.actions        [3731]: NOTICE  [sshd] Ban 45.33.32.157"
    unban = "2026-10-29 23:51:00,000 fail2ban.actions        [3731]: NOTICE  [sshd] Unban 45.33.32.157"
    assert parse_fail2ban(found) == ("warn", "f2b:sshd", "found 45.33.32.157", None)
    assert parse_fail2ban(ban)[0] == "error" and parse_fail2ban(unban)[0] == "info"
    assert parse_fail2ban("2026-10-29 fail2ban.server [1]: INFO    Jail 'sshd' started") is None


def test_parse_caddy():
    SAFARI = "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 Mobile/15E148 Safari/604.1"
    GPT = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.4; +https://openai.com/gptbot)"

    def line(status, uri="/x", ua="curl/7.88.1", method="GET", ctype=None):
        return json.dumps({"ts": 1793314106.38, "msg": "handled request", "status": status,
                           "resp_headers": {"Content-Type": [ctype] if ctype else []},
                           "request": {"method": method, "host": "example.com", "uri": uri, "client_ip": "9.9.9.9",
                                       "headers": {"User-Agent": [ua]}}})
    assert parse_caddy(line(200))[0] == "info"
    assert parse_caddy(line(404, "/wp-login.php"))[0] == "warn"
    lvl, src, msg, ts = parse_caddy(line(502))
    assert (lvl, src, ts) == ("error", "caddy", 1793314106.38) and msg == "curl requested /x (502) from 9.9.9.9"
    assert parse_caddy(line(200, "/", SAFARI))[2] == "Safari on iPhone viewed / from 9.9.9.9"
    assert parse_caddy(line(200, "/fonts/a.woff2", GPT))[2] == "GPTBot loaded /fonts/a.woff2 from 9.9.9.9"
    assert parse_caddy(line(200, "/dl", ctype="image/png"))[2] == "curl loaded /dl from 9.9.9.9"
    assert parse_caddy(line(404, "/.env.bak"))[2] == "curl probed /.env.bak (404) from 9.9.9.9"
    assert parse_caddy(line(200, "/.well-known/security.txt"))[2].startswith("curl viewed")
    assert parse_caddy(line(308, "/", SAFARI))[2] == "Safari on iPhone was redirected at / (308) from 9.9.9.9"
    assert parse_caddy(line(304, "/a.css"))[2] == "curl loaded /a.css (cached) from 9.9.9.9"
    assert parse_caddy(line(401, "/api", method="POST"))[2] == "curl sent POST to /api (401) from 9.9.9.9"
    assert parse_caddy(line(200, "/", ua=""))[2].startswith("a client with no User-Agent viewed")
    assert len(parse_caddy(line(404, "/" + "a" * 300))[2]) < 120
    assert parse_caddy(json.dumps({"msg": "no request here"})) is None
    assert parse_caddy("not json") is None


def test_caddy_fold_folds_assets_into_the_page_view():
    def line(ts, uri, ip="9.9.9.9", ua="Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) Safari/604.1"):
        return json.dumps({"ts": ts, "status": 200, "resp_headers": {}, "request": {
            "method": "GET", "uri": uri, "client_ip": ip, "headers": {"User-Agent": [ua]}}})
    f = CaddyFold()
    assert not isinstance(f(line(100, "/")), HistoryOnly)                       # the view shows
    assert isinstance(f(line(100.5, "/a.css")), HistoryOnly)                    # its assets fold
    assert isinstance(f(line(109, "/b.woff2")), HistoryOnly)                    # window slides while assets arrive
    assert not isinstance(f(line(120, "/c.png")), HistoryOnly)                  # gap too long: shows again
    assert not isinstance(f(line(100.6, "/a.css", ip="8.8.8.8")), HistoryOnly)  # other visitor, no view: shows
    assert not isinstance(f(line(101, "/a.css", ua="curl/8")), HistoryOnly)     # same IP, other client: shows
    assert f("not json") is None


async def test_follow_command_ingests_then_announces_loss_and_restarts(monkeypatch):
    import tail
    e = RainEngine(mock=False)
    monkeypatch.setattr(tail.asyncio, "sleep", _fast_sleep(tail.asyncio.sleep))
    runs = []

    parse = lambda ln: ("error", "t", ln.strip(), None) if ln.strip() else None
    t = asyncio.create_task(follow_command(e, ["sh", "-c", "echo hello; echo world"], parse, "fake host"))
    for _ in range(100):
        await asyncio.sleep(0.02)
        msgs = [s.message for s in e.streams]
        if "lost fake host, retrying" in msgs and msgs.count("hello") >= 2:
            break
    t.cancel()
    msgs = [s.message for s in e.streams]
    assert msgs[:2] == ["hello", "world"]
    assert "lost fake host, retrying" in msgs
    assert msgs.count("hello") >= 2                    # it restarted


async def test_follow_command_records_history_only_records_without_displaying(monkeypatch):
    e = RainEngine(mock=False)
    seen = []
    e.record = lambda *a, **kw: seen.append(a + (kw.get("quiet"),))
    parse = lambda ln: HistoryOnly(("info", "t", ln.strip(), None)) if ln.strip() else None
    t = asyncio.create_task(follow_command(e, ["sh", "-c", "echo quiet; sleep 5"], parse, "h"))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if seen:
            break
    t.cancel()
    assert seen == [("info", "t", "quiet", None, True)] and not any(s.message == "quiet" for s in e.streams)


async def test_follow_command_survives_missing_binary(monkeypatch):
    import tail
    e = RainEngine(mock=False)
    monkeypatch.setattr(tail.asyncio, "sleep", _fast_sleep(tail.asyncio.sleep))
    t = asyncio.create_task(follow_command(e, ["definitely-not-a-command-xyz"], lambda l: None, "ghost"))
    await asyncio.sleep(0.2)
    t.cancel()
    assert [s.message for s in e.streams][:1] == ["lost ghost, retrying"]


def _fast_sleep(real):
    async def sleep(d):
        await real(min(d, 0.02))
    return sleep


async def test_follow_command_terminates_its_child_when_cancelled():
    import os
    import tail
    pids = []
    real = asyncio.create_subprocess_exec

    async def spy(*a, **kw):
        p = await real(*a, **kw)
        pids.append(p.pid)
        return p

    e = RainEngine(mock=False)
    orig = tail.asyncio.create_subprocess_exec
    tail.asyncio.create_subprocess_exec = spy
    try:
        t = asyncio.create_task(follow_command(e, ["sleep", "60"], lambda l: None, "sleeper"))
        await asyncio.sleep(0.3)
        assert pids and _alive(pids[0])
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    finally:
        tail.asyncio.create_subprocess_exec = orig
    for _ in range(20):
        if not _alive(pids[0]):
            break
        await asyncio.sleep(0.1)
    assert not _alive(pids[0]), "child outlived the cancelled follower"


def _alive(pid: int) -> bool:
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"       # a zombie is not alive
    except FileNotFoundError:
        return False


def test_caddy_own_log_becomes_a_short_line_not_raw_json():
    from tail import caddy_service
    raw = ('{"level":"info","ts":1793366272.66,"logger":"tls.renew","msg":"renewing certificate",'
           '"identifier":"sc.example.com","remaining":2597511.3}')
    assert caddy_service(raw) == ("info", "tls.renew: renewing certificate · sc.example.com")
    warn = ('{"level":"warn","logger":"http.handlers.reverse_proxy","msg":"aborting with incomplete response",'
            '"upstream":"localhost:3000","request":{"remote_ip":"1.2.3.4","client_ip":"1.2.3.4","method":"GET","host":"a.example","uri":"/x"}}')
    level, line = caddy_service(warn)
    assert level == "warn" and line == "reverse_proxy: aborting with incomplete response · GET a.example/x from 1.2.3.4"
    assert caddy_service("Safari viewed / from 1.2.3.4") is None and caddy_service('{"no":"msg"}') is None


def test_journal_record_from_caddy_is_rendered():
    rec = {"MESSAGE": '{"level":"error","logger":"tls","msg":"boom","error":"x failed"}', "SYSLOG_IDENTIFIER": "caddy", "PRIORITY": "6"}
    assert parse_journal(json.dumps(rec)) == ("error", "caddy", "tls: boom · x failed", None)


def test_caddy_access_line_arriving_via_the_journal_is_a_sentence():
    access = {"level": "info", "ts": 1793411226.4, "logger": "http.log.access", "msg": "handled request", "status": 404,
              "request": {"remote_ip": "1.2.3.4", "client_ip": "1.2.3.4", "method": "GET", "host": "x.example", "uri": "/.env",
                          "headers": {"User-Agent": ["curl/8.0"]}}}
    rec = {"MESSAGE": json.dumps(access), "SYSLOG_IDENTIFIER": "caddy", "PRIORITY": "6"}
    level, source, msg, _ = parse_journal(json.dumps(rec))
    assert (level, source) == ("warn", "caddy") and msg.endswith("probed /.env (404) from 1.2.3.4") and "{" not in msg




def _journal_line(i, cursor, age=3600):
    import time as _t
    return json.dumps({"MESSAGE": f"m{i}", "PRIORITY": "4", "SYSLOG_IDENTIFIER": "x", "__CURSOR": cursor,
                       "__REALTIME_TIMESTAMP": str(int((_t.time() - age) * 1e6))})


async def test_journal_sync_records_old_entries_shows_fresh_ones_and_stores_the_cursor(monkeypatch):
    import tail
    from history import History
    e = RainEngine(mock=False, history=History(":memory:"))
    old, fresh = _journal_line(1, "c1"), _journal_line(2, "c2", age=1)
    script = f"printf '%s\\n%s\\n' '{old}' '{fresh}'"
    monkeypatch.setattr(tail.asyncio, "sleep", _fast_sleep(tail.asyncio.sleep))
    t = asyncio.create_task(tail.follow_command(e, ["sh", "-c", script], parse_journal, "h",
                                                key="k", cursor_of=tail.journal_cursor))
    await asyncio.sleep(0.3)
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    await asyncio.sleep(0.2)                           # let the shielded child reap finish
    assert e.history.get_cursor("k") == "c2"
    assert e.history.count() >= 2
    msgs = [s.message for s in e.streams]
    assert "m2" in msgs and "m1" not in msgs          # the night's backlog is history, not rain


async def test_follow_command_passes_the_stored_cursor_to_each_restart(monkeypatch):
    import tail
    from history import History
    e = RainEngine(mock=False, history=History(":memory:"))
    e.history.set_cursor("k", "start")
    seen = []

    def build(cur):
        seen.append(cur)
        return ["sh", "-c", f"echo '{_journal_line(len(seen), f'c{len(seen)}')}'"]
    monkeypatch.setattr(tail.asyncio, "sleep", _fast_sleep(tail.asyncio.sleep))
    t = asyncio.create_task(tail.follow_command(e, build, parse_journal, "h", key="k", cursor_of=tail.journal_cursor))
    for _ in range(200):
        if len(seen) >= 3:
            break
        await asyncio.sleep(0.01)
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    await asyncio.sleep(0.2)                           # let the shielded child reap finish
    assert seen[:3] == ["start", "c1", "c2"]


async def test_a_cursor_the_journal_rejects_is_dropped(monkeypatch):
    import tail
    from history import History
    e = RainEngine(mock=False, history=History(":memory:"))
    e.history.set_cursor("k", "gone")
    monkeypatch.setattr(tail.asyncio, "sleep", _fast_sleep(tail.asyncio.sleep))
    t = asyncio.create_task(tail.follow_command(e, ["sh", "-c", "exit 1"], parse_journal, "h",
                                                key="k", cursor_of=tail.journal_cursor))
    await asyncio.sleep(0.3)
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    await asyncio.sleep(0.2)                           # let the shielded child reap finish
    assert e.history.get_cursor("k") is None


async def test_time_resume_skips_what_history_already_has(monkeypatch):
    import time as _t
    import tail
    from history import History
    e = RainEngine(mock=False, history=History(":memory:"))
    now = _t.time()
    e.history.set_cursor("k", repr(now - 5000))
    recs = {"a": ("info", "x", "a", now - 9000), "b": ("info", "x", "b", now - 4000)}
    monkeypatch.setattr(tail.asyncio, "sleep", _fast_sleep(tail.asyncio.sleep))
    t = asyncio.create_task(tail.follow_command(e, ["sh", "-c", "echo a; echo b; sleep 5"], lambda l: recs[l.strip()],
                                                "h", key="k", cursor_of=tail.time_cursor, resume="time"))
    await asyncio.sleep(0.3)
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    await asyncio.sleep(0.2)                           # let the shielded child reap finish
    assert [r[3] for r in e.history.between(0, now + 1)] == ["b"]
    assert float(e.history.get_cursor("k")) == now - 4000


def test_fail2ban_gets_real_epochs_once_the_server_offset_is_known():
    import calendar
    import tail
    f = tail.Fail2ban()
    line = "2026-11-01 00:12:33,809 fail2ban.filter [1]: INFO    [sshd] Found 213.177.179.52 - 2026-11-01 00:12:33"
    assert f(line)[3] is None and not f.timed
    f.offset = -6 * 3600                       # server at UTC-6
    assert f.timed
    assert f(line)[3] == calendar.timegm((2026, 11, 1, 0, 12, 33)) + 6 * 3600


def test_ssh_shares_one_connection_per_host():
    import tail
    argv = tail.ssh_argv("myserver", "true")
    assert "ControlMaster=auto" in argv and any(a.startswith("ControlPath=") and "%C" in a for a in argv)
