"""The README media pipeline (tools/screenshots.py): the parts that must keep working between runs.

The capture itself needs a browser and four minutes, so it is run by hand; these cover the sanitizer that
guards every capture and the WebP assembly."""
import os
import sys

import pytest

pytest.importorskip("playwright")
from PIL import Image  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import screenshots as shots  # noqa: E402


def test_documentation_addresses_pass_the_sanitizer():
    shots.sanitize_text("block TCP 203.0.113.9 -> :22 and 192.0.2.1 and 198.51.100.200", "x")
    shots.sanitize_text("block TCP 2001:0db8:0100:0000:0000:0000:0000:0662 -> :443", "x")
    shots.sanitize_text("no addresses here, port 8080 and 12:05:29", "x")


@pytest.mark.parametrize("text", [
    "probed from 8.8.8.8",
    "block TCP 66.132.159.123 -> :993",
    "block TCP 2a06:4880:4000:0000:0000:0000:0000:0062 -> :22",
])
def test_a_real_looking_address_aborts_the_run(text):
    with pytest.raises(SystemExit):
        shots.sanitize_text(text, "x")


@pytest.mark.parametrize("word", ["/home/someone/code", "ubuntu@somehost"])
def test_denylisted_words_abort_the_run(word):
    with pytest.raises(SystemExit):
        shots.sanitize_text(f"something {word} something", "x")


def test_animation_keeps_real_timing_and_drops_frames_that_come_too_fast(tmp_path):
    frames = [(i * 0.02, Image.new("RGB", (200, 100), (i * 5 % 255, 40, 40))) for i in range(50)]   # 50 fps in
    out = tmp_path / "x.webp"
    seconds = shots.save_anim(frames, out, width=100, max_fps=10)
    im = Image.open(out)
    assert im.size == (100, 50) and getattr(im, "is_animated", False) and im.n_frames <= 12   # about 1 s at 10 fps
    assert 1.5 <= seconds <= 2.6                                 # real duration (1 s) plus the loop pause


def test_stills_are_webp_and_scaled(tmp_path):
    out = tmp_path / "x.webp"
    shots.save_still(Image.new("RGB", (3200, 1800), (10, 20, 10)), out, width=1600)
    assert Image.open(out).size == (1600, 900) and out.stat().st_size < 50_000
