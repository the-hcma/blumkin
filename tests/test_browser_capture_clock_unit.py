"""One capture clock for frames, cards, captions and narration cues (#821)."""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from blumkin.browser import capture_clock, narration, recording
from blumkin.browser.capture_clock import capture_now, read_anchor, write_anchor


def test_without_an_anchor_it_is_the_wall_clock(tmp_path: Path) -> None:
    assert capture_now(tmp_path, wall=lambda: 123.0, steady=lambda: 5.0) == 123.0


def test_the_anchor_is_written_whole_and_private_and_read_back(tmp_path: Path) -> None:
    write_anchor(tmp_path, 1000.5, 42.25)

    assert read_anchor(tmp_path) == (1000.5, 42.25)
    assert stat.S_IMODE((tmp_path / capture_clock.CLOCK_NAME).stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [capture_clock.CLOCK_NAME]  # no scratch left


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"t": 1}', '{"t": "x", "steady": 2}'])
def test_a_missing_or_damaged_anchor_is_no_anchor(tmp_path: Path, text: str) -> None:
    (tmp_path / capture_clock.CLOCK_NAME).write_text(text)
    assert read_anchor(tmp_path) is None
    assert capture_now(tmp_path, wall=lambda: 7.0, steady=lambda: 1.0) == 7.0


def test_a_wall_clock_that_steps_does_not_move_the_stamp(tmp_path: Path) -> None:
    write_anchor(tmp_path, 1000.0, 50.0)

    # 20 s of steady time later the wall clock says 60 s more than it should (a forward step)
    stamp = capture_now(tmp_path, wall=lambda: 1080.0, steady=lambda: 70.0)

    assert stamp == pytest.approx(1020.0)


def test_a_steady_clock_that_restarted_falls_back_to_the_wall_clock(tmp_path: Path) -> None:
    write_anchor(tmp_path, 1000.0, 5000.0)  # recorded on an earlier boot

    assert capture_now(tmp_path, wall=lambda: 9999.0, steady=lambda: 12.0) == 9999.0


def _running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261123T000000000Z-recording"
    session.mkdir()
    (directory / recording.STATE_NAME).write_text(
        json.dumps({"pid": 4242, "session": session.name, "started": 1.0, "task": "t"})
    )
    monkeypatch.setattr(recording, "_worker_alive", lambda state: True)
    return session


def _step_the_clocks(monkeypatch: pytest.MonkeyPatch, *, wall: float, steady: float) -> None:
    monkeypatch.setattr(time, "time", lambda: wall)
    monkeypatch.setattr(time, "monotonic", lambda: steady)


def test_a_cue_added_after_a_wall_clock_step_is_stamped_on_the_recordings_axis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running(tmp_path, monkeypatch)
    write_anchor(session, 1000.0, 50.0)
    _step_the_clocks(monkeypatch, wall=1080.0, steady=70.0)  # 20 s in, wall stepped +60 s
    config = SimpleNamespace(config_dir=tmp_path)

    recording.narrate_recording(config, "time-sheet", "A cue")

    (cue,) = narration.read_cues(session)
    assert cue.t == pytest.approx(1020.0)


def test_a_caption_added_after_a_wall_clock_step_is_stamped_on_the_recordings_axis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running(tmp_path, monkeypatch)
    write_anchor(session, 1000.0, 50.0)
    _step_the_clocks(monkeypatch, wall=920.0, steady=75.0)  # 25 s in, wall stepped back 105 s

    recording.note_command(tmp_path, "time-sheet", "click", {"name": "Add", "role": "button"})

    (line,) = (session / "captions.jsonl").read_text().splitlines()
    assert json.loads(line)["t"] == pytest.approx(1025.0)


def test_before_the_anchor_exists_a_cue_and_a_caption_use_the_wall_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running(tmp_path, monkeypatch)
    _step_the_clocks(monkeypatch, wall=555.0, steady=3.0)
    config = SimpleNamespace(config_dir=tmp_path)

    recording.narrate_recording(config, "time-sheet", "Early")
    recording.note_command(tmp_path, "time-sheet", "click", {"name": "Add", "role": "button"})

    assert narration.read_cues(session)[0].t == 555.0
    assert json.loads((session / "captions.jsonl").read_text().splitlines()[0])["t"] == 555.0


def test_a_cue_after_a_step_still_lands_inside_the_video_it_was_given_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug: a static page, a +60 s wall step, a cue 20 s in, a 30 s video: the cue was cut."""
    session = _running(tmp_path, monkeypatch)
    (session / "index.jsonl").write_text(
        json.dumps({"file": "frames/f000001.jpg", "kind": "frame", "t": 1000.0})
        + "\n"
        + json.dumps({"kind": "end", "t": 1030.0})  # the steady clock's end, 30 s later
        + "\n"
    )
    write_anchor(session, 1000.0, 50.0)
    _step_the_clocks(monkeypatch, wall=1080.0, steady=70.0)
    recording.narrate_recording(SimpleNamespace(config_dir=tmp_path), "time-sheet", "Mid way")

    entries, ended = recording.read_index(session)
    shown = recording.durations(entries, ended)
    (cue,) = narration.read_cues(session)
    placed = recording.map_time(cue.t, entries, shown, 1.0)

    assert placed == pytest.approx(20.0)
    assert placed < sum(shown)


def test_an_explicit_clock_still_wins_for_a_caller_that_passes_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running(tmp_path, monkeypatch)
    write_anchor(session, 1000.0, 50.0)
    config: Any = SimpleNamespace(config_dir=tmp_path)

    recording.narrate_recording(config, "time-sheet", "Fixed", now=lambda: 5.0)

    assert narration.read_cues(session)[0].t == 5.0
