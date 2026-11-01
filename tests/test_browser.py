"""Real-browser tests against a real server process fed by a real log file."""
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, tmp_path, token=None, port=None, db=None):
        self.port = port or free_port()
        self.log = tmp_path / "alm.log"
        self.log.touch()
        self.token = token
        self.db = db                 # history db path; None = no history (never touch the real one)
        self.proc = None

    def start(self):
        cmd = [sys.executable, "main.py", "--web-only", "--port", str(self.port), "--tail", str(self.log)]
        cmd += ["--db", str(self.db), "--backfill", "0"] if self.db else ["--no-history"]
        if self.token:
            cmd += ["--token", self.token]
        self.proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(5)
            self.proc = None

    def emit(self, level, source, message):
        with open(self.log, "a") as f:
            f.write(f"[00:00:00] {level.upper()} {source} {message}\n")

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"

    def ws(self):
        suffix = f"?token={self.token}" if self.token else ""
        return connect(f"ws://127.0.0.1:{self.port}/ws{suffix}")


@pytest.fixture
def server(tmp_path):
    s = Server(tmp_path).start()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def page_for(browser, url):
    pg = browser.new_page(viewport={"width": 1400, "height": 800})
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.goto(url)
    return pg


def first_spawn_id(ws):
    json.loads(ws.recv())                         # snapshot
    while True:
        ev = json.loads(ws.recv(timeout=10))
        if ev["type"] == "spawn":
            return ev["id"]


def test_caught_on_connect(server, browser):
    with server.ws() as ws:
        time.sleep(0.3)
        server.emit("error", "t", "caught while nobody watched")
        sid = first_spawn_id(ws)
        time.sleep(2)
        ws.send(json.dumps({"action": "catch", "id": sid}))
        time.sleep(0.4)
        pg = page_for(browser, server.url)
        time.sleep(1.2)
        caught = pg.locator(".stream.caught")
        assert caught.count() == 1
        x1 = caught.first.evaluate("e=>e.getBoundingClientRect().left")
        time.sleep(1.5)
        assert caught.first.evaluate("e=>e.getBoundingClientRect().left") == pytest.approx(x1, abs=0.5)
        caught.first.click()                       # already caught: opens detail directly
        assert pg.locator("#detailPanel:not(.hidden)").count() == 1
        pg.mouse.click(5, 5)                        # click outside releases
        time.sleep(0.5)
        assert pg.locator(".stream.caught").count() == 0
        assert pg.errors == []


def test_log_text_is_escaped(server, browser):
    pg = page_for(browser, server.url)
    time.sleep(0.5)
    server.emit("error", "t", "<img src=x onerror=window.__pwned=1> & <b>bold</b>")
    pg.wait_for_selector(".stream")
    time.sleep(0.5)
    assert pg.evaluate("window.__pwned") is None
    assert pg.locator(".stream b").count() == 0
    assert "<img src=x" in pg.locator(".stream .msg").first.inner_text()


def test_normal_mode_still_has_chrome(server, browser):
    pg = page_for(browser, server.url)
    assert pg.locator("header.chrome").is_visible()


def test_reconnect_resnapshots_without_duplicates(tmp_path, browser):
    s = Server(tmp_path).start()
    try:
        pg = page_for(browser, s.url + "?screensaver=1")
        time.sleep(0.5)
        s.emit("error", "t", "before restart")
        pg.wait_for_selector(".stream")
        port = s.port
        s.stop()
        time.sleep(1.0)
        assert pg.locator("#ssOffline").is_visible()
        s2 = Server(tmp_path, port=port)
        s2.log = s.log
        s2.start()
        try:
            time.sleep(0.5)
            s2.emit("error", "t", "after restart")
            pg.wait_for_function(
                "[...document.querySelectorAll('.stream .msg')].some(e => e.textContent === 'after restart')",
                timeout=20000)
            texts = pg.locator(".stream .msg").all_inner_texts()
            assert texts == ["after restart"], texts      # the reconnect snapshot cleared the old stream
            assert not pg.locator("#ssOffline").is_visible()
        finally:
            s2.stop()
    finally:
        s.stop()


def test_token_required(tmp_path, browser):
    s = Server(tmp_path, token="s3cret").start()
    try:
        with pytest.raises(Exception, match="403|rejected"):
            connect(f"ws://127.0.0.1:{s.port}/ws")
        import urllib.request, urllib.error
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{s.port}/api/state")
        assert e.value.code == 401
        assert urllib.request.urlopen(f"http://127.0.0.1:{s.port}/api/state?token=s3cret").status == 200
        pg = page_for(browser, s.url + "?token=s3cret")
        time.sleep(0.5)
        s.emit("error", "t", "with token")
        pg.wait_for_selector(".stream")
    finally:
        s.stop()
