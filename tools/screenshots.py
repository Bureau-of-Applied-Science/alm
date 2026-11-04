#!/usr/bin/env python3
"""Regenerate the README media (docs/img/) from the demo data, so nothing real can appear in them.

    .venv/bin/python tools/screenshots.py [--out docs/img] [--only hero,detail,...]

Starts `main.py --demo` (invented data, in memory) on a free port, drives it with headless Chromium
(Playwright), and writes stills and GIFs. Every capture first checks the page text: any IP address outside the
documentation ranges (RFC 5737, RFC 3849), or any word on the denylist, aborts the run, so the images are
sanitized by construction. Needs `./install.sh --dev` and `playwright install chromium`. Takes about three minutes
(the replay is captured in real time).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DOC_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")]
# generic leaks always abort; add your own host names and user names (comma separated) in ALM_MEDIA_DENY
DENY = re.compile("|".join([r"/home/", r"/Users/", r"ubuntu@"] +
                           [re.escape(w.strip()) for w in os.environ.get("ALM_MEDIA_DENY", "").split(",") if w.strip()]), re.I)
IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\b[0-9a-f]{1,4}(?::[0-9a-f]{0,4}){2,7}\b", re.I)
W, H = 1280, 720


def shown(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sanitize_text(text: str, where: str) -> None:
    for m in IP_RE.findall(text):
        try:
            ip = ipaddress.ip_address(m)
        except ValueError:
            continue
        if not any(ip in n for n in DOC_NETS):
            sys.exit(f"sanitize: {m!r} in {where} is not a documentation address")
    d = DENY.search(text)
    if d:
        sys.exit(f"sanitize: {d.group()!r} found in {where}")


async def page_text(page) -> str:
    return await page.evaluate("document.body.innerText + ' ' + (document.getElementById('detailPanel')||{}).innerText")


def quantize(img: Image.Image, colors: int = 96) -> Image.Image:
    return img.convert("RGB").quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)


def save_png(img: Image.Image, path: Path, width: int | None = None) -> None:
    if width and img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    img.convert("RGB").quantize(colors=200, method=Image.Quantize.MEDIANCUT).save(path, optimize=True)
    print(f"  {shown(path)}  {path.stat().st_size // 1024} KB  {img.width}x{img.height}")


def save_gif(frames: list[tuple[float, Image.Image]], path: Path, width: int, max_fps: float = 12,
             colors: int = 96, loop_pause: float = 1.2) -> None:
    """frames: (timestamp, image). Drops frames closer than 1/max_fps, scales to `width`, keeps real timing."""
    kept: list[tuple[float, Image.Image]] = []
    for t, im in frames:
        if not kept or t - kept[-1][0] >= 1 / max_fps:
            kept.append((t, im))
    imgs, durs = [], []
    for i, (t, im) in enumerate(kept):
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        imgs.append(quantize(im, colors))
        nxt = kept[i + 1][0] if i + 1 < len(kept) else t + loop_pause
        durs.append(max(20, int((nxt - t) * 1000)))
    imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=durs, loop=0, optimize=True, disposal=1)
    print(f"  {shown(path)}  {path.stat().st_size // 1024} KB  {len(imgs)} frames  {sum(durs) / 1000:.1f}s")


class Recorder:
    """Chromium screencast over CDP: JPEG frames with real timestamps whenever the page changes."""

    def __init__(self, page, ctx, scale: int = 1):
        self.page, self.ctx, self.frames, self.scale = page, ctx, [], scale
        self.cdp = None

    async def start(self):
        self.cdp = await self.ctx.new_cdp_session(self.page)

        async def on_frame(p):
            self.frames.append((p["metadata"]["timestamp"], base64.b64decode(p["data"])))
            await self.cdp.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]})
        self.cdp.on("Page.screencastFrame", lambda p: asyncio.create_task(on_frame(p)))
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 88, "maxWidth": W * self.scale, "maxHeight": H * self.scale,
                                                      "everyNthFrame": 1})

    async def stop(self, absolute: bool = False) -> list[tuple[float, Image.Image]]:
        await self.cdp.send("Page.stopScreencast")
        await asyncio.sleep(0.3)
        t0 = 0.0 if absolute else self.frames[0][0]
        return [(t - t0, Image.open(io.BytesIO(b)).convert("RGB")) for t, b in self.frames]


async def wait_for_rain(page, n: int = 12, timeout: float = 90) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if await page.evaluate("document.querySelectorAll('.stream').length") >= n:
            return
        await asyncio.sleep(0.5)
    sys.exit("the rain never filled")


async def grab(page, path: Path, clip: dict | None = None, width: int | None = None, label: str = "") -> Image.Image:
    sanitize_text(await page_text(page), label or path.name)
    png = await page.screenshot(clip=clip) if clip else await page.screenshot()
    img = Image.open(io.BytesIO(png))
    save_png(img, path, width)
    return img


async def box(page, selector: str, pad: int = 12) -> dict:
    b = await page.evaluate(f"(() => {{ const r = document.querySelector({json.dumps(selector)}).getBoundingClientRect();"
                            f" return {{x:r.x, y:r.y, width:r.width, height:r.height}}; }})()")
    x, y = max(0, b["x"] - pad), max(0, b["y"] - pad)
    return {"x": x, "y": y, "width": min(W - x, b["width"] + 2 * pad), "height": min(H - y, b["height"] + 2 * pad)}


async def main_async(out: Path, only: set[str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    port = free_port()
    server = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--demo", "--web-only", "--port", str(port),
                               "--demo-pace", "1.6"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                socket.create_connection(("127.0.0.1", port), 0.3).close()
                break
            except OSError:
                await asyncio.sleep(0.25)
        url = f"http://127.0.0.1:{port}/"
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx = await browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=2)
            page = await ctx.new_page()
            await page.goto(url)
            print("filling the screen with demo traffic ...")
            await wait_for_rain(page)
            await page.wait_for_timeout(25000)

            want = lambda name: not only or name in only

            if want("hero"):
                print("hero")
                await page.mouse.move(W - 4, H - 4)
                await grab(page, out / "hero.png", width=1600, label="hero")
                rec = Recorder(page, ctx)
                await rec.start()
                await asyncio.sleep(7)
                frames = await rec.stop()
                sanitize_text(await page_text(page), "hero gif")
                save_gif(frames, out / "hero.gif", width=900, max_fps=10, colors=80)

            if want("detail"):
                print("detail")
                handle = await page.evaluate_handle("""() => {
                    const els = [...document.querySelectorAll('.stream.lvl-warn')].filter(e => {
                        const r = e.getBoundingClientRect(); return r.left > 120 && r.right < 1050 && r.top > 90 && r.bottom < 640
                          && /probed|Invalid user/.test(e.innerText); });
                    return els[0] || null; }""")
                el = handle.as_element()
                if el is None:
                    sys.exit("no warn stream in view to inspect")
                await el.evaluate("e => e.click()")
                await page.wait_for_selector("#detailPanel:not(.hidden)", timeout=5000)
                await page.wait_for_timeout(900)
                await page.mouse.move(W - 4, H - 4)
                await grab(page, out / "detail.png", width=1600, label="detail view")
                await grab(page, out / "detail-zoom.png", clip=await box(page, "#detailPanel", pad=16), label="detail panel")
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(600)

            if want("replay"):
                print("timeline")
                await page.click("#tlLabel")
                await page.wait_for_timeout(700)
                await page.mouse.move(W - 4, H - 4)
                await grab(page, out / "timeline-zoom.png", clip=await box(page, "#tlControls", pad=18), label="timeline controls")
                print("replay (about a minute, in real time) ...")
                # Two short recordings, not one long one: a screencast of a busy page is hundreds of MB a minute.
                rec = Recorder(page, ctx, scale=1)
                await rec.start()
                await page.click('[data-win="30m"]')
                await page.wait_for_selector("#tlClock:not(.hidden)", timeout=10000)
                await page.wait_for_timeout(9000)
                head = await rec.stop()
                await grab(page, out / "replay.png", width=1600, label="replay")
                clock = await box(page, "#tlClock", pad=5)
                await grab(page, out / "clock-zoom.png", clip=clock, label="replay clock")
                await page.mouse.move(W - 4, H - 4)
                await page.wait_for_function("document.getElementById('lampEnd').classList.contains('on')", timeout=150000, polling=100)
                # the clock lingers until the last replayed drop has drifted off, then glitches away: start
                # recording only when the last couple are crossing, so the capture stays short
                await page.wait_for_function(
                    "[...document.querySelectorAll('.stream')].filter(e => e.innerText.includes('REPLAY')).length <= 2",
                    timeout=120000, polling=200)
                rec = Recorder(page, ctx, scale=2)
                await rec.start()
                await page.wait_for_function("document.getElementById('tlClock').classList.contains('glitch')", timeout=90000, polling=30)
                glitch_at = time.time()
                await page.wait_for_function("document.getElementById('tlClock').classList.contains('hidden')", timeout=10000)
                await page.wait_for_timeout(1500)
                frames = await rec.stop(absolute=True)
                sanitize_text(await page_text(page), "replay gif")
                t_first = frames[0][0]
                # keep only the last stretch before the glitch (the file would otherwise run to tens of MB)
                lo = glitch_at - 6
                tail = [(t - lo + head[-1][0] + 0.25, im) for t, im in frames if t >= lo]
                save_gif(head + tail, out / "replay.gif", width=800, max_fps=8, colors=64)
                # the glitch alone, zoomed on the clock
                sc = frames[0][1].width / W
                crop = (int(clock["x"] * sc), int(clock["y"] * sc), int((clock["x"] + clock["width"]) * sc),
                        int((clock["y"] + clock["height"]) * sc))
                g = [(t - (glitch_at - 1.4), im.crop(crop)) for t, im in frames if glitch_at - 1.4 <= t <= glitch_at + 1.3]
                save_gif(g, out / "glitch.gif", width=560, max_fps=30, colors=64, loop_pause=1.5)
            await browser.close()
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "img"))
    ap.add_argument("--only", default="", help="comma list of: hero, detail, replay")
    a = ap.parse_args()
    asyncio.run(main_async(Path(a.out), {s for s in a.only.split(",") if s}))


if __name__ == "__main__":
    main()
