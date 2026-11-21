"""Real-time recordings against a real browser (#701): length, content at a time, sign-in.

These run the real recorder and encoder on a scripted page and decode the video. They are
skipped where no Chromium, Chrome or Edge is installed, or no ffmpeg (the hermetic CI has
none of them); `test_browser_recording_unit.py` covers the same logic with fakes.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from blumkin.browser.cdp import PlaywrightDriver
from blumkin.browser.recording import encode_recording, read_index, run_recorder

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are needed to check the video",
)

_APP = """<!doctype html><title>secret title</title>
<body style="margin:0;background:#ffffff;font:60px sans-serif"><div id=c>x</div>"""
_LOGIN = "<!doctype html><body style='margin:0;background:#cc0000;color:#fff'>PASSWORD PLEASE"
_HOSTS = ("app.example.com",)
_SIGN_IN = ("login.microsoftonline.com",)


@pytest.fixture(scope="module")
def _browser() -> Any:
    sync_api = pytest.importorskip("playwright.sync_api")
    manager = sync_api.sync_playwright().start()
    browser = None
    for options in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
        try:
            browser = manager.chromium.launch(headless=True, **options)
            break
        except Exception:  # not installed here: try the next one
            continue
    if browser is None:
        manager.stop()
        pytest.skip("no Chromium, Chrome or Edge is available to run the recorder in")
    yield browser, manager
    browser.close()
    manager.stop()


_CONTEXTS: list[Any] = []


@pytest.fixture(autouse=True)
def _close_contexts() -> Any:
    """Every test's browser context goes with the test, not with the module."""
    yield
    while _CONTEXTS:
        try:
            _CONTEXTS.pop().close()
        except Exception:  # noqa: BLE001 - the browser may already be gone
            pass


def _scripted_page(real: Any, scale: float = 1.0) -> tuple[PlaywrightDriver, Any]:
    browser, manager = real
    context = browser.new_context(viewport={"width": 800, "height": 450}, device_scale_factor=scale)
    _CONTEXTS.append(context)
    page = context.new_page()
    page.route(
        "https://app.example.com/**", lambda r: r.fulfill(body=_APP, content_type="text/html")
    )
    page.route(
        "https://login.microsoftonline.com/**",
        lambda r: r.fulfill(body=_LOGIN, content_type="text/html"),
    )
    page.goto("https://app.example.com/")
    return PlaywrightDriver(browser=browser, page=page, playwright=manager, timeout_s=5.0), page


def _record(
    real: Any, tmp_path: Path, script: list[tuple[float, str]], seconds: float, scale: float = 1.0
) -> tuple[Path, float, Any]:
    """Run the recorder for `seconds`, applying `script` (time, action) as the clock reaches it.

    An action is a page address to go to, or `color:#rrggbb` to paint the page.
    """
    driver, page = _scripted_page(real, scale)
    session = tmp_path / "session"
    session.mkdir()
    pending = list(script)
    began = time.monotonic()  # steady: the system clock can step during a run

    def stop() -> bool:
        elapsed = time.monotonic() - began
        while pending and elapsed >= pending[0][0]:
            _, action = pending.pop(0)
            if action.startswith("color:"):
                page.evaluate(f"document.body.style.background = '{action[6:]}'")
            else:
                page.goto(action)
        return elapsed >= seconds

    began_steady = time.monotonic()
    began_wall = time.time()
    ready_at: list[float] = []  # when the first picture was in (this harness's own clock)
    why = run_recorder(
        driver,
        session,
        hosts=_HOSTS,
        sign_in_hosts=_SIGN_IN,
        stop=stop,
        on_ready=lambda: ready_at.append(time.monotonic()),
    )
    stepped = (time.time() - began_wall) - (time.monotonic() - began_steady)
    if abs(stepped) > 0.5:
        # Browser paint times are wall-clock times: a clock that stepped during the run (a time
        # sync, a long sleep) puts them out of step with the rest, and what is measured here
        # is then the clock, not the recorder.
        pytest.skip(f"the system clock stepped by {stepped:.1f} s during the run")
    assert why == "stopped", why  # a recording cut short would only match its own shorter journal
    elapsed = time.monotonic() - began_steady  # a clock the journal's own times do not feed
    # The video begins at the first picture (the recorder lets the page settle first), so its
    # real length is counted from there, not from the moment the recorder was asked to start.
    entries, ended = read_index(session)
    span = ended - entries[0].t
    # The journal's span must agree with an independent clock: the run, less the time the page
    # took to give its first picture (measured here, so a slow or busy machine does not fail it).
    # A recorder that stopped early would write a short span the encoder then matched.
    startup = ready_at[0] - began_steady
    assert elapsed - startup - 1.0 <= span <= elapsed - startup + 1.0, (span, elapsed, startup)
    assert elapsed >= seconds - 0.3, (elapsed, seconds)
    return session, span, page


def _probe(path: Path) -> dict[str, Any]:
    done = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)


def _color_at(video: Path, at: float, tmp_path: Path) -> tuple[int, int, int]:
    from PIL import Image

    image = tmp_path / f"at-{at}.png"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{at}", "-i", str(video),
         "-frames:v", "1", str(image)],
        check=True,
    )  # fmt: skip
    with Image.open(image) as shot:
        rgb = shot.convert("RGB")
        return rgb.getpixel((rgb.width // 8, rgb.height // 8))  # type: ignore[return-value]


def _near(color: tuple[int, int, int], want: tuple[int, int, int], slack: int = 40) -> bool:
    return all(abs(got - expected) <= slack for got, expected in zip(color, want, strict=True))


def test_the_video_is_as_long_as_the_session_and_shows_the_page_at_each_time(
    _browser: Any, tmp_path: Path
) -> None:
    script = [(2.0, "color:#0000ff"), (5.0, "color:#00aa00")]
    session, wall, _ = _record(_browser, tmp_path, script, seconds=8.0)
    video = tmp_path / "out.mp4"

    written = encode_recording(session, video)

    probe = _probe(video)
    length = float(probe["format"]["duration"])
    assert abs(length - wall) <= wall * 0.05, (length, wall)
    assert abs(written["seconds"] - wall) <= wall * 0.05
    assert _near(_color_at(video, 1.0, tmp_path), (255, 255, 255))
    assert _near(_color_at(video, 3.5, tmp_path), (0, 0, 255))
    assert _near(_color_at(video, 6.5, tmp_path), (0, 170, 0))
    stream = next(item for item in probe["streams"] if item["codec_type"] == "video")
    assert stream["codec_name"] == "h264" and stream["pix_fmt"] == "yuv420p"
    assert stream["r_frame_rate"] == "30/1"


def test_a_stretch_on_a_sign_in_page_keeps_its_length_and_shows_no_sign_in_pixels(
    _browser: Any, tmp_path: Path
) -> None:
    script = [(2.0, "https://login.microsoftonline.com/x"), (5.0, "https://app.example.com/")]
    session, wall, _ = _record(_browser, tmp_path, script, seconds=7.0)
    video = tmp_path / "out.mp4"

    written = encode_recording(session, video)

    assert written["cards"] == 1
    assert abs(float(_probe(video)["format"]["duration"]) - wall) <= wall * 0.05
    # the middle of the sign-in stretch is the grey card, not the page's red
    assert not _near(_color_at(video, 3.5, tmp_path), (204, 0, 0), slack=60)
    assert _near(_color_at(video, 3.5, tmp_path), (32, 36, 44), slack=20)
    # and no frame on disk holds a pixel of the sign-in page
    from PIL import Image

    for frame in sorted((session / "frames").glob("*.jpg")):
        with Image.open(frame) as shot:
            red, green, blue = shot.convert("RGB").getpixel((shot.width // 8, shot.height // 8))  # type: ignore[misc]
        assert not (red > 150 and green < 60 and blue < 60), frame.name


def test_the_files_are_private_and_the_video_carries_no_page_details(
    _browser: Any, tmp_path: Path
) -> None:
    session, _, _ = _record(_browser, tmp_path, [], seconds=2.0)
    video = tmp_path / "out.mp4"
    encode_recording(session, video)

    assert stat.S_IMODE(video.stat().st_mode) == 0o600
    for item in (session / "index.jsonl", *(session / "frames").glob("*.jpg")):
        assert stat.S_IMODE(item.stat().st_mode) == 0o600
    tags = _probe(video)["format"].get("tags", {})
    assert not any(
        "secret" in str(value).lower() or "example" in str(value) for value in tags.values()
    )
    assert b"secret title" not in video.read_bytes()
    assert b"app.example.com" not in video.read_bytes()


def test_the_page_is_never_resized_while_it_is_recorded(_browser: Any, tmp_path: Path) -> None:
    session, _, page = _record(_browser, tmp_path, [(1.0, "color:#112233")], seconds=2.5)

    assert page.viewport_size == {"width": 800, "height": 450}
    assert len(list((session / "frames").glob("*.jpg"))) >= 2


def test_an_idle_page_is_held_for_the_whole_time_it_was_idle(_browser: Any, tmp_path: Path) -> None:
    session, wall, _ = _record(_browser, tmp_path, [], seconds=4.0)  # nothing changes
    video = tmp_path / "out.mp4"

    encode_recording(session, video)

    assert abs(float(_probe(video)["format"]["duration"]) - wall) <= wall * 0.05


def test_speed_and_idle_trimming_shorten_the_video_without_changing_the_actions(
    _browser: Any, tmp_path: Path
) -> None:
    session, wall, _ = _record(_browser, tmp_path, [(3.0, "color:#0000ff")], seconds=6.0)

    fast = encode_recording(session, tmp_path / "fast.mp4", speed=2.0)
    trimmed = encode_recording(session, tmp_path / "trim.mp4", trim_idle_s=1.0)

    assert abs(fast["seconds"] - wall / 2) <= wall * 0.05
    assert trimmed["seconds"] < wall - 2.0


def test_a_high_density_display_does_not_cost_the_video_any_of_its_length(
    _browser: Any, tmp_path: Path
) -> None:
    """The start keyframe is taken at the display's own pixels, the screencast frames are
    scaled by the browser: the sizes differ, and ffmpeg once dropped the frame it was holding."""
    session, wall, _ = _record(_browser, tmp_path, [(3.0, "color:#0000ff")], seconds=6.0, scale=2.0)
    video = tmp_path / "out.mp4"

    written = encode_recording(session, video)

    assert abs(float(_probe(video)["format"]["duration"]) - wall) <= wall * 0.05
    assert abs(written["seconds"] - wall) <= wall * 0.05
    assert _near(_color_at(video, 1.5, tmp_path), (255, 255, 255))
    assert _near(_color_at(video, 4.5, tmp_path), (0, 0, 255))
