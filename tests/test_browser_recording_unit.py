"""Real-time recordings (#701): timeline, recorder loop, encoder command, start and stop."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from blumkin.browser import recording
from blumkin.browser.cdp import BrowserActionError, ScreenFrame
from blumkin.browser.narration import NarrationOptions
from blumkin.browser.recording import (
    Entry,
    classify,
    concat_listing,
    durations,
    encode_recording,
    map_time,
    output_times,
    read_index,
    run_recorder,
    srt_text,
)

_HOSTS = ("app.example.com",)
_SIGN_IN = ("login.microsoftonline.com",)
_APP = "https://app.example.com/page"
_LOGIN = "https://login.microsoftonline.com/x"


def _frames(*stamps: float) -> list[ScreenFrame]:
    return [ScreenFrame(data=f"jpeg{stamp}".encode(), timestamp=stamp) for stamp in stamps]


# --------------------------------------------------------------------------- policy


@pytest.mark.parametrize(
    ("url", "want"),
    [
        (_APP, "ok"),
        ("https://APP.example.com./x", "ok"),
        (_LOGIN, "sign-in"),
        ("https://evil.example.net/", "off-hosts"),
        ("file:///etc/hosts", "off-hosts"),
        ("about:blank", "off-hosts"),
        ("https:///nohost", "off-hosts"),
    ],
)
def test_a_page_is_recorded_only_on_an_allowed_host_and_never_on_a_sign_in_page(
    url: str, want: str
) -> None:
    assert classify(url, _HOSTS, _SIGN_IN) == want


def test_with_no_hosts_declared_any_web_page_but_a_sign_in_page_is_recorded() -> None:
    assert classify("https://anywhere.example.org/", (), _SIGN_IN) == "ok"
    assert classify(_LOGIN, (), _SIGN_IN) == "sign-in"


# --------------------------------------------------------------------------- timeline


def _timeline() -> tuple[list[Entry], float]:
    entries = [
        Entry(t=100.0, kind="frame", file="frames/a.jpg"),
        Entry(t=101.0, kind="frame", file="frames/b.jpg"),
        Entry(t=105.0, kind="card", reason="sign-in"),
    ]
    return entries, 105.5


def test_each_frame_is_shown_for_as_long_as_it_was_on_screen_and_the_last_until_the_end() -> None:
    entries, ended = _timeline()

    assert durations(entries, ended) == [1.0, 4.0, 0.5]
    assert sum(durations(entries, ended)) == pytest.approx(5.5)


def test_speed_divides_every_duration_and_trim_idle_caps_one_stretch() -> None:
    entries, ended = _timeline()

    assert durations(entries, ended, speed=2.0) == [0.5, 2.0, 0.25]
    assert durations(entries, ended, trim_idle_s=1.5) == [1.0, 1.5, 0.5]


def test_a_burst_of_frames_in_one_instant_still_has_a_duration() -> None:
    entries = [Entry(t=1.0, kind="frame", file="a"), Entry(t=1.0, kind="frame", file="b")]

    assert min(durations(entries, 1.0)) >= recording.MIN_FRAME_S


def test_output_times_are_the_running_sum_of_the_durations() -> None:
    entries, ended = _timeline()
    shown = durations(entries, ended)

    assert output_times(entries, shown) == [0.0, 1.0, 5.0]


def test_a_wall_clock_moment_maps_into_the_video_through_speed_and_trimming() -> None:
    entries, ended = _timeline()
    shown = durations(entries, ended, trim_idle_s=1.5)

    assert map_time(99.0, entries, shown, 1.0) == 0.0
    assert map_time(100.5, entries, shown, 1.0) == pytest.approx(0.5)
    assert map_time(103.0, entries, shown, 1.0) == pytest.approx(2.5)  # 1.0 + 1.5 cap
    assert map_time(104.9, entries, shown, 1.0) == pytest.approx(2.5)  # clamped into the cap
    assert map_time(105.2, entries, shown, 1.0) == pytest.approx(2.7)


def test_the_concat_listing_gives_each_image_its_duration_and_repeats_the_last_bare() -> None:
    entries, ended = _timeline()
    shown = durations(entries, ended)
    paths = {"frames/a.jpg": Path("/s/a.jpg"), "frames/b.jpg": Path("/s/b it's.jpg")}
    cards = {"sign-in": Path("/s/card.jpg")}

    lines = concat_listing(entries, shown, paths, cards).splitlines()

    assert lines[:2] == ["file '/s/a.jpg'", "duration 1.000000"]
    assert lines[2] == "file '/s/b it'\\''s.jpg'"  # a quote in a path cannot break the list
    assert lines[4:6] == ["file '/s/card.jpg'", "duration 0.500000"]
    assert lines[-1] == "file '/s/card.jpg'" and "duration" not in lines[-1]


def test_captions_are_placed_on_the_videos_timeline_and_capped() -> None:
    entries, ended = _timeline()
    shown = durations(entries, ended)
    captions = [(100.5, 'click "Add"'), (101.0, "fill Project"), (104.0, "read")]

    text = srt_text(captions, entries, shown, 1.0, sum(shown))

    assert '1\n00:00:00,500 --> 00:00:01,000\nclick "Add"' in text
    assert "2\n00:00:01,000 --> 00:00:04,000\nfill Project" in text
    assert "3\n00:00:04,000 --> 00:00:05,500\nread" in text  # ends at the video's end


def test_a_caption_after_the_end_of_the_video_is_dropped() -> None:
    entries, ended = _timeline()
    shown = durations(entries, ended)

    assert srt_text([(500.0, "late")], entries, shown, 1.0, sum(shown)) == ""


# --------------------------------------------------------------------------- the index


def test_the_index_is_read_by_time_and_a_cut_short_line_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "index.jsonl").write_text(
        json.dumps({"kind": "frame", "file": "frames/b.jpg", "t": 2.0})
        + "\n"
        + json.dumps({"kind": "frame", "file": "frames/a.jpg", "t": 1.0})
        + "\n"
        + json.dumps({"kind": "end", "t": 9.0})
        + '\n{"kind": "frame", "fi'
    )

    entries, ended = read_index(tmp_path)

    assert [entry.file for entry in entries] == ["frames/a.jpg", "frames/b.jpg"]
    assert ended == 9.0


def test_a_recording_that_never_ended_ends_at_its_last_entry(tmp_path: Path) -> None:
    (tmp_path / "index.jsonl").write_text(json.dumps({"kind": "frame", "file": "f", "t": 3.0}))

    assert read_index(tmp_path)[1] == 3.0


@pytest.mark.parametrize("body", [None, "", json.dumps({"kind": "end", "t": 1.0})])
def test_an_index_with_no_frame_or_none_at_all_is_an_error(
    tmp_path: Path, body: str | None
) -> None:
    if body is not None:
        (tmp_path / "index.jsonl").write_text(body)

    with pytest.raises(BrowserActionError, match="Expected"):
        read_index(tmp_path)


# --------------------------------------------------------------------------- the recorder


def _jpeg(color: tuple[int, int, int]) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (16, 9), color).save(buffer, format="JPEG")
    return buffer.getvalue()


_APP_PIXELS = _jpeg((255, 255, 255))
_NEXT_PIXELS = _jpeg((255, 255, 0))
_LOGIN_PIXELS = _jpeg((204, 0, 0))


def _shot(data: bytes, stamp: float) -> ScreenFrame:
    return ScreenFrame(data=data, timestamp=stamp)


@dataclass
class _Step:
    """One read of the page: where it was before and after, what it committed, what it sent."""

    before: str = _APP
    after: str = _APP
    committed: tuple[str, ...] = ()
    frames: tuple[ScreenFrame, ...] = ()
    embedded: tuple[str, ...] = ()  # hosts of frames inside the page after the read


class _Driver:
    """A fake driver played from a script: each `screencast_pump(POLL_MS)` is one `_Step`.

    The page is at a step's `before` address until its pump runs and at its `after` address
    from then on; a pump with `SETTLE_MS` is the wait before a keyframe and moves nothing.
    """

    def __init__(self, steps: list[_Step], *, start: str = _APP, fail_after: int | None = None):
        self.steps = list(steps)
        self.start = start
        self.fail_after = fail_after
        self.current: _Step | None = None
        self.pending: _Step | None = self.steps.pop(0) if self.steps else None
        self.committed_now: list[str] = []
        self.inner_now: list[str] = []
        self.pumped = 0
        self.keyframes = 0
        self.keyframe_pixels: list[bytes] = []
        self.started = 0
        self.stopped = 0
        self.settles = 0
        self.keyframe_url_after: str | None = None
        self.keyframe_stamp: Callable[[], float] | None = None  # a keyframe's paint time
        self._ran = False

    @property
    def url(self) -> str:
        if self.pending is not None and not self._ran:
            return self.pending.before
        if self.pending is not None:
            return self.pending.after
        return self.current.after if self.current is not None else self.start

    def frame_hosts(self) -> tuple[str, ...]:
        step = self.pending if self._ran and self.pending is not None else self.current
        return step.embedded if step is not None and self._ran_any() else ()

    def _ran_any(self) -> bool:
        return self._ran or self.current is not None

    def take_committed_urls(self) -> list[str]:
        taken, self.committed_now = self.committed_now, []
        return taken

    def take_committed_frame_hosts(self) -> tuple[str, ...]:
        taken, self.inner_now = self.inner_now, []
        return tuple(taken)

    def screencast_start(self, **kwargs: Any) -> None:
        self.started += 1
        self.kwargs = kwargs

    def screencast_keyframe(self) -> ScreenFrame:
        self.keyframes += 1
        pixels = self.keyframe_pixels.pop(0) if self.keyframe_pixels else _APP_PIXELS
        if self.keyframe_url_after is not None:
            self.start = self.keyframe_url_after
            self.pending = None
            self.current = _Step(before=self.keyframe_url_after, after=self.keyframe_url_after)
        stamp = self.keyframe_stamp() if self.keyframe_stamp else 100.0 + self.keyframes
        return _shot(pixels, stamp)

    def screencast_pump(self, wait_ms: int) -> list[ScreenFrame]:
        if wait_ms == recording.SETTLE_MS:
            self.settles += 1
            return []
        self.pumped += 1
        if self.fail_after is not None and self.pumped > self.fail_after:
            raise BrowserActionError("Expected an open page, got a closed one")
        if self.pending is None:
            return []
        step = self.pending
        if not self._ran:
            self._ran = True  # the page is now at the step's `after` address
            self.committed_now.extend(step.committed)
            return list(step.frames)
        self.current, self.pending = step, None
        self._ran = False
        if self.steps:
            self.pending = self.steps.pop(0)
        return self.screencast_pump(wait_ms)

    def screencast_stop(self) -> None:
        self.stopped += 1


def _stops(*answers: bool) -> Any:
    queue: Iterator[bool] = iter(answers)
    return lambda: next(queue, True)


def _run(
    tmp_path: Path, driver: _Driver, *answers: bool, **kwargs: Any
) -> tuple[str, list[dict[str, Any]]]:
    why = run_recorder(
        driver,  # type: ignore[arg-type]
        tmp_path,
        hosts=_HOSTS,
        sign_in_hosts=_SIGN_IN,
        stop=_stops(*answers),
        **kwargs,
    )
    lines = [json.loads(line) for line in (tmp_path / "index.jsonl").read_text().splitlines()]
    return why, lines


def _stored(tmp_path: Path) -> list[bytes]:
    return [path.read_bytes() for path in sorted((tmp_path / "frames").iterdir())]


def test_the_recorder_keeps_a_first_frame_then_every_frame_the_page_sends(tmp_path: Path) -> None:
    driver = _Driver([_Step(frames=(_shot(_NEXT_PIXELS, 101.0), _shot(_NEXT_PIXELS, 102.5)))])

    why, lines = _run(tmp_path, driver, False, True)

    assert why == "stopped"
    assert [item["kind"] for item in lines] == ["frame", "frame", "frame", "end"]
    assert driver.started == driver.stopped == 1
    assert driver.kwargs["max_width"] == recording.RECORD_MAX_WIDTH
    assert driver.settles == 1  # the first keyframe waited for the page to paint
    assert _stored(tmp_path) == [_APP_PIXELS, _NEXT_PIXELS, _NEXT_PIXELS]


def test_the_recorder_writes_the_capture_clock_anchor_with_its_first_entry(
    tmp_path: Path,
) -> None:
    from blumkin.browser.capture_clock import read_anchor

    driver = _Driver([_Step(frames=(_shot(_NEXT_PIXELS, 101.0),))])
    ticks = iter([10.0, 10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 14.0] + [15.0] * 40)

    _, lines = _run(tmp_path, driver, False, True, tick=lambda: next(ticks))

    anchor = read_anchor(tmp_path)
    assert anchor is not None
    assert anchor[0] == lines[0]["t"]  # the first entry's time on the journal's axis


def test_without_a_step_the_frames_keep_the_browsers_paint_times(tmp_path: Path) -> None:
    steps = [_Step(frames=(_shot(_NEXT_PIXELS, 101.0 + 5 * n),)) for n in range(1, 4)]
    driver = _Driver(steps)

    _, lines = _run(
        tmp_path,
        driver,
        False,
        False,
        False,
        True,
        clock=lambda: 1000.0 + 5.0 * driver.pumped,
        tick=lambda: 50.0 + 5.0 * driver.pumped,
    )

    assert [item["t"] for item in lines if item["kind"] == "frame"] == [101.0, 106.0, 111.0, 116.0]


def test_frames_painted_before_the_keyframe_are_stale_and_not_kept(tmp_path: Path) -> None:
    """Frames queued while the keyframe was being taken may be the page that was just left."""
    driver = _Driver([_Step(frames=(_shot(_LOGIN_PIXELS, 100.5), _shot(_NEXT_PIXELS, 101.5)))])

    _, lines = _run(tmp_path, driver, False, True)

    assert _LOGIN_PIXELS not in _stored(tmp_path)  # stamped before the keyframe (101.0)
    assert [item["kind"] for item in lines] == ["frame", "frame", "end"]


def test_a_sign_in_stretch_becomes_one_card_and_none_of_its_pixels_are_stored(
    tmp_path: Path,
) -> None:
    driver = _Driver(
        [
            _Step(frames=(_shot(_NEXT_PIXELS, 102.0),)),
            _Step(
                before=_APP,
                after=_LOGIN,
                committed=(_LOGIN,),
                frames=(_shot(_LOGIN_PIXELS, 103.0),),
            ),
            _Step(before=_LOGIN, after=_LOGIN, frames=(_shot(_LOGIN_PIXELS, 104.0),)),
        ]
    )

    _, lines = _run(tmp_path, driver, False, False, False, True)

    kinds = [item["kind"] for item in lines]
    assert kinds.count("card") == 1
    assert {item["reason"] for item in lines if item["kind"] == "card"} == {"sign-in"}
    assert _LOGIN_PIXELS not in _stored(tmp_path)


def test_app_then_sign_in_then_app_within_one_read_keeps_nothing_of_that_read(
    tmp_path: Path,
) -> None:
    """The address is the app's at both ends, but the page went through sign-in meanwhile."""
    driver = _Driver(
        [
            _Step(
                before=_APP,
                after=_APP,
                committed=(_LOGIN, _APP),
                frames=(_shot(_LOGIN_PIXELS, 102.0),),
            )
        ]
    )

    _, lines = _run(tmp_path, driver, False, True)

    assert _LOGIN_PIXELS not in _stored(tmp_path)
    assert any(item["kind"] == "card" and item["reason"] == "sign-in" for item in lines)


def test_an_ordinary_navigation_between_recordable_pages_keeps_its_frames(tmp_path: Path) -> None:
    next_page = "https://app.example.com/next"
    driver = _Driver(
        [
            _Step(
                before=_APP,
                after=next_page,
                committed=(next_page,),
                frames=(_shot(_NEXT_PIXELS, 102.0),),
            )
        ]
    )

    _, lines = _run(tmp_path, driver, False, True)

    assert _NEXT_PIXELS in _stored(tmp_path)
    assert not any(item["kind"] == "card" for item in lines)


def test_a_sign_in_frame_embedded_in_an_allowed_page_is_sign_in_content(tmp_path: Path) -> None:
    driver = _Driver(
        [_Step(embedded=("login.microsoftonline.com",), frames=(_shot(_LOGIN_PIXELS, 102.0),))]
    )

    _, lines = _run(tmp_path, driver, False, True)

    assert _LOGIN_PIXELS not in _stored(tmp_path)
    assert any(item["kind"] == "card" and item["reason"] == "sign-in" for item in lines)


def test_a_frame_batch_is_dropped_when_the_page_changed_while_it_was_read(tmp_path: Path) -> None:
    """Recordable at the start of the read but not at the end: nothing of it is kept."""
    driver = _Driver(
        [
            _Step(
                before=_APP,
                after=_LOGIN,
                committed=(_LOGIN,),
                frames=(_shot(_LOGIN_PIXELS, 102.0),),
            )
        ]
    )

    _, lines = _run(tmp_path, driver, False, True)

    assert [item["kind"] for item in lines] == ["frame", "card", "end"]
    assert _LOGIN_PIXELS not in _stored(tmp_path)


def test_a_batch_read_while_the_page_left_a_sign_in_page_is_dropped_too(tmp_path: Path) -> None:
    """Not recordable at the start of the read but recordable at the end: its frames may be the
    sign-in page's, so none are kept and a fresh frame is taken once the page has settled."""
    driver = _Driver(
        [
            _Step(
                before=_LOGIN, after=_APP, committed=(_APP,), frames=(_shot(_LOGIN_PIXELS, 102.0),)
            ),
            _Step(),
        ],
        start=_LOGIN,
    )

    _, lines = _run(tmp_path, driver, False, False, True)

    assert _LOGIN_PIXELS not in _stored(tmp_path)
    assert [item["kind"] for item in lines][0] == "card"
    assert driver.keyframes >= 1


def test_a_page_that_is_unsteady_at_the_start_gets_a_card_not_a_keyframe(tmp_path: Path) -> None:
    """The page went somewhere recordable-looking but committed a sign-in page on the way."""
    driver = _Driver([_Step()])
    driver.committed_now = [_LOGIN]  # committed while the settle wait ran

    original = driver.screencast_pump

    def pump(wait_ms: int) -> list[ScreenFrame]:
        if wait_ms == recording.SETTLE_MS:
            driver.committed_now.append(_LOGIN)
        return original(wait_ms)

    driver.screencast_pump = pump  # type: ignore[method-assign]

    _, lines = _run(tmp_path, driver, True)

    assert lines[0] == {"kind": "card", "reason": "sign-in", "t": lines[0]["t"]}
    assert driver.keyframes == 0


def test_a_keyframe_that_does_not_decode_is_not_written(tmp_path: Path) -> None:
    driver = _Driver([_Step()])
    driver.keyframe_pixels = [b"", b"not a jpeg"]

    _, lines = _run(tmp_path, driver, False, True)

    assert _stored(tmp_path) == []
    assert lines[0]["kind"] == "card" and lines[0]["reason"] == "unstable"


def test_a_page_off_the_tasks_hosts_gets_its_own_card_reason(tmp_path: Path) -> None:
    driver = _Driver([], start="https://evil.example.net/")

    _, lines = _run(tmp_path, driver, True)

    assert lines[0] == {"kind": "card", "reason": "off-hosts", "t": lines[0]["t"]}


def test_the_recording_resumes_with_a_fresh_frame_after_a_left_out_stretch(tmp_path: Path) -> None:
    driver = _Driver(
        [
            _Step(before=_LOGIN, after=_LOGIN),
            _Step(before=_LOGIN, after=_APP, committed=(_APP,)),
            _Step(),
        ],
        start=_LOGIN,
    )

    _, lines = _run(tmp_path, driver, False, False, False, True)

    kinds = [item["kind"] for item in lines]
    assert kinds[0] == "card" and "frame" in kinds
    assert driver.keyframes == 1  # an idle page sends none, so one is taken


def test_a_closed_browser_ends_the_recording_with_what_it_has(tmp_path: Path) -> None:
    driver = _Driver([_Step(frames=(_shot(_NEXT_PIXELS, 102.0),))], fail_after=1)

    why, lines = _run(tmp_path, driver, False, False, False)

    assert why == "browser closed"
    assert lines[-1]["kind"] == "end" and driver.stopped == 1
    assert read_index(tmp_path)[0]  # still readable


def test_a_forgotten_recording_stops_at_the_time_limit(tmp_path: Path) -> None:
    ticks = iter(range(0, 10_000))
    driver = _Driver([])

    why, _ = _run(
        tmp_path, driver, *([False] * 50), max_seconds=5.0, tick=lambda: float(next(ticks))
    )

    assert why == "time limit"


def test_the_index_and_frames_are_owner_only(tmp_path: Path) -> None:
    driver = _Driver([_Step(frames=(_shot(_NEXT_PIXELS, 102.0),))])
    _run(tmp_path, driver, False, True)

    for path in (tmp_path / "index.jsonl", *(tmp_path / "frames").iterdir()):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "frames").stat().st_mode) == 0o700


# --------------------------------------------------------------------------- the encoder


def _session(tmp_path: Path, *, frames: int = 2) -> Path:
    from PIL import Image

    session = tmp_path / "session"
    (session / "frames").mkdir(parents=True)
    lines = []
    for number in range(frames):
        name = f"frames/f{number + 1:06d}.jpg"
        Image.new("RGB", (64, 36), (10 * number, 20, 30)).save(session / name, format="JPEG")
        lines.append({"file": name, "kind": "frame", "t": 100.0 + number})
    lines.append({"kind": "end", "t": 100.0 + frames})
    (session / "index.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return session


def test_the_mp4_command_cuts_the_output_to_the_real_length_and_strips_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    seen: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        seen.append(command)
        Path(command[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(recording.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(recording.subprocess, "run", fake_run)

    written = encode_recording(session, tmp_path / "out.mp4")

    command = seen[0]
    assert command[command.index("-t") + 1] == "2.000"
    assert command[command.index("-map_metadata") + 1] == "-1"
    assert command[command.index("-f") + 1] == "concat"
    assert "+faststart" in command and "libx264" in command
    vf = command[command.index("-vf") + 1]
    assert f"fps={recording.RECORD_FPS}" in vf and "format=yuv420p" in vf
    assert "in_range=full:out_range=limited" in vf
    assert written["seconds"] == 2.0 and written["frames"] == 2
    assert stat.S_IMODE((tmp_path / "out.mp4").stat().st_mode) == 0o600


def test_captions_become_a_soft_subtitle_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    (session / "captions.jsonl").write_text(json.dumps({"t": 100.5, "text": 'click "Add"'}) + "\n")
    seen: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        seen.append(command)
        Path(command[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(recording.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(recording.subprocess, "run", fake_run)

    written = encode_recording(session, tmp_path / "out.mp4")

    command = seen[0]
    assert "mov_text" in command and written["captions"] == 1
    assert 'click "Add"' in (session / "captions.srt").read_text()


def test_an_mp4_without_ffmpeg_is_refused_and_says_to_use_a_gif(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    monkeypatch.setattr(recording.shutil, "which", lambda name: None)

    with pytest.raises(ValueError, match="ffmpeg.*--format gif"):
        encode_recording(session, tmp_path / "out.mp4")
    assert not (tmp_path / "out.mp4").exists()


def test_a_failed_ffmpeg_leaves_no_half_written_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        Path(command[-1]).write_bytes(b"half")
        return SimpleNamespace(returncode=1, stderr="boom")

    monkeypatch.setattr(recording.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(recording.subprocess, "run", fake_run)

    with pytest.raises(BrowserActionError, match="boom"):
        encode_recording(session, tmp_path / "out.mp4")
    assert not (tmp_path / "out.mp4").exists()


def test_a_gif_gives_each_frame_its_own_duration(tmp_path: Path) -> None:
    from PIL import Image

    session = _session(tmp_path, frames=3)

    written = encode_recording(session, tmp_path / "out.gif", fmt="gif")

    with Image.open(tmp_path / "out.gif") as gif:
        durations_ms = []
        for number in range(int(getattr(gif, "n_frames", 1))):
            gif.seek(number)
            durations_ms.append(gif.info["duration"])
    assert durations_ms == [1000, 1000, 1000] and written["format"] == "gif"
    assert stat.S_IMODE((tmp_path / "out.gif").stat().st_mode) == 0o600


def test_a_gif_with_too_many_frames_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, frames=3)
    monkeypatch.setattr(recording, "GIF_MAX_FRAMES", 2)

    with pytest.raises(ValueError, match="at most 2 frames.*mp4"):
        encode_recording(session, tmp_path / "out.gif", fmt="gif")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"fmt": "avi"}, "format"), ({"speed": 0.0}, "speed"), ({"trim_idle_s": 0.0}, "idle limit")],
)
def test_encoding_options_are_checked_before_anything_is_written(
    tmp_path: Path, kwargs: dict[str, Any], message: str
) -> None:
    session = _session(tmp_path)

    with pytest.raises(ValueError, match=message):
        encode_recording(session, tmp_path / "out.mp4", **kwargs)
    assert not (tmp_path / "out.mp4").exists()


def test_the_cards_are_jpegs_like_the_frames(tmp_path: Path) -> None:
    """The concat demuxer decodes every file with the first one's codec: a PNG would vanish."""
    session = _session(tmp_path)
    with (session / "index.jsonl").open("a") as handle:
        handle.write(json.dumps({"kind": "card", "reason": "sign-in", "t": 100.5}) + "\n")

    entries, _ = read_index(session)
    cards = recording._write_cards(session, entries, 64, 36)

    assert cards["sign-in"].suffix == ".jpg"
    assert cards["sign-in"].read_bytes()[:3] == b"\xff\xd8\xff"


# --------------------------------------------------------------------------- start and stop


@pytest.fixture(autouse=True)
def _ffmpeg_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests do not depend on the machine having ffmpeg (CI has none)."""
    monkeypatch.setattr(recording.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(recording, "_process_started", lambda pid: "Fri Nov 20 10:00:00 2026")


def _left(directory: Path) -> list[str]:
    """What is in a task's captures folder, not counting its lock file."""
    return sorted(p.name for p in directory.iterdir() if p.name != ".lock")


def _setup(tmp_path: Path) -> Any:
    return SimpleNamespace(config_dir=tmp_path, browser=SimpleNamespace(tasks={}))


def _state(directory: Path, session: str = "20261119T000000000Z-recording") -> None:
    (directory / recording.STATE_NAME).write_text(
        json.dumps({"pid": 999_999, "session": session, "started": 1000.0, "task": "t"})
    )


def test_stopping_with_no_recording_says_so(tmp_path: Path) -> None:
    with pytest.raises(BrowserActionError, match="to stop, got none"):
        recording.stop_recording(_setup(tmp_path), "time-sheet")


def test_status_reports_not_recording_running_and_unfinished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path)
    directory = recording.captures_dir(tmp_path, "time-sheet")
    assert recording.recording_status(setup, "time-sheet")["recording"] is False

    _state(directory)
    monkeypatch.setattr(recording, "_worker_alive", lambda state: True)
    running = recording.recording_status(setup, "time-sheet", now=lambda: 1030.0)
    assert running["recording"] is True and running["seconds"] == 30.0

    monkeypatch.setattr(recording, "_worker_alive", lambda state: False)
    gone = recording.recording_status(setup, "time-sheet")
    assert gone["recording"] is False and gone["unfinished"] is True


def test_start_refuses_while_a_recording_runs_or_is_unfinished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path)
    monkeypatch.setattr(recording, "load_profile", lambda *args: SimpleNamespace())
    directory = recording.captures_dir(tmp_path, "time-sheet")
    _state(directory)

    monkeypatch.setattr(recording, "_worker_alive", lambda state: True)
    with pytest.raises(BrowserActionError, match="to be running"):
        recording.start_recording(setup, "time-sheet")

    monkeypatch.setattr(recording, "_worker_alive", lambda state: False)
    with pytest.raises(BrowserActionError, match="unfinished recording.*record stop"):
        recording.start_recording(setup, "time-sheet")


def test_start_without_a_browser_profile_names_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "load_profile", lambda *args: None)

    with pytest.raises(recording.TaskNotFoundError, match="time-sheet"):
        recording.start_recording(_setup(tmp_path), "time-sheet")


def test_a_worker_that_fails_to_start_leaves_nothing_behind_and_its_last_log_line_is_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "load_profile", lambda *args: SimpleNamespace())
    monkeypatch.setattr(recording, "sign_in_hosts_for", lambda *args: (), raising=False)
    started: list[list[str]] = []

    class _Worker:
        pid = 4242

        def poll(self) -> int:
            return 1

        def kill(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 1

    def fake_popen(command: list[str], **kwargs: Any) -> _Worker:
        started.append(command)
        session = Path(command[-1])
        (session / "worker.log").write_text("Expected a running browser, got none\n")
        return _Worker()

    monkeypatch.setattr(recording.subprocess, "Popen", fake_popen)

    with pytest.raises(BrowserActionError, match="Expected a running browser"):
        recording.start_recording(_setup(tmp_path), "time-sheet")

    directory = recording.captures_dir(tmp_path, "time-sheet")
    assert not (directory / recording.STATE_NAME).exists()
    assert _left(directory) == []
    assert started[0][1:3] == ["-m", "blumkin.browser.recording"]


def test_start_returns_once_the_worker_says_it_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "load_profile", lambda *args: SimpleNamespace())
    seen: dict[str, Any] = {}

    class _Worker:
        pid = 4242

        def poll(self) -> None:
            return None

    def fake_popen(command: list[str], **kwargs: Any) -> _Worker:
        seen["kwargs"] = kwargs
        session = Path(command[-1])
        seen["meta"] = json.loads((session / "meta.json").read_text())
        (session / "ready").write_text("1")
        return _Worker()

    monkeypatch.setattr(recording.subprocess, "Popen", fake_popen)

    result = recording.start_recording(_setup(tmp_path), "time-sheet")

    assert result["recording"] is True
    assert seen["kwargs"]["start_new_session"] is True  # it outlives this command
    assert (
        seen["meta"]["task"] == "time-sheet"
        and seen["meta"]["max_seconds"] == recording.RECORD_MAX_SECONDS
    )
    state = json.loads((tmp_path / "captures" / "time-sheet" / recording.STATE_NAME).read_text())
    assert state["pid"] == 4242


def test_stop_encodes_what_an_unfinished_recording_holds_and_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261119T000000000Z-recording"
    _session_into(session)
    _state(directory, session.name)
    monkeypatch.setattr(recording, "_worker_alive", lambda state: False)
    monkeypatch.setattr(recording.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        Path(command[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(recording.subprocess, "run", fake_run)

    result = recording.stop_recording(_setup(tmp_path), "time-sheet", now=lambda: 1060.0)

    assert result["ok"] is True and result["ended"] == "worker ended early"
    assert Path(result["path"]).name == "20261119T000000000Z.mp4"
    assert not session.exists() and not (directory / recording.STATE_NAME).exists()
    assert result["wall_seconds"] == 60.0


def _session_into(session: Path) -> None:
    from PIL import Image

    (session / "frames").mkdir(parents=True)
    Image.new("RGB", (64, 36), (1, 2, 3)).save(session / "frames" / "f000001.jpg", format="JPEG")
    (session / "index.jsonl").write_text(
        json.dumps({"file": "frames/f000001.jpg", "kind": "frame", "t": 1.0})
        + "\n"
        + json.dumps({"kind": "end", "t": 3.0})
        + "\n"
    )


def test_stop_keeps_the_frames_on_request_and_refuses_an_existing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261119T000000000Z-recording"
    _session_into(session)
    _state(directory, session.name)
    monkeypatch.setattr(recording, "_worker_alive", lambda state: False)
    existing = tmp_path / "taken.mp4"
    existing.write_bytes(b"x")

    with pytest.raises(ValueError, match="does not exist yet"):
        recording.stop_recording(_setup(tmp_path), "time-sheet", out=existing)
    assert session.exists() and (directory / recording.STATE_NAME).exists()  # nothing was lost


def test_stop_signals_a_live_worker_and_waits_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261119T000000000Z-recording"
    _session_into(session)
    _state(directory, session.name)
    signals: list[tuple[int, int]] = []
    alive = iter([True, True, False, False, False])
    monkeypatch.setattr(recording, "_worker_alive", lambda state: next(alive, False))
    monkeypatch.setattr(recording.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(recording.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        recording.subprocess,
        "run",
        lambda command, **kwargs: (
            (Path(command[-1]).write_bytes(b"mp4") and None)
            or SimpleNamespace(returncode=0, stderr="")
        ),
    )
    monkeypatch.setattr(recording.time, "sleep", lambda seconds: None)

    recording.stop_recording(_setup(tmp_path), "time-sheet")

    assert signals == [(999_999, recording.signal.SIGTERM)]


def test_the_worker_check_is_not_fooled_by_a_reused_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recording.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(
        recording.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="/usr/bin/vim notes.txt\n"),
    )
    assert recording._worker_alive({"pid": 5, "session": "s-recording"}) is False

    monkeypatch.setattr(
        recording.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            stdout="python -m blumkin.browser.recording /x/s-recording\n"
        ),
    )
    assert recording._worker_alive({"pid": 5, "session": "s-recording"}) is True
    # another recorder (a different session) that took over the pid is not this one's worker
    assert recording._worker_alive({"pid": 5, "session": "older-recording"}) is False


def test_clip_refuses_a_length_outside_the_limits(tmp_path: Path) -> None:
    for seconds in (0.5, recording.RECORD_MAX_SECONDS + 1):
        with pytest.raises(ValueError, match="seconds"):
            recording.clip_recording(_setup(tmp_path), "time-sheet", seconds=seconds)


def test_clip_refuses_while_a_recording_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "load_profile", lambda *args: SimpleNamespace())
    _state(recording.captures_dir(tmp_path, "time-sheet"))

    with pytest.raises(BrowserActionError, match="record stop"):
        recording.clip_recording(_setup(tmp_path), "time-sheet", seconds=5)


def test_the_prototype_polling_recorder_is_gone() -> None:
    from blumkin.browser import capture

    assert not hasattr(capture, "record_task")


# --------------------------------------------------------------------------- captions


def test_a_caption_names_the_control_and_never_the_value() -> None:
    params = {
        "label": "Amount",
        "value": "4111 1111 1111 1111",
        "option": "Taxi",
        "path": "/r/a.pdf",
    }

    assert recording.caption_for("fill", params) == 'fill "Amount"'
    assert recording.caption_for("select", params) == 'select "Amount"'
    assert recording.caption_for("upload", params) == 'upload "Amount"'
    assert recording.caption_for("cell", {"row": 1, "column": "Mon,Nov 16", "value": "8"}) == (
        'cell 1 "Mon,Nov 16"'
    )
    for command in ("fill", "select", "upload", "cell", "verify", "press"):
        text = recording.caption_for(command, {**params, "expected": "secret", "key": "Tab"}) or ""
        assert "4111" not in text and "Taxi" not in text and "a.pdf" not in text
        assert "secret" not in text


def test_a_goto_caption_shows_the_host_only() -> None:
    caption = recording.caption_for("goto", {"url": "https://erp.example.com/a/b?token=xyz#f"})

    assert caption == "goto erp.example.com"


def test_a_dialog_caption_lists_what_it_opens() -> None:
    assert recording.caption_for("dialog", {"open_specs": ("Templates", "Apply")}) == (
        "dialog opens Templates, Apply"
    )
    assert recording.caption_for("dialog", {"open_specs": ()}) == "dialog"


def test_a_caption_is_one_short_printable_line() -> None:
    caption = recording.caption_for("click", {"name": "Add\nline\x07" + "x" * 300})

    assert caption is not None and "\n" not in caption and "\x07" not in caption
    assert len(caption) <= recording.CAPTION_MAX_CHARS


@pytest.mark.parametrize("command", ["status", "launch", "close", "handoff", "address", "record"])
def test_commands_that_say_nothing_about_the_page_have_no_caption(command: str) -> None:
    assert recording.caption_for(command, {"task": "t"}) is None


def test_every_parameter_the_caption_table_names_exists_on_the_real_command() -> None:
    """A renamed option would otherwise silently drop out of the captions."""
    from blumkin.cli import browser

    for command, (_, parts) in recording.CAPTION_PARTS.items():
        assert command in browser.commands, command
        real = {param.name for param in browser.commands[command].params}
        assert {name for name, _ in parts} <= real, command


def test_a_command_run_during_a_recording_leaves_a_caption(tmp_path: Path) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261119T000000000Z-recording"
    session.mkdir()
    _state(directory, session.name)

    recording.note_command(tmp_path, "time-sheet", "click", {"name": "Add"}, now=lambda: 50.0)
    recording.note_command(tmp_path, "time-sheet", "status", {}, now=lambda: 51.0)

    assert recording.read_captions(session) == [(50.0, 'click "Add"')]
    assert stat.S_IMODE((session / "captions.jsonl").stat().st_mode) == 0o600


def test_no_caption_is_written_without_a_recording_or_after_it_ended(tmp_path: Path) -> None:
    recording.note_command(tmp_path, "time-sheet", "click", {"name": "Add"})
    assert not (tmp_path / "captures" / "time-sheet").exists() or not list(
        (tmp_path / "captures" / "time-sheet").rglob("captions.jsonl")
    )

    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261119T000000000Z-recording"
    session.mkdir()
    (session / "reason").write_text("stopped")
    _state(directory, session.name)
    recording.note_command(tmp_path, "time-sheet", "click", {"name": "Add"})
    assert not (session / "captions.jsonl").exists()


def test_a_task_name_with_a_path_in_it_writes_nothing(tmp_path: Path) -> None:
    recording.note_command(tmp_path, "../evil", "click", {"name": "Add"})

    assert not (tmp_path / "evil").exists()


def test_a_caption_failure_never_fails_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        recording, "_read_state", lambda directory: (_ for _ in ()).throw(OSError("x"))
    )

    recording.note_command(tmp_path, "time-sheet", "click", {"name": "Add"})  # does not raise


def test_a_browser_command_captions_a_running_recording_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from blumkin import cli

    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261119T000000000Z-recording"
    session.mkdir()
    _state(directory, session.name)
    monkeypatch.setattr(
        cli,
        "click_task",
        lambda *a, **k: {
            "action": "click",
            "ok": True,
            "role": "button",
            "name": "Add line",
            "task": "time-sheet",
        },
    )
    monkeypatch.setattr(cli, "load_browser_setup", lambda: SimpleNamespace())

    result = CliRunner().invoke(cli.main, ["browser", "click", "time-sheet", "Add line", "--json"])

    assert result.exit_code == 0, result.output
    assert [text for _, text in recording.read_captions(session)] == ['click "Add line"']


# --------------------------------------------------------------------------- the commands


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config))
    (config / "config.toml").write_text('[profiles.default]\nclient_id = "abc-123"\n')
    return config


def _cli(*args: str) -> Any:
    from click.testing import CliRunner

    from blumkin.cli import main

    return CliRunner().invoke(main, ["--json", "browser", "record", *args])


_WRITTEN = {
    "action": "record-stop",
    "bytes": 1234,
    "captions": 2,
    "cards": 1,
    "ended": "stopped",
    "format": "mp4",
    "frames": 9,
    "frames_dir": None,
    "height": 450,
    "ok": True,
    "path": "/c/t/x.mp4",
    "seconds": 12.5,
    "speed": 1.0,
    "task": "time-sheet",
    "untrusted": "data",
    "wall_seconds": 12.6,
    "width": 800,
}


def test_record_start_calls_the_recorder_with_the_task(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blumkin import cli

    seen: list[str] = []
    monkeypatch.setattr(
        cli, "start_recording", lambda setup, task: seen.append(task) or {"ok": True, "task": task}
    )

    result = _cli("start", "time-sheet")

    assert result.exit_code == 0, result.output
    assert seen == ["time-sheet"]


def test_record_stop_passes_its_encoding_options(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blumkin import cli

    seen: dict[str, Any] = {}

    def fake(setup: Any, task: str, **kwargs: Any) -> dict[str, Any]:
        seen.update(task=task, **kwargs)
        return _WRITTEN

    monkeypatch.setattr(cli, "stop_recording", fake)

    result = _cli(
        "stop", "time-sheet", "--format", "gif", "--out", "/tmp/x.gif", "--speed", "2",
        "--trim-idle", "1.5", "--keep-frames",
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert seen == {
        "task": "time-sheet",
        "fmt": "gif",
        "out": Path("/tmp/x.gif"),
        "speed": 2.0,
        "trim_idle_s": 1.5,
        "keep_frames": True,
        "narration": NarrationOptions(),  # on by default; the cues given with `narrate` are spoken
    }


def test_record_stop_defaults_to_an_mp4_at_real_speed(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blumkin import cli

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        cli, "stop_recording", lambda setup, task, **kwargs: seen.update(kwargs) or _WRITTEN
    )

    _cli("stop", "time-sheet")

    assert seen["fmt"] == "mp4" and seen["speed"] == 1.0 and seen["trim_idle_s"] is None


def test_record_clip_needs_a_duration_and_passes_it_on(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blumkin import cli

    assert _cli("clip", "time-sheet").exit_code == 2
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        cli, "clip_recording", lambda setup, task, **kwargs: seen.update(kwargs) or _WRITTEN
    )

    result = _cli("clip", "time-sheet", "--seconds", "20")

    assert result.exit_code == 0, result.output
    assert seen["seconds"] == 20.0


@pytest.mark.parametrize(
    "args",
    [
        ("stop", "t", "--speed", "0"),
        ("stop", "t", "--speed", "nan"),
        ("stop", "t", "--trim-idle", "0"),
        ("stop", "t", "--format", "avi"),
        ("clip", "t", "--seconds", "0"),
        ("clip", "t", "--seconds", "100000"),
    ],
)
def test_bad_record_options_are_usage_errors(config_dir: Path, args: tuple[str, ...]) -> None:
    assert _cli(*args).exit_code == 2


def test_record_status_prints_each_state(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from blumkin import cli

    states = iter(
        [
            {"ok": True, "recording": True, "seconds": 3.0, "task": "t"},
            {"ok": True, "recording": False, "task": "t", "unfinished": True},
            {"ok": True, "recording": False, "task": "t"},
        ]
    )
    monkeypatch.setattr(cli, "recording_status", lambda setup, task: next(states))

    outputs = [
        CliRunner().invoke(cli.main, ["browser", "record", "status", "t"]).output for _ in range(3)
    ]

    assert "recording for 3.0 s" in outputs[0]
    assert "unfinished recording is waiting" in outputs[1]
    assert "not recording" in outputs[2]


def test_a_recording_failure_is_reported_as_an_error_not_a_traceback(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blumkin import cli

    def fail(setup: Any, task: str) -> dict[str, Any]:
        raise BrowserActionError("Expected a running browser, got none")

    monkeypatch.setattr(cli, "start_recording", fail)

    result = _cli("start", "time-sheet")

    assert result.exit_code == 1 and "Expected a running browser" in result.output
    assert "Traceback" not in result.output


# --------------------------------------------------------------------------- frame sizes


def _ffprobe_seconds(path: Path) -> float:
    done = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(json.loads(done.stdout)["format"]["duration"])


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are needed to check the video",
)
def test_frames_of_different_sizes_do_not_cost_the_video_any_of_its_length(tmp_path: Path) -> None:
    """The start keyframe is full size and the screencast frames are scaled by the browser:
    ffmpeg drops the frame it is holding when the size changes, which cut a 4.8 s frame."""
    from PIL import Image

    session = tmp_path / "session"
    (session / "frames").mkdir(parents=True)
    sizes = [(128, 72), (64, 36), (64, 36), (63, 36), (64, 36)]
    lines = []
    for number, size in enumerate(sizes):
        name = f"frames/f{number + 1:06d}.jpg"
        Image.new("RGB", size, (40 * number, 20, 30)).save(session / name, format="JPEG")
        lines.append({"file": name, "kind": "frame", "t": 100.0 + [0, 4.8, 5.0, 7.0, 8.0][number]})
    lines.append({"kind": "end", "t": 109.0})
    (session / "index.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")

    written = encode_recording(session, tmp_path / "out.mp4")

    assert written["seconds"] == pytest.approx(9.0, abs=0.01)
    assert _ffprobe_seconds(tmp_path / "out.mp4") == pytest.approx(9.0, abs=0.3)
    assert (written["width"], written["height"]) == (64, 36)  # the size most frames have


def test_the_size_most_frames_have_is_the_size_of_the_video(tmp_path: Path) -> None:
    from PIL import Image

    session = tmp_path / "session"
    (session / "frames").mkdir(parents=True)
    entries = []
    for number, size in enumerate([(128, 72), (64, 36), (64, 36)]):
        name = f"frames/f{number}.jpg"
        Image.new("RGB", size, (1, 2, 3)).save(session / name, format="JPEG")
        entries.append(Entry(t=float(number), kind="frame", file=name))
    paths = {entry.file: session / entry.file for entry in entries if entry.file}

    assert recording._video_size(entries, paths) == (64, 36)


def test_a_frame_of_another_size_is_made_the_video_size_before_it_is_encoded(
    tmp_path: Path,
) -> None:
    from PIL import Image

    session = tmp_path / "session"
    (session / "frames").mkdir(parents=True)
    Image.new("RGB", (128, 72), (200, 0, 0)).save(session / "frames" / "a.jpg", format="JPEG")
    Image.new("RGB", (64, 36), (0, 0, 200)).save(session / "frames" / "b.jpg", format="JPEG")
    entries = [
        Entry(t=0.0, kind="frame", file="frames/a.jpg"),
        Entry(t=1.0, kind="frame", file="frames/b.jpg"),
    ]
    paths = {entry.file: session / entry.file for entry in entries if entry.file}

    fixed = recording._same_size(entries, paths, 64, 36, session)

    for entry in entries:
        with Image.open(fixed[entry.file or ""]) as image:
            assert image.size == (64, 36)
    assert fixed["frames/b.jpg"] == paths["frames/b.jpg"]  # a frame that is right is left alone


def test_a_keyframe_is_fitted_to_the_screencast_size_and_a_small_one_is_left_alone() -> None:
    import io

    from PIL import Image

    from blumkin.browser.cdp import _fit_jpeg

    def jpeg(size: tuple[int, int]) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", size, (9, 9, 9)).save(buffer, format="JPEG")
        return buffer.getvalue()

    big = _fit_jpeg(jpeg((2000, 1000)), 1280, 60)
    with Image.open(io.BytesIO(big)) as image:
        assert image.size == (1280, 640)  # fitted inside the box, aspect kept
    tall = _fit_jpeg(jpeg((1000, 2560)), 1280, 60)
    with Image.open(io.BytesIO(tall)) as image:
        assert image.size == (500, 1280)
    small = jpeg((300, 200))
    assert _fit_jpeg(small, 1280, 60) is small


# --------------------------------------------------------------------------- lifecycle safety


def _worker_double(*, polls: int | None = None, ready: bool = True) -> Any:
    class _Worker:
        pid = 4242
        killed = 0

        def poll(self) -> int | None:
            return polls

        def kill(self) -> None:
            type(self).killed += 1

        def wait(self, timeout: float | None = None) -> int:
            return 0

    return _Worker


def test_the_task_is_reserved_before_the_worker_is_launched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "load_profile", lambda *args: SimpleNamespace())
    seen: dict[str, Any] = {}
    worker = _worker_double()

    def fake_popen(command: list[str], **kwargs: Any) -> Any:
        state = tmp_path / "captures" / "time-sheet" / recording.STATE_NAME
        seen["state_at_launch"] = json.loads(state.read_text())
        (Path(command[-1]) / "ready").write_text("1")
        return worker()

    monkeypatch.setattr(recording.subprocess, "Popen", fake_popen)

    recording.start_recording(_setup(tmp_path), "time-sheet")

    assert seen["state_at_launch"]["pid"] == 0  # reserved first, the pid filled in after
    final = json.loads((tmp_path / "captures" / "time-sheet" / recording.STATE_NAME).read_text())
    assert final["pid"] == 4242


def test_a_second_start_while_one_is_starting_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "load_profile", lambda *args: SimpleNamespace())
    directory = recording.captures_dir(tmp_path, "time-sheet")
    (directory / recording.STATE_NAME).write_text(
        json.dumps({"pid": 0, "session": "s-recording", "started": time.time(), "task": "t"})
    )

    with pytest.raises(BrowserActionError, match="to be starting"):
        recording.start_recording(_setup(tmp_path), "time-sheet")


def test_the_state_file_can_only_be_created_once(tmp_path: Path) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    state = {"pid": 0, "session": "a", "started": 0.0, "task": "time-sheet"}
    recording._reserve_state(directory, state)

    with pytest.raises(BrowserActionError, match="just started by another command"):
        recording._reserve_state(directory, state)


def test_a_launch_that_raises_leaves_no_session_and_no_state_and_closes_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "load_profile", lambda *args: SimpleNamespace())

    def fail(command: list[str], **kwargs: Any) -> Any:
        assert not kwargs["stdout"].closed
        seen["log"] = kwargs["stdout"]
        raise OSError("cannot exec")

    seen: dict[str, Any] = {}
    monkeypatch.setattr(recording.subprocess, "Popen", fail)

    with pytest.raises(OSError, match="cannot exec"):
        recording.start_recording(_setup(tmp_path), "time-sheet")

    directory = tmp_path / "captures" / "time-sheet"
    assert seen["log"].closed
    assert _left(directory) == []  # no session folder, no state file


def test_a_state_that_cannot_be_written_stops_the_worker_that_was_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "load_profile", lambda *args: SimpleNamespace())
    worker = _worker_double()
    monkeypatch.setattr(recording.subprocess, "Popen", lambda *a, **k: worker())
    monkeypatch.setattr(
        recording, "_write_state", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(OSError, match="disk full"):
        recording.start_recording(_setup(tmp_path), "time-sheet")

    assert worker.killed == 1
    assert _left(tmp_path / "captures" / "time-sheet") == []


def test_a_worker_that_will_not_start_is_stopped_before_its_folder_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "load_profile", lambda *args: SimpleNamespace())
    worker = _worker_double(polls=1)
    monkeypatch.setattr(recording.subprocess, "Popen", lambda *a, **k: worker())

    with pytest.raises(BrowserActionError, match="Expected the recording to start"):
        recording.start_recording(_setup(tmp_path), "time-sheet")

    assert worker.killed == 1
    assert _left(tmp_path / "captures" / "time-sheet") == []


def test_stop_does_not_read_the_journal_while_the_worker_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261120T000000000Z-recording"
    _session_into(session)
    _state(directory, session.name)
    monkeypatch.setattr(recording, "_worker_alive", lambda state: True)  # it never exits
    monkeypatch.setattr(recording.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(recording.time, "sleep", lambda seconds: None)
    clock = iter(range(0, 10_000, 40))
    monkeypatch.setattr(recording.time, "monotonic", lambda: float(next(clock)))
    encoded: list[Path] = []
    monkeypatch.setattr(recording, "encode_recording", lambda *a, **k: encoded.append(a[0]))

    with pytest.raises(BrowserActionError, match="still runs"):
        recording.stop_recording(_setup(tmp_path), "time-sheet")

    assert encoded == []  # nothing was encoded from a journal still being written
    assert session.exists() and (directory / recording.STATE_NAME).exists()


def test_stop_refuses_a_record_clip_that_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    (directory / recording.STATE_NAME).write_text(
        json.dumps(
            {"kind": "clip", "pid": 99, "session": "s", "started": 0.0, "task": "time-sheet"}
        )
    )
    monkeypatch.setattr(recording, "_worker_alive", lambda state: True)

    with pytest.raises(BrowserActionError, match="ends by itself"):
        recording.stop_recording(_setup(tmp_path), "time-sheet")


def test_releasing_a_state_never_removes_another_sessions(tmp_path: Path) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    _state(directory, "running-recording")

    recording._release_state(directory, "some-other-recording")
    assert (directory / recording.STATE_NAME).exists()

    recording._release_state(directory, "running-recording")
    assert not (directory / recording.STATE_NAME).exists()


# --------------------------------------------------------------------------- the output file


def _ffmpeg_that_writes(monkeypatch: pytest.MonkeyPatch, data: bytes = b"mp4") -> list[Path]:
    written: list[Path] = []

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        path = Path(command[-1])
        written.append(path)
        path.write_bytes(data)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(recording.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(recording.subprocess, "run", fake_run)
    return written


def _finished(tmp_path: Path, out: Path | None) -> dict[str, Any]:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261120T000000000Z-recording"
    _session_into(session)
    return recording._finish(
        directory, session, task="time-sheet", fmt="mp4", out=out, speed=1.0, trim_idle_s=None,
        keep_frames=False, started=0.0, now=lambda: 3.0, action="record-stop",
    )  # fmt: skip


def test_the_video_is_encoded_into_a_private_temporary_file_and_never_to_its_final_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = _ffmpeg_that_writes(monkeypatch)
    target = tmp_path / "out.mp4"

    result = _finished(tmp_path, target)

    assert written and written[0] != target  # ffmpeg wrote the temporary file
    assert stat.S_IMODE(target.stat().st_mode) == 0o600 and target.read_bytes() == b"mp4"
    assert not written[0].exists()  # and it is gone
    assert result["path"] == str(target)


def test_a_destination_that_appears_before_publishing_is_not_overwritten_or_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out.mp4"

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        Path(command[-1]).write_bytes(b"ours")
        target.write_bytes(b"someone else's file")  # created while ffmpeg ran
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(recording.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(recording.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="does not exist yet"):
        _finished(tmp_path, target)

    assert target.read_bytes() == b"someone else's file"
    assert not list((tmp_path / "captures" / "time-sheet").rglob(".encoding-*"))


def test_a_destination_that_exists_is_refused_before_any_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = _ffmpeg_that_writes(monkeypatch)
    target = tmp_path / "out.mp4"
    target.write_bytes(b"x")

    with pytest.raises(ValueError, match="does not exist yet"):
        _finished(tmp_path, target)

    assert written == [] and target.read_bytes() == b"x"


def test_a_failed_encode_removes_only_its_own_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(command: list[str], **kwargs: Any) -> Any:
        Path(command[-1]).write_bytes(b"half")
        return SimpleNamespace(returncode=1, stderr="boom")

    monkeypatch.setattr(recording.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(recording.subprocess, "run", fail)
    target = tmp_path / "out.mp4"

    with pytest.raises(BrowserActionError, match="boom"):
        _finished(tmp_path, target)

    assert not target.exists()
    assert not list((tmp_path / "captures" / "time-sheet").rglob(".encoding-*"))


def test_publishing_falls_back_to_a_copy_into_a_private_file_when_a_link_is_not_possible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work.mp4"
    work.write_bytes(b"video")
    target = tmp_path / "other-filesystem.mp4"
    monkeypatch.setattr(
        recording.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("cross-device"))
    )

    recording._publish(work, target)

    assert target.read_bytes() == b"video"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_the_copy_fallback_never_replaces_an_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work.mp4"
    work.write_bytes(b"video")
    target = tmp_path / "taken.mp4"
    target.write_bytes(b"theirs")
    monkeypatch.setattr(
        recording.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("cross-device"))
    )

    with pytest.raises(ValueError, match="does not exist yet"):
        recording._publish(work, target)

    assert target.read_bytes() == b"theirs"


def test_a_clip_checks_its_output_before_it_records_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attached: list[Any] = []
    monkeypatch.setattr(recording, "load_profile", lambda *args: SimpleNamespace())
    monkeypatch.setattr(recording, "attach", lambda *a, **k: attached.append(1))
    existing = tmp_path / "taken.mp4"
    existing.write_bytes(b"x")

    monkeypatch.setattr(recording.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="ffmpeg"):
        recording.clip_recording(_setup(tmp_path), "time-sheet", seconds=5)
    monkeypatch.setattr(recording.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    with pytest.raises(ValueError, match="does not exist yet"):
        recording.clip_recording(_setup(tmp_path), "time-sheet", seconds=5, out=existing)
    with pytest.raises(ValueError, match="format"):
        recording.clip_recording(_setup(tmp_path), "time-sheet", seconds=5, fmt="avi")

    assert attached == []  # the browser was never touched


def test_a_clip_holds_the_task_while_it_records_and_releases_it_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "load_profile", lambda *args: SimpleNamespace())
    monkeypatch.setattr(recording, "sign_in_hosts_for", lambda *a: (), raising=False)
    monkeypatch.setattr(recording.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    state_file = tmp_path / "captures" / "time-sheet" / recording.STATE_NAME
    seen: dict[str, Any] = {}

    class _Driver2:
        def close(self) -> None:
            pass

    monkeypatch.setattr(recording, "attach", lambda *a, **k: _Driver2())

    def fake_run_recorder(driver: Any, session: Path, **kwargs: Any) -> str:
        seen["state"] = json.loads(state_file.read_text())
        _session_into(session)
        return "stopped"

    monkeypatch.setattr(recording, "run_recorder", fake_run_recorder)
    _ffmpeg_that_writes(monkeypatch)

    recording.clip_recording(_setup(tmp_path), "time-sheet", seconds=5)

    assert seen["state"]["kind"] == "clip" and seen["state"]["pid"] == os.getpid()
    assert not state_file.exists()


# --------------------------------------------------------------------------- small policy checks


def test_a_picked_text_never_reaches_a_caption() -> None:
    assert recording.caption_for("pick", {"text": "Secret Project 4242", "role": "row"}) == "pick"


def test_a_frame_on_a_sign_in_host_makes_the_page_a_sign_in_page() -> None:
    assert classify(_APP, _HOSTS, _SIGN_IN, ("app.example.com", "login.microsoftonline.com")) == (
        "sign-in"
    )
    assert classify(_APP, _HOSTS, _SIGN_IN, ("app.example.com", "")) == "ok"


def test_only_a_jpeg_that_decodes_is_a_frame() -> None:
    assert recording._valid_jpeg(_APP_PIXELS) is True
    assert recording._valid_jpeg(b"") is False
    assert recording._valid_jpeg(b"\xff\xd8\xff garbage") is False
    assert recording._valid_jpeg(_APP_PIXELS[: len(_APP_PIXELS) // 2]) is False


# --------------------------------------------------------------------------- review round


def test_an_inner_sign_in_frame_that_came_and_went_leaves_a_card_not_pixels(
    tmp_path: Path,
) -> None:
    driver = _Driver([_Step()])
    original = driver.screencast_pump

    def pump(wait_ms: int) -> list[ScreenFrame]:
        if wait_ms == recording.POLL_MS:
            driver.inner_now.append("login.microsoftonline.com")  # gone again by the next look
        return original(wait_ms)

    driver.screencast_pump = pump  # type: ignore[method-assign]

    _, lines = _run(tmp_path, driver, False, True)

    assert any(item.get("reason") == "sign-in" for item in lines if item["kind"] == "card")


def test_an_inner_frame_on_another_host_does_not_make_a_card(tmp_path: Path) -> None:
    driver = _Driver([_Step()])
    original = driver.screencast_pump

    def pump(wait_ms: int) -> list[ScreenFrame]:
        if wait_ms == recording.POLL_MS:
            driver.inner_now.append("ads.example.net")
        return original(wait_ms)

    driver.screencast_pump = pump  # type: ignore[method-assign]

    _, lines = _run(tmp_path, driver, False, True)

    assert not [item for item in lines if item["kind"] == "card"]


def test_only_keys_that_type_nothing_are_named_in_a_caption() -> None:
    for typed in ("Shift+A", "a", "Control+S", "5"):
        assert recording.caption_for("press", {"label": "Amount", "key": typed}) == 'press "Amount"'
    assert recording.caption_for("press", {"label": "Amount", "key": "Tab"}) == 'press "Amount" Tab'


def test_a_typed_character_is_never_a_caption() -> None:
    assert recording.caption_for("press", {"label": "Password", "key": "s"}) == 'press "Password"'
    assert (
        recording.caption_for("press", {"label": "Amount", "key": "Enter"})
        == 'press "Amount" Enter'
    )


def test_the_playlist_names_files_by_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    entry = recording.Entry(t=0.0, kind="frame", file="f.jpg", reason=None)

    listing = recording.concat_listing([entry], [1.0], {"f.jpg": Path("rel/f.jpg")}, {})

    assert f"file '{tmp_path / 'rel' / 'f.jpg'}'" in listing


def test_a_missing_output_folder_is_refused_before_recording(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="existing folder"):
        recording._check_output("gif", tmp_path / "nope" / "out.gif", 1.0, None)


def test_a_recorder_that_died_before_writing_anything_is_cleared_by_stop(
    tmp_path: Path,
) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261120T000000000Z-recording"
    session.mkdir()
    _state(directory, session.name)  # pid 999999: nothing runs there

    with pytest.raises(BrowserActionError, match="died before it wrote anything"):
        recording.stop_recording(_setup(tmp_path), "time-sheet")

    assert not session.exists() and not (directory / recording.STATE_NAME).exists()


def test_a_clip_that_cannot_be_written_keeps_what_it_recorded_for_record_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "load_profile", lambda *args: SimpleNamespace())
    monkeypatch.setattr(recording.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(recording, "attach", lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        recording, "run_recorder", lambda driver, session, **k: (_session_into(session), "x")[1]
    )

    def broken(command: list[str], **kwargs: Any) -> Any:
        return SimpleNamespace(returncode=1, stderr="boom")

    monkeypatch.setattr(recording.subprocess, "run", broken)

    with pytest.raises(BrowserActionError, match="is kept"):
        recording.clip_recording(_setup(tmp_path), "time-sheet", seconds=5)

    directory = tmp_path / "captures" / "time-sheet"
    assert [p.name for p in directory.glob("*-recording")]  # the frames are still there
    assert (directory / recording.STATE_NAME).exists()  # `record stop` can write them again


def test_a_clips_time_runs_from_its_first_picture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "load_profile", lambda *args: SimpleNamespace())
    monkeypatch.setattr(recording.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(recording, "attach", lambda *a, **k: SimpleNamespace(close=lambda: None))
    _ffmpeg_that_writes(monkeypatch)
    now = [1000.0]
    seen: dict[str, Any] = {}

    def fake(driver: Any, session: Path, *, stop: Any, on_ready: Any, **kwargs: Any) -> str:
        seen["before_ready"] = stop()
        now[0] += 400.0  # attaching and the settle wait took a long while
        on_ready()
        seen["just_ready"] = stop()
        now[0] += 5.0
        seen["after_time"] = stop()
        _session_into(session)
        return "stopped"

    monkeypatch.setattr(recording, "run_recorder", fake)

    recording.clip_recording(
        _setup(tmp_path), "time-sheet", seconds=5, now=lambda: now[0], tick=lambda: now[0]
    )

    assert seen == {"before_ready": False, "just_ready": False, "after_time": True}


# --------------------------------------------------------------------------- review round 3


def test_cleanup_waits_for_the_task_lock_that_a_start_holds(tmp_path: Path) -> None:
    import threading

    from blumkin.browser import capture

    directory = recording.captures_dir(tmp_path, "time-sheet")
    finished = threading.Event()

    def clean() -> None:
        capture.prune_captures(directory, 0.0)
        finished.set()

    with capture.task_lock(directory):
        worker = threading.Thread(target=clean)
        worker.start()
        assert not finished.wait(0.3)  # held back while the lock is ours
    assert finished.wait(5.0)
    worker.join()


def test_stop_refuses_a_recording_that_is_still_starting(tmp_path: Path) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    (directory / recording.STATE_NAME).write_text(
        json.dumps({"pid": 0, "session": "s-recording", "started": time.time(), "task": "t"})
    )

    with pytest.raises(BrowserActionError, match="still starting"):
        recording.stop_recording(_setup(tmp_path), "time-sheet")

    assert (directory / recording.STATE_NAME).exists()  # not cleared from under the starter


def test_a_dead_recorder_that_left_an_empty_journal_is_cleared(tmp_path: Path) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261120T000000000Z-recording"
    session.mkdir()
    (session / "index.jsonl").write_text("")  # created, then the worker died
    _state(directory, session.name)

    with pytest.raises(BrowserActionError, match="died before it wrote anything"):
        recording.stop_recording(_setup(tmp_path), "time-sheet")

    assert _left(directory) == []


def test_a_dead_recorder_with_one_picture_is_kept_and_encoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261120T000000000Z-recording"
    _session_into(session)
    _state(directory, session.name)
    _ffmpeg_that_writes(monkeypatch)

    result = recording.stop_recording(_setup(tmp_path), "time-sheet")

    assert result["ok"] is True


def test_a_failed_state_update_leaves_the_reservation_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    state = {"pid": 0, "session": "a-recording", "started": 1.0, "task": "time-sheet"}
    recording._reserve_state(directory, state)
    monkeypatch.setattr(
        recording, "_write_private", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(OSError, match="disk full"):
        recording._write_state(directory, {**state, "pid": 77})

    assert json.loads((directory / recording.STATE_NAME).read_text()) == state
    assert _left(directory) == [recording.STATE_NAME]  # and no half-written sibling


def test_a_state_update_never_replaces_another_sessions_reservation(tmp_path: Path) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    recording._reserve_state(
        directory, {"pid": 0, "session": "b-recording", "started": 1.0, "task": "time-sheet"}
    )

    with pytest.raises(BrowserActionError, match="to still be ours"):
        recording._write_state(
            directory,
            {"pid": 5, "session": "a-recording", "started": 1.0, "task": "time-sheet"},
        )

    assert json.loads((directory / recording.STATE_NAME).read_text())["session"] == "b-recording"


def test_a_jpeg_cut_inside_its_pixel_data_is_not_a_frame() -> None:
    cut = _APP_PIXELS[: len(_APP_PIXELS) - 40]  # headers intact, the scan truncated

    assert recording._valid_jpeg(_APP_PIXELS) is True
    assert recording._valid_jpeg(cut) is False


def test_a_caption_never_runs_into_the_next_one() -> None:
    entries = [recording.Entry(t=0.0, kind="frame", file="f", reason=None)]
    shown = [10.0]
    text = recording.srt_text(
        [(1.0, "click A"), (1.2, "click B"), (6.0, "click C")], entries, shown, 1.0, 10.0
    )

    cues = [block.splitlines()[1:3] for block in text.strip().split("\n\n")]
    ends = [c[0].split(" --> ")[1] for c in cues]
    begins = [c[0].split(" --> ")[0] for c in cues]
    assert all(end <= begins[i + 1] for i, end in enumerate(ends[:-1]))
    assert "00:00:01,000 --> 00:00:01,200\nclick A" in text  # cut where B begins
    assert "00:00:01,200 --> 00:00:04,200\nclick B" in text


def test_two_commands_at_the_same_instant_keep_the_last() -> None:
    entries = [recording.Entry(t=0.0, kind="frame", file="f", reason=None)]

    text = recording.srt_text([(2.0, "click A"), (2.0, "click B")], entries, [10.0], 1.0, 10.0)

    assert "click B" in text and "click A" not in text


def test_a_gif_that_cannot_hold_the_wait_is_refused_with_the_way_out(tmp_path: Path) -> None:
    entry = recording.Entry(t=0.0, kind="frame", file="f.jpg", reason=None)

    with pytest.raises(ValueError, match="--format mp4 or --trim-idle"):
        recording._encode_gif(
            [entry], [700.0], {"f.jpg": tmp_path / "f.jpg"}, {}, 64, 36, tmp_path / "o.gif"
        )


def test_a_gif_reports_no_captions_because_it_carries_none(tmp_path: Path) -> None:
    session = tmp_path / "s"
    _session_into(session)
    (session / "captions.jsonl").write_text(json.dumps({"t": 1.5, "text": "click A"}) + "\n")

    result = recording.encode_recording(session, tmp_path / "o.gif", fmt="gif")

    assert result["captions"] == 0


def test_a_clip_that_ends_says_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recording, "load_profile", lambda *args: SimpleNamespace())
    monkeypatch.setattr(recording, "attach", lambda *a, **k: SimpleNamespace(close=lambda: None))
    _ffmpeg_that_writes(monkeypatch)
    monkeypatch.setattr(
        recording,
        "run_recorder",
        lambda driver, session, **k: (_session_into(session), "browser closed")[1],
    )

    result = recording.clip_recording(_setup(tmp_path), "time-sheet", seconds=5)

    assert result["ended"] == "browser closed"


def test_the_time_limit_follows_a_clock_that_never_steps_back(tmp_path: Path) -> None:
    wall = iter([100.0, 50.0, 20.0, 10.0, 5.0, 1.0] * 20)  # the system clock goes backwards
    steady = iter(range(0, 10_000))

    why, _ = _run(
        tmp_path,
        _Driver([]),
        *([False] * 50),
        max_seconds=5.0,
        clock=lambda: next(wall),
        tick=lambda: float(next(steady)),
    )

    assert why == "time limit"


# --------------------------------------------------------------------------- review round 4


def test_a_second_stop_is_refused_while_the_first_writes_the_video(tmp_path: Path) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    state = {"pid": 999_999, "session": "s-recording", "started": 1.0, "task": "time-sheet"}
    recording._reserve_state(directory, state)

    recording._claim_export(directory, state)
    # the claim is ours, so asking again from this process is fine; another process is refused
    other = {**json.loads((directory / recording.STATE_NAME).read_text())}
    other["exporting"] = {"pid": os.getppid(), "process_started": None}
    (directory / recording.STATE_NAME).write_text(json.dumps(other))

    with pytest.raises(BrowserActionError, match="to be writing the video"):
        recording._claim_export(directory, state)


def test_a_failed_export_gives_the_session_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261120T000000000Z-recording"
    _session_into(session)
    _state(directory, session.name)
    monkeypatch.setattr(
        recording, "_finish", lambda *a, **k: (_ for _ in ()).throw(ValueError("taken"))
    )

    with pytest.raises(ValueError, match="taken"):
        recording.stop_recording(_setup(tmp_path), "time-sheet")

    kept = json.loads((directory / recording.STATE_NAME).read_text())
    assert "exporting" not in kept and session.exists()


def test_a_destination_inside_the_recordings_own_folder_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = _ffmpeg_that_writes(monkeypatch)
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261120T000000000Z-recording"
    _session_into(session)

    with pytest.raises(ValueError, match="outside the recording's own folder"):
        recording._finish(
            directory, session, task="time-sheet", fmt="mp4", out=session / "video.mp4",
            speed=1.0, trim_idle_s=None, keep_frames=False, started=0.0, now=lambda: 3.0,
            action="record-stop",
        )  # fmt: skip

    assert written == [] and session.exists()


def test_the_temporary_video_lives_in_the_reserved_session_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = _ffmpeg_that_writes(monkeypatch)
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261120T000000000Z-recording"
    _session_into(session)

    recording._finish(
        directory, session, task="time-sheet", fmt="mp4", out=tmp_path / "o.mp4",
        speed=1.0, trim_idle_s=None, keep_frames=True, started=0.0, now=lambda: 3.0,
        action="record-stop",
    )  # fmt: skip

    assert written[0].parent == session


def test_a_reused_pid_is_not_a_clip_that_is_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recording.os, "kill", lambda pid, sig: None)
    state = {"kind": "clip", "pid": 5, "process_started": "Mon Nov 16 09:00:00 2026"}

    monkeypatch.setattr(recording, "_process_started", lambda pid: "Mon Nov 16 09:00:00 2026")
    assert recording._worker_alive(state) is True
    monkeypatch.setattr(recording, "_process_started", lambda pid: "Fri Nov 20 15:00:00 2026")
    assert recording._worker_alive(state) is False  # the pid belongs to another process now


def test_a_reservation_that_cannot_be_written_is_not_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    real_dumps = recording.json.dumps
    monkeypatch.setattr(
        recording.json, "dumps", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(OSError, match="disk full"):
        recording._reserve_state(
            directory, {"pid": 0, "session": "s", "started": 1.0, "task": "time-sheet"}
        )

    monkeypatch.setattr(recording.json, "dumps", real_dumps)
    recording._reserve_state(  # and the next start can reserve again
        directory, {"pid": 0, "session": "s", "started": 1.0, "task": "time-sheet"}
    )


def test_a_gif_of_two_long_holds_is_refused_before_pillow_merges_them(tmp_path: Path) -> None:
    entries = [recording.Entry(t=0.0, kind="frame", file="f.jpg", reason=None)] * 2

    with pytest.raises(ValueError, match="--format mp4 or --trim-idle"):
        recording._encode_gif(
            entries, [350.0, 350.0], {"f.jpg": tmp_path / "f.jpg"}, {}, 64, 36, tmp_path / "o.gif"
        )


def test_a_frame_without_a_paint_time_is_stamped_when_it_arrived() -> None:
    from blumkin.browser import cdp

    assert cdp._paint_time({"metadata": {"timestamp": 12.5}}) == 12.5
    assert abs(cdp._paint_time({"metadata": {}}) - time.time()) < 5
    assert abs(cdp._paint_time({}) - time.time()) < 5


def test_the_end_of_the_journal_follows_elapsed_time_not_a_wall_clock_that_stepped_back(
    tmp_path: Path,
) -> None:
    wall = iter([100.0, 10.0, 5.0, 1.0] + [1.0] * 100)  # steps back after the first picture
    steady = iter(float(n) for n in range(1000))

    _run(
        tmp_path,
        _Driver([_Step()]),
        False,
        False,
        True,
        clock=lambda: next(wall),
        tick=lambda: next(steady),
    )

    entries, ended = recording.read_index(tmp_path)
    assert ended > entries[0].t + 1.0  # not clamped to a single instant


# --------------------------------------------------------------------------- review round 5


def test_the_cue_count_is_the_cues_not_the_arrows_in_their_text() -> None:
    entries = [recording.Entry(t=0.0, kind="frame", file="f", reason=None)]

    cues = recording.srt_cues([(1.0, "click A --> B")], entries, [10.0], 1.0, 10.0)

    assert len(cues) == 1


def test_a_gif_reports_the_seconds_it_plays(tmp_path: Path) -> None:
    from PIL import Image

    session = tmp_path / "s"
    (session / "frames").mkdir(parents=True)
    lines = []
    for number in range(5):
        Image.new("RGB", (64, 36), (number * 40, 2, 3)).save(
            session / "frames" / f"f{number}.jpg", format="JPEG"
        )
        lines.append(
            json.dumps({"file": f"frames/f{number}.jpg", "kind": "frame", "t": number * 0.01})
        )
    lines.append(json.dumps({"kind": "end", "t": 0.05}))
    (session / "index.jsonl").write_text("\n".join(lines) + "\n")

    result = recording.encode_recording(session, tmp_path / "o.gif", fmt="gif")

    assert result["seconds"] == pytest.approx(0.1)  # five 20 ms delays, not the 0.05 s asked for


def test_a_clip_that_fails_late_keeps_footage_that_has_pictures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "load_profile", lambda *args: SimpleNamespace())
    monkeypatch.setattr(recording.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(recording, "attach", lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        recording,
        "run_recorder",
        lambda driver, session, **k: (_session_into(session), "stopped")[1],
    )
    monkeypatch.setattr(
        recording, "_write_private", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(OSError, match="disk full"):
        recording.clip_recording(_setup(tmp_path), "time-sheet", seconds=5)

    directory = tmp_path / "captures" / "time-sheet"
    assert list(directory.glob("*-recording")) and (directory / recording.STATE_NAME).exists()


def test_captures_list_leaves_out_a_recording_in_progress(tmp_path: Path) -> None:
    from blumkin.browser import capture

    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261120T000000000Z-recording"
    _session_into(session)
    _state(directory, session.name)
    (directory / "20261119T000000000Z-screenshot.png").write_bytes(b"x")

    names = [Path(item["path"]).name for item in capture.list_captures(tmp_path, "time-sheet")]

    assert names == ["20261119T000000000Z-screenshot.png"]


def test_kept_frames_expire_together_never_in_part(tmp_path: Path) -> None:
    import os as _os

    from blumkin.browser import capture

    directory = recording.captures_dir(tmp_path, "time-sheet")
    bundle = directory / "20261110T000000000Z-recording"
    _session_into(bundle)
    old = time.time() - 30 * 3600
    _os.utime(bundle / "frames" / "f000001.jpg", (old, old))  # the first frame is old...
    # ...the index is new: the bundle is as new as its newest file

    capture.prune_captures(directory, 24 * 3600.0)

    assert (bundle / "frames" / "f000001.jpg").exists() and (bundle / "index.jsonl").exists()

    for member in bundle.rglob("*"):
        _os.utime(member, (old, old))
    capture.prune_captures(directory, 24 * 3600.0)

    assert not bundle.exists()


def test_a_caption_lookup_that_raises_never_fails_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from blumkin import cli

    monkeypatch.setattr(
        cli, "config_dir", lambda: (_ for _ in ()).throw(RuntimeError("cannot expand ~user"))
    )
    monkeypatch.setattr(
        cli,
        "click_task",
        lambda *a, **k: {
            "action": "click",
            "ok": True,
            "role": "button",
            "name": "Go",
            "task": "t",
        },
    )
    monkeypatch.setattr(cli, "load_browser_setup", lambda: SimpleNamespace())

    result = CliRunner().invoke(cli.main, ["browser", "click", "time-sheet", "Go", "--json"])

    assert result.exit_code == 0, result.output


def test_a_failure_to_write_the_anchor_does_not_stop_the_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(recording, "write_anchor", refuse)
    driver = _Driver([_Step(frames=(_shot(_NEXT_PIXELS, 102.0),))])

    why, lines = _run(tmp_path, driver, False, True)

    assert why == "stopped"
    assert [item["kind"] for item in lines] == ["frame", "frame", "end"]


def _stepping_clocks(driver: _Driver, *, step_at: int, step: float) -> tuple[Any, Any]:
    """Clocks that move 5 s a read, the wall one stepping by `step` from read `step_at` on."""

    def wall() -> float:
        return 1000.0 + 5.0 * driver.pumped + (step if driver.pumped >= step_at else 0.0)

    driver.keyframe_stamp = lambda: wall() - 899.0  # a keyframe is stamped with the wall clock
    return wall, lambda: 50.0 + 5.0 * driver.pumped


def test_frames_painted_after_a_forward_step_stay_on_the_steady_axis(tmp_path: Path) -> None:
    steps = [
        # read number n is the (2n-1)th, as a step takes two: painted then, on the wall clock
        _Step(frames=(_shot(_NEXT_PIXELS, 101.0 + 5 * (2 * n - 1) + (60.0 if n >= 3 else 0.0)),))
        for n in range(1, 5)
    ]
    driver = _Driver(steps)
    wall, steady = _stepping_clocks(driver, step_at=5, step=60.0)

    _, lines = _run(tmp_path, driver, *([False] * 7), True, clock=wall, tick=steady)

    frames = [item["t"] for item in lines if item["kind"] == "frame"]
    # The batch read as the step is seen (the third) is left out and a fresh picture is taken in
    # its place: no jump of 60 s anywhere.
    assert frames == [101.0, 106.0, 116.0, 126.0, 136.0]
    assert driver.keyframes == 2


def test_a_frame_painted_before_a_forward_step_but_read_after_it_is_not_misplaced(
    tmp_path: Path,
) -> None:
    steps = [
        _Step(frames=(_shot(_NEXT_PIXELS, 106.0),)),  # painted before the step, read after it
        _Step(frames=(_shot(_NEXT_PIXELS, 176.0),)),  # painted after it (116 on the axis)
    ]
    driver = _Driver(steps)
    wall, steady = _stepping_clocks(driver, step_at=1, step=60.0)

    _, lines = _run(tmp_path, driver, False, False, False, True, clock=wall, tick=steady)

    # Its batch is left out and a fresh picture (106 on the axis) is taken instead of it.
    assert [item["t"] for item in lines if item["kind"] == "frame"] == [101.0, 106.0, 116.0]


def test_a_backward_step_leaves_out_its_batch_and_does_not_freeze_the_frames_after_it(
    tmp_path: Path,
) -> None:
    steps = [
        _Step(frames=(_shot(_NEXT_PIXELS, 106.0),)),  # painted before the step, read after it
        _Step(frames=(_shot(_NEXT_PIXELS, 56.0),)),  # painted after it: 116 on the axis
    ]
    driver = _Driver(steps)
    wall, steady = _stepping_clocks(driver, step_at=1, step=-60.0)

    _, lines = _run(tmp_path, driver, False, False, False, True, clock=wall, tick=steady)

    assert [item["t"] for item in lines if item["kind"] == "frame"] == [101.0, 106.0, 116.0]


def test_a_stale_frame_read_after_a_backward_step_is_not_let_back_in(tmp_path: Path) -> None:
    """A frame painted before the keyframe (the page just left) must not pass a moved floor."""
    steps = [_Step(frames=(_shot(_LOGIN_PIXELS, 99.0),))]  # painted before the keyframe (101)
    driver = _Driver(steps)
    wall, steady = _stepping_clocks(driver, step_at=1, step=-60.0)

    _, lines = _run(tmp_path, driver, False, False, True, clock=wall, tick=steady)

    assert _LOGIN_PIXELS not in _stored(tmp_path)
    assert [item["t"] for item in lines if item["kind"] == "frame"] == [101.0, 106.0]


def test_a_clock_that_is_slewed_is_not_a_step(tmp_path: Path) -> None:
    """Drift of 0.2 s a read adds up past the step size, but no single look sees a step."""
    steps = [_Step(frames=(_shot(_NEXT_PIXELS, 101.0 + 5 * n),)) for n in range(1, 4)]
    driver = _Driver(steps)

    _, lines = _run(
        tmp_path,
        driver,
        False,
        False,
        False,
        False,
        False,
        True,
        clock=lambda: 1000.0 + 5.2 * driver.pumped,
        tick=lambda: 50.0 + 5.0 * driver.pumped,
    )

    assert driver.keyframes == 1  # no fresh picture was needed
    assert [item["t"] for item in lines if item["kind"] == "frame"] == [101.0, 106.0, 111.0, 116.0]
