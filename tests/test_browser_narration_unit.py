"""Narration for recordings (#816): the journal, the placement, the engine and the wiring."""

from __future__ import annotations

import json
import math
import shutil
import stat
import struct
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from PIL import Image

from blumkin.browser import narration, recording
from blumkin.browser.cdp import BrowserActionError
from blumkin.browser.narration import (
    Cue,
    NarrationError,
    NarrationOptions,
    Placement,
    Spoken,
    plan_cues,
)
from blumkin.cli import main

_REAL_CHECK_VOICE = narration.SayEngine.check_voice


@pytest.fixture(autouse=True)
def _no_dependence_on_this_machines_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests do not depend on the machine having ffmpeg or `say` (CI has neither).

    The voice check, which asks `say` for its voices, is switched off here; the tests of it call
    the real one (`_REAL_CHECK_VOICE`) against a listing they supply.
    """
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(narration.SayEngine, "check_voice", lambda self, voice: None)


def _spoken(cue: Cue, seconds: float) -> Spoken:
    return Spoken(cue=cue, path=Path("x.wav"), seconds=seconds)


def _identity(moment: float) -> float:
    return moment


def _write_tone(path: Path, seconds: float, rate: int = 24000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        count = round(seconds * rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", round(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(count)
            )
        )


class _FakeEngine:
    """A speech engine that makes one second of tone for every ten characters of text."""

    name = "say"
    default_voice = "Fake"

    def __init__(self) -> None:
        self.spoken: list[tuple[str, str, float]] = []

    def ensure_ready(self) -> None:
        return None

    def check_voice(self, voice: str) -> None:
        return None

    def synthesize(self, text: str, *, voice: str, speed: float, out: Path) -> None:
        self.spoken.append((text, voice, speed))
        _write_tone(out, max(len(text) / 10.0, 0.1) / speed)


# --------------------------------------------------------------------------- text and options


def test_a_cue_is_tidied_to_one_line_of_words() -> None:
    assert narration.validate_text("  Copying\n the   card\t now ") == "Copying the card now"


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_an_empty_cue_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="some text"):
        narration.validate_text(text)


def test_a_cue_with_a_control_character_is_refused() -> None:
    with pytest.raises(ValueError, match="control character"):
        narration.validate_text("hello\x07there")


def test_a_cue_over_the_length_limit_is_refused_with_the_way_out() -> None:
    with pytest.raises(ValueError, match="shorter cues"):
        narration.validate_text("a" * (narration.NARRATION_MAX_CHARS + 1))
    assert narration.validate_text("a" * narration.NARRATION_MAX_CHARS)


@pytest.mark.parametrize("voice", ["", "  ", "-v", "--rate", "x" * 61, "Bad\x00Voice"])
def test_a_voice_that_could_pass_for_an_option_or_is_malformed_is_refused(voice: str) -> None:
    with pytest.raises(ValueError, match="voice name"):
        narration.validate_voice(voice)


def test_a_voice_name_is_accepted_with_its_spaces() -> None:
    assert narration.validate_voice(" Eddy (English (US)) ") == "Eddy (English (US))"


@pytest.mark.parametrize("speed", [0.1, 3.0, float("nan"), float("inf")])
def test_a_speed_outside_the_range_is_refused(speed: float) -> None:
    with pytest.raises(ValueError, match="narration speed"):
        narration.validate_speed(speed)


def test_an_unknown_engine_is_refused_naming_the_known_ones() -> None:
    with pytest.raises(ValueError, match="engine of say"):
        narration.get_engine("festival")


# --------------------------------------------------------------------------- the journal


def test_a_cue_is_journalled_privately_in_order(tmp_path: Path) -> None:
    narration.record_cue(tmp_path, "second", now=lambda: 20.0)
    narration.record_cue(tmp_path, "first", now=lambda: 10.0, ahead=True)

    cues = narration.read_cues(tmp_path)

    assert [(c.t, c.text, c.ahead) for c in cues] == [
        (10.0, "first", True),
        (20.0, "second", False),
    ]
    assert stat.S_IMODE((tmp_path / narration.NARRATION_FILE).stat().st_mode) == 0o600


def test_a_torn_last_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    (tmp_path / narration.NARRATION_FILE).write_text(
        json.dumps({"t": 1.0, "text": "kept"}) + "\n" + '{"t": 2.0, "te'
    )

    assert [c.text for c in narration.read_cues(tmp_path)] == ["kept"]


def test_a_recording_holds_a_limited_number_of_cues(tmp_path: Path) -> None:
    for _ in range(narration.NARRATION_MAX_CUES):
        narration.record_cue(tmp_path, "x")

    with pytest.raises(ValueError, match="at most"):
        narration.record_cue(tmp_path, "one too many")


# --------------------------------------------------------------------------- the placement


def test_a_plain_cue_is_placed_at_the_moment_it_was_given() -> None:
    placements = plan_cues([_spoken(Cue(t=5.0, text="a"), 2.0)], [], _identity)

    assert placements == [Placement(planned=5.0, start=5.0, seconds=2.0)]


def test_a_cue_is_placed_through_the_videos_own_timeline() -> None:
    """Sped up or trimmed, the wall-clock moment maps to another position in the video."""
    placements = plan_cues([_spoken(Cue(t=10.0, text="a"), 1.0)], [], lambda moment: moment / 2)

    assert placements[0].start == 5.0


def test_an_ahead_cue_ends_just_before_the_next_command() -> None:
    cue = Cue(t=2.0, text="a", ahead=True)

    (placement,) = plan_cues([_spoken(cue, 3.0)], [1.0, 10.0, 12.0], _identity)

    assert placement.start == pytest.approx(10.0 - 3.0 - narration.AHEAD_LEAD_S)
    assert placement.late == 0


def test_an_ahead_cue_never_starts_before_it_was_given() -> None:
    cue = Cue(t=8.0, text="a", ahead=True)

    (placement,) = plan_cues([_spoken(cue, 5.0)], [9.0], _identity)  # the command is 1 s away

    assert placement.start == 8.0  # not 9 - 5 - 0.2 = 3.8: it cannot be heard before it exists


def test_an_ahead_cue_with_no_command_after_it_is_heard_when_it_was_given() -> None:
    (placement,) = plan_cues(
        [_spoken(Cue(t=4.0, text="a", ahead=True), 2.0)], [1.0, 3.0], _identity
    )

    assert placement.start == 4.0


def test_a_cue_that_cannot_start_on_time_waits_for_the_one_before_and_is_late() -> None:
    first = _spoken(Cue(t=0.0, text="a"), 3.0)
    second = _spoken(Cue(t=1.0, text="b"), 1.0)

    placements = plan_cues([first, second], [], _identity)

    assert [p.start for p in placements] == [0.0, 3.0]
    assert placements[1].late == 2.0 and placements[0].late == 0.0


def test_each_placement_stays_with_the_cue_it_was_made_for_whatever_order_they_play_in() -> None:
    """Placements come back aligned with the cues given, so a sound file keeps its own start."""
    given_first_plays_last = _spoken(Cue(t=9.0, text="b"), 1.0)
    given_last_plays_first = _spoken(Cue(t=1.0, text="a"), 2.0)

    placements = plan_cues([given_first_plays_last, given_last_plays_first], [], _identity)

    assert [p.start for p in placements] == [9.0, 1.0]  # index 0 is the cue given first
    assert [p.seconds for p in placements] == [1.0, 2.0]


def test_two_ahead_cues_before_the_same_command_keep_their_own_starts() -> None:
    """The longer one plans earlier; it must not swap start times with the shorter one."""
    short = _spoken(Cue(t=1.0, text="short", ahead=True), 1.0)
    long = _spoken(Cue(t=1.5, text="long", ahead=True), 4.0)

    placements = plan_cues([short, long], [10.0], _identity)

    # long plans for 10 - 4 - 0.2 = 5.8 and short for 10 - 1 - 0.2 = 8.8; each keeps its own
    assert placements[1].planned == pytest.approx(5.8) and placements[1].start == pytest.approx(5.8)
    assert placements[0].planned == pytest.approx(8.8) and placements[0].start == pytest.approx(9.8)
    assert placements[0].late == pytest.approx(1.0)  # queued behind the long one that ends at 9.8


def test_the_summary_counts_late_and_cut_cues_and_never_holds_a_word() -> None:
    placements = [
        Placement(planned=0.0, start=0.0, seconds=2.0),
        Placement(planned=1.0, start=2.0, seconds=2.0),  # late by 1 s
        Placement(planned=11.0, start=11.0, seconds=1.0),  # starts after the video ends
    ]

    told = narration.summarize(placements, total=10.0)

    assert told == {"cues": 3, "cut": 1, "late": 1, "max_late_s": 1.0, "spoken_s": 5.0}


def test_a_cue_that_starts_inside_the_video_but_runs_past_its_end_counts_as_cut() -> None:
    placements = [
        Placement(planned=4.0, start=4.0, seconds=8.0),  # heard only in part
        Placement(planned=1.0, start=1.0, seconds=2.0),  # whole
        Placement(planned=8.0, start=8.0, seconds=2.0),  # ends exactly at the end: not cut
    ]

    assert narration.summarize(placements, total=10.0)["cut"] == 1


def test_a_cue_late_by_less_than_the_tolerance_is_not_counted_late() -> None:
    placements = [Placement(planned=1.0, start=1.0 + narration.LATE_TOLERANCE_S / 2, seconds=1.0)]

    assert narration.summarize(placements, total=10.0)["late"] == 0


def test_the_overrun_is_how_far_the_last_cue_ends_past_the_video() -> None:
    inside = [Placement(planned=1.0, start=1.0, seconds=2.0)]
    outside = [Placement(planned=9.0, start=9.0, seconds=3.5)]

    assert narration.overrun(inside, total=10.0) == 0.0
    assert narration.overrun(outside, total=10.0) == pytest.approx(2.5)


# --------------------------------------------------------------------------- the filter graph


def test_the_mix_delays_each_cue_to_its_start_and_mixes_them_without_rescaling() -> None:
    graph = narration.mix_filter(
        [
            Placement(planned=0.0, start=0.0, seconds=1.0),
            Placement(planned=2.5, start=2.5, seconds=1.0),
        ],
        total=9.0,
    )

    assert "[0:a]aresample=24000" in graph and "adelay=0|0[c0]" in graph
    assert "adelay=2500|2500[c1]" in graph
    assert "[c0][c1]amix=inputs=2:normalize=0:duration=longest[mixed]" in graph
    assert graph.endswith("[mixed]apad=whole_dur=9.000[mix]")  # padded to the video's length


# --------------------------------------------------------------------------- the engine


def test_say_gets_the_text_on_stdin_and_never_on_its_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        seen.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    narration.SayEngine().synthesize(
        "the secret words", voice="Daniel", speed=1.2, out=tmp_path / "a.wav"
    )

    assert seen["kwargs"]["input"] == "the secret words"
    assert not any("secret" in part for part in seen["command"])  # not in the process list
    assert seen["command"][seen["command"].index("-v") + 1] == "Daniel"
    assert seen["command"][seen["command"].index("-r") + 1] == "210"  # 175 wpm at 1.2
    assert seen["command"][-2:] == ["-f", "-"]


def test_a_failed_say_reports_its_complaint_but_never_the_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stderr="Voice 'Nobody' not found\n"),
    )

    with pytest.raises(NarrationError) as excinfo:
        narration.SayEngine().synthesize(
            "private text", voice="Nobody", speed=1.0, out=tmp_path / "a"
        )

    assert "Nobody" in str(excinfo.value) and "private text" not in str(excinfo.value)


def test_say_missing_from_the_machine_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(NarrationError, match="macOS `say`"):
        narration.SayEngine().synthesize("x", voice="V", speed=1.0, out=tmp_path / "a")


def test_every_cue_is_spoken_into_a_private_file_with_its_length_measured(tmp_path: Path) -> None:
    engine = _FakeEngine()
    cues = [Cue(t=1.0, text="ten chars!"), Cue(t=2.0, text="twenty characters!!!")]

    spoken = narration.synthesize_cues(cues, engine, voice="V", speed=1.0, folder=tmp_path / "w")

    assert [round(s.seconds, 1) for s in spoken] == [1.0, 2.0]
    assert all(stat.S_IMODE(s.path.stat().st_mode) == 0o600 for s in spoken)
    assert stat.S_IMODE((tmp_path / "w").stat().st_mode) == 0o700


def test_a_file_the_engine_did_not_make_a_wav_of_is_an_error(tmp_path: Path) -> None:
    class Broken:
        name = "say"
        default_voice = "x"

        def ensure_ready(self) -> None:
            return None

        def check_voice(self, voice: str) -> None:
            return None

        def synthesize(self, text: str, *, voice: str, speed: float, out: Path) -> None:
            out.write_bytes(b"not a wav")

    with pytest.raises(NarrationError, match="WAV"):
        narration.synthesize_cues(
            [Cue(t=0.0, text="a")], Broken(), voice="x", speed=1.0, folder=tmp_path
        )


# --------------------------------------------------------------------------- the encode wiring


def _session(session: Path, seconds: float = 6.0) -> None:
    (session / "frames").mkdir(parents=True)
    Image.new("RGB", (64, 36), (1, 2, 3)).save(session / "frames" / "f000001.jpg", format="JPEG")
    (session / "index.jsonl").write_text(
        json.dumps({"file": "frames/f000001.jpg", "kind": "frame", "t": 100.0})
        + "\n"
        + json.dumps({"kind": "end", "t": 100.0 + seconds})
        + "\n"
    )


def _fake_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        commands.append(command)
        Path(command[-1]).write_bytes(b"media")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return commands


def _use_fake_engine(monkeypatch: pytest.MonkeyPatch) -> _FakeEngine:
    engine = _FakeEngine()
    monkeypatch.setattr(recording, "get_engine", lambda name: engine)
    return engine


def test_the_cues_are_spoken_mixed_and_muxed_into_the_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "s"
    _session(session)
    narration.record_cue(session, "Copying the card", now=lambda: 101.0)
    engine = _use_fake_engine(monkeypatch)
    commands = _fake_ffmpeg(monkeypatch)

    result = recording.encode_recording(session, tmp_path / "o.mp4", narration=NarrationOptions())

    mix, video = commands  # the audio is mixed first, then the video takes it as an input
    assert "-filter_complex" in mix and mix[mix.index("-c:a") + 1] == "aac"
    assert "-i" in video and video[video.index("-map") + 1] == "0:v"
    assert ["-map", "1:a", "-c:a", "copy"] == video[video.index("1:a") - 1 : video.index("1:a") + 3]
    assert result["narration"]["cues"] == 1 and result["narration"]["engine"] == "say"
    assert result["narration"]["voice"] == "Fake"  # the engine's own default
    assert engine.spoken == [("Copying the card", "Fake", 1.0)]


def test_the_audio_takes_the_input_after_the_subtitles_when_there_are_captions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "s"
    _session(session)
    (session / "captions.jsonl").write_text(json.dumps({"t": 101.0, "text": 'click "Add"'}) + "\n")
    narration.record_cue(session, "Adding a line", now=lambda: 101.5)
    _use_fake_engine(monkeypatch)
    commands = _fake_ffmpeg(monkeypatch)

    recording.encode_recording(session, tmp_path / "o.mp4", narration=NarrationOptions())

    video = commands[-1]
    inputs = [video[k + 1] for k, part in enumerate(video) if part == "-i"]
    maps = [video[k + 1] for k, part in enumerate(video) if part == "-map"]
    assert [Path(item).suffix for item in inputs] == [
        ".txt",
        ".srt",
        ".m4a",
    ]  # frames, captions, sound
    assert maps == ["0:v", "1:0", "2:a"]  # video, subtitles, then the audio from the third input


def test_each_sound_file_keeps_its_own_delay_when_the_cues_play_in_another_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two ahead cues before one command: the long one plays first, but input 0 is the short one."""
    session = tmp_path / "s"
    _session(session, seconds=12.0)
    (session / "captions.jsonl").write_text(json.dumps({"t": 110.0, "text": 'click "Add"'}) + "\n")
    narration.record_cue(session, "x" * 10, ahead=True, now=lambda: 101.0)  # 1 s of speech
    narration.record_cue(session, "y" * 40, ahead=True, now=lambda: 101.5)  # 4 s of speech
    _use_fake_engine(monkeypatch)
    commands = _fake_ffmpeg(monkeypatch)

    recording.encode_recording(session, tmp_path / "o.mp4", narration=NarrationOptions())

    graph = commands[0][commands[0].index("-filter_complex") + 1]
    assert "[0:a]aresample=24000,aformat=channel_layouts=mono,adelay=9800|9800[c0]" in graph
    assert "[1:a]aresample=24000,aformat=channel_layouts=mono,adelay=5800|5800[c1]" in graph


def test_the_voice_and_speed_the_caller_chose_reach_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "s"
    _session(session)
    narration.record_cue(session, "Hello there", now=lambda: 101.0)
    engine = _use_fake_engine(monkeypatch)
    _fake_ffmpeg(monkeypatch)

    result = recording.encode_recording(
        session, tmp_path / "o.mp4", narration=NarrationOptions(voice="Daniel", speed=0.8)
    )

    assert engine.spoken == [("Hello there", "Daniel", 0.8)]
    assert result["narration"]["voice"] == "Daniel"


def test_a_last_cue_that_runs_past_the_end_is_cut_unless_the_video_is_asked_to_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "s"
    _session(session, seconds=6.0)
    narration.record_cue(session, "x" * 80, now=lambda: 104.0)  # 8 s of speech from second 4
    _use_fake_engine(monkeypatch)
    commands = _fake_ffmpeg(monkeypatch)

    plain = recording.encode_recording(session, tmp_path / "a.mp4", narration=NarrationOptions())
    held = recording.encode_recording(
        session, tmp_path / "b.mp4", narration=NarrationOptions(hold=True)
    )

    assert plain["seconds"] == pytest.approx(6.0) and plain["narration"]["hold_s"] == 0.0
    assert held["seconds"] == pytest.approx(12.0)  # 4 + 8: the last frame held for the cue
    assert held["narration"]["hold_s"] == pytest.approx(6.0)
    video = commands[-1]
    assert video[video.index("-t") + 1] == "12.000"


def test_a_gif_has_no_audio_and_says_why_without_speaking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "s"
    _session(session)
    narration.record_cue(session, "a cue", now=lambda: 101.0)
    engine = _use_fake_engine(monkeypatch)

    result = recording.encode_recording(
        session, tmp_path / "o.gif", fmt="gif", narration=NarrationOptions()
    )

    assert result["narration"] == {"cues": 1, "skipped": "a GIF has no audio"}
    assert engine.spoken == []


def test_no_narration_leaves_the_cues_out_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "s"
    _session(session)
    narration.record_cue(session, "a cue", now=lambda: 101.0)
    engine = _use_fake_engine(monkeypatch)
    commands = _fake_ffmpeg(monkeypatch)

    result = recording.encode_recording(session, tmp_path / "o.mp4", narration=None)

    assert result["narration"] == {"cues": 1, "skipped": "narration was turned off"}
    assert engine.spoken == [] and len(commands) == 1  # only the video was written


def test_a_recording_without_cues_reports_no_narration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "s"
    _session(session)
    _fake_ffmpeg(monkeypatch)

    result = recording.encode_recording(session, tmp_path / "o.mp4", narration=NarrationOptions())

    assert "narration" not in result


def test_the_sound_files_are_removed_after_the_encode_even_when_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "s"
    _session(session)
    narration.record_cue(session, "a cue", now=lambda: 101.0)
    _use_fake_engine(monkeypatch)
    calls = {"n": 0}

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        calls["n"] += 1
        Path(command[-1]).write_bytes(b"media")
        return SimpleNamespace(returncode=0 if calls["n"] == 1 else 1, stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(BrowserActionError):
        recording.encode_recording(session, tmp_path / "o.mp4", narration=NarrationOptions())

    assert not (session / "narration").exists()  # the sound files and the mix go together


def test_a_narration_failure_is_a_browser_action_error_that_names_no_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "s"
    _session(session)
    narration.record_cue(session, "private words", now=lambda: 101.0)

    class Failing:
        name = "say"
        default_voice = "V"

        def ensure_ready(self) -> None:
            return None

        def check_voice(self, voice: str) -> None:
            return None

        def synthesize(self, text: str, *, voice: str, speed: float, out: Path) -> None:
            raise NarrationError("Expected `say` to speak a cue, got exit 1")

    monkeypatch.setattr(recording, "get_engine", lambda name: Failing())
    _fake_ffmpeg(monkeypatch)

    with pytest.raises(BrowserActionError) as excinfo:
        recording.encode_recording(session, tmp_path / "o.mp4", narration=NarrationOptions())

    assert "private words" not in str(excinfo.value)


def test_a_bad_option_is_refused_before_the_recorder_is_stopped(tmp_path: Path) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    (directory / recording.STATE_NAME).write_text(
        json.dumps({"pid": 4242, "session": "s-recording", "started": 1.0, "task": "t"})
    )

    with pytest.raises(ValueError, match="voice name"):
        recording.stop_recording(
            SimpleNamespace(config_dir=tmp_path, browser=SimpleNamespace(tasks={})),
            "time-sheet",
            narration=NarrationOptions(voice="--rate"),
        )

    assert (directory / recording.STATE_NAME).exists()  # nothing was stopped or cleared


# --------------------------------------------------------------------------- narrate


def _running_recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261123T000000000Z-recording"
    session.mkdir()
    (directory / recording.STATE_NAME).write_text(
        json.dumps({"pid": 4242, "session": session.name, "started": 1.0, "task": "t"})
    )
    monkeypatch.setattr(recording, "_worker_alive", lambda state: True)
    return session


def _setup(tmp_path: Path) -> Any:
    return SimpleNamespace(config_dir=tmp_path, browser=SimpleNamespace(tasks={}))


def test_narrate_adds_a_cue_to_the_running_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)

    first = recording.narrate_recording(_setup(tmp_path), "time-sheet", "One", now=lambda: 5.0)
    second = recording.narrate_recording(
        _setup(tmp_path), "time-sheet", "Two", ahead=True, now=lambda: 6.0
    )

    assert (first["cues"], second["cues"], second["ahead"]) == (1, 2, True)
    assert [(c.text, c.ahead) for c in narration.read_cues(session)] == [
        ("One", False),
        ("Two", True),
    ]


def test_narrate_with_no_recording_is_an_error_that_says_how_to_start_one(tmp_path: Path) -> None:
    with pytest.raises(BrowserActionError, match="record start time-sheet"):
        recording.narrate_recording(_setup(tmp_path), "time-sheet", "Hello")


def test_narrate_refuses_a_clip_and_a_recording_that_has_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)
    state_file = session.parent / recording.STATE_NAME
    state_file.write_text(
        json.dumps(
            {"kind": "clip", "pid": 4242, "session": session.name, "started": 1.0, "task": "t"}
        )
    )
    with pytest.raises(BrowserActionError, match="running recording"):
        recording.narrate_recording(_setup(tmp_path), "time-sheet", "Hello")

    state_file.write_text(
        json.dumps({"pid": 4242, "session": session.name, "started": 1.0, "task": "t"})
    )
    (session / "reason").write_text("stopped")  # the worker has finished
    with pytest.raises(BrowserActionError, match="has ended"):
        recording.narrate_recording(_setup(tmp_path), "time-sheet", "Hello")


def test_narrate_refuses_a_bad_text_before_looking_at_the_recording(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="some text"):
        recording.narrate_recording(_setup(tmp_path), "time-sheet", "   ")


def test_a_cue_the_journal_cannot_take_is_an_error_not_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)
    for _ in range(narration.NARRATION_MAX_CUES):
        narration.record_cue(session, "x")

    with pytest.raises(BrowserActionError, match="at most"):
        recording.narrate_recording(_setup(tmp_path), "time-sheet", "one more")


# --------------------------------------------------------------------------- the commands


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(main, ["--json", *args])


def test_the_narrate_command_hands_its_text_and_flag_to_the_recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake(config: Any, task: str, text: str, *, ahead: bool = False) -> dict[str, Any]:
        seen.update(task=task, text=text, ahead=ahead)
        return {"action": "narrate", "ahead": ahead, "cues": 3, "ok": True, "task": task}

    monkeypatch.setattr("blumkin.cli.narrate_recording", fake)
    monkeypatch.setattr("blumkin.cli.load_browser_setup", lambda: SimpleNamespace())

    result = _invoke("browser", "narrate", "time-sheet", "About to click", "--ahead")

    assert result.exit_code == 0, result.output
    assert seen == {"task": "time-sheet", "text": "About to click", "ahead": True}
    assert json.loads(result.output)["cues"] == 3


def test_record_stop_turns_its_options_into_narration_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake(config: Any, task: str, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr("blumkin.cli.stop_recording", fake)
    monkeypatch.setattr("blumkin.cli.load_browser_setup", lambda: SimpleNamespace())

    _invoke(
        "browser", "record", "stop", "time-sheet", "--voice", "Daniel",
        "--narration-speed", "0.9", "--hold-for-narration",
    )  # fmt: skip
    assert seen["narration"] == NarrationOptions(engine="say", voice="Daniel", speed=0.9, hold=True)

    _invoke("browser", "record", "stop", "time-sheet", "--no-narration")
    assert seen["narration"] is None

    _invoke("browser", "record", "stop", "time-sheet")
    assert seen["narration"] == NarrationOptions()  # on by default


def test_a_narration_speed_outside_the_range_is_a_usage_error() -> None:
    result = _invoke("browser", "record", "stop", "time-sheet", "--narration-speed", "5")

    assert result.exit_code == 2


def test_the_text_note_counts_cues_without_saying_what_was_said() -> None:
    from blumkin import cli

    told = {
        "cues": 3, "cut": 1, "engine": "say", "hold_s": 0.0, "late": 2, "max_late_s": 1.5,
        "spoken_s": 9.0, "voice": "Samantha",
    }  # fmt: skip

    (line,) = cli._narration_notes(told)

    assert "3 cue(s)" in line and "late by up to 1.5" in line and "1 cut off" in line
    assert cli._narration_notes({"cues": 2, "skipped": "a GIF has no audio"}) == [
        "  narration: 2 cue(s) left out (a GIF has no audio)"
    ]
    assert cli._narration_notes(None) == []


# --------------------------------------------------------------------------- stop and narrate


def _state_of(session: Path) -> dict[str, Any]:
    return json.loads((session.parent / recording.STATE_NAME).read_text())


def test_stop_marks_the_recording_as_stopping_so_a_late_cue_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)
    _session(session)
    monkeypatch.setattr(recording.os, "kill", lambda pid, sig: None)
    seen: dict[str, Any] = {}

    def fake_finish(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen["state"] = _state_of(session)
        # a `narrate` that arrives while the encode runs must be refused, not lost
        try:
            recording.narrate_recording(_setup(tmp_path), "time-sheet", "too late")
        except BrowserActionError as exc:
            seen["refused"] = str(exc)
        return {"ok": True}

    monkeypatch.setattr(recording, "_finish", fake_finish)
    monkeypatch.setattr(recording, "_worker_alive", lambda state: state.get("stopping") is None)

    recording.stop_recording(_setup(tmp_path), "time-sheet")

    assert seen["state"]["stopping"] is True
    assert "being stopped" in seen["refused"]
    assert narration.read_cues(session) == []  # the late cue was refused, never half-accepted


def test_a_cue_is_accepted_before_stop_and_is_in_the_journal_the_encode_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)
    _session(session)
    recording.narrate_recording(_setup(tmp_path), "time-sheet", "Accepted", now=lambda: 101.0)
    monkeypatch.setattr(recording.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(recording, "_worker_alive", lambda state: state.get("stopping") is None)
    seen: dict[str, Any] = {}

    def fake_finish(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen["cues"] = [c.text for c in narration.read_cues(session)]
        return {"ok": True}

    monkeypatch.setattr(recording, "_finish", fake_finish)

    recording.stop_recording(_setup(tmp_path), "time-sheet")

    assert seen["cues"] == ["Accepted"]


def test_narrate_appends_under_the_task_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _running_recording(tmp_path, monkeypatch)
    held: list[str] = []
    real_record = recording.record_cue

    class _Lock:
        def __enter__(self) -> None:
            held.append("lock")

        def __exit__(self, *exc: Any) -> None:
            held.append("unlock")

    def record(*args: Any, **kwargs: Any) -> int:
        held.append("append")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(recording, "task_lock", lambda directory: _Lock())
    monkeypatch.setattr(recording, "record_cue", record)

    recording.narrate_recording(_setup(tmp_path), "time-sheet", "Hello")

    assert held == ["lock", "append", "unlock"]


def test_narration_that_cannot_be_made_is_refused_before_the_recorder_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)
    _session(session)
    narration.record_cue(session, "a cue", now=lambda: 101.0)
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "say" else f"/usr/bin/{name}")
    signals: list[int] = []
    monkeypatch.setattr(recording.os, "kill", lambda pid, sig: signals.append(sig))

    with pytest.raises(BrowserActionError, match="macOS `say`"):
        recording.stop_recording(_setup(tmp_path), "time-sheet")

    assert signals == []  # no worker was signalled
    assert "stopping" not in _state_of(session)  # and the recording still takes cues
    recording.narrate_recording(_setup(tmp_path), "time-sheet", "still open")


def test_a_missing_ffmpeg_is_refused_early_with_the_way_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)
    _session(session)
    narration.record_cue(session, "a cue", now=lambda: 101.0)
    monkeypatch.setattr(
        shutil, "which", lambda name: None if name == "ffmpeg" else f"/usr/bin/{name}"
    )

    with pytest.raises(BrowserActionError, match="--format gif"):
        recording.stop_recording(_setup(tmp_path), "time-sheet")


def test_a_recording_with_no_cues_needs_neither_say_nor_ffmpeg_to_be_stopped_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)
    _session(session)
    monkeypatch.setattr(shutil, "which", lambda name: None)  # nothing is installed
    monkeypatch.setattr(recording.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(recording, "_worker_alive", lambda state: state.get("stopping") is None)
    monkeypatch.setattr(recording, "_finish", lambda *a, **k: {"ok": True})

    assert recording.stop_recording(_setup(tmp_path), "time-sheet") == {"ok": True}


def test_the_narration_text_never_reaches_a_caption() -> None:
    """`narrate` is a browser command, which captions a running recording: it must caption none."""
    assert recording.caption_for("narrate", {"text": "private words", "ahead": True}) is None


def test_narrating_through_the_cli_writes_no_caption_with_the_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("blumkin.cli.load_browser_setup", lambda: _setup(tmp_path))

    result = _invoke("browser", "narrate", "time-sheet", "private words")

    assert result.exit_code == 0, result.output
    assert not (session / "captions.jsonl").exists()  # nothing was captioned
    assert [c.text for c in narration.read_cues(session)] == ["private words"]


# --------------------------------------------------------------------------- review round


def test_a_gif_stop_needs_neither_say_nor_ffmpeg_even_when_the_recording_has_cues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)
    _session(session)
    narration.record_cue(session, "a cue", now=lambda: 101.0)
    monkeypatch.setattr(shutil, "which", lambda name: None)  # no say, no ffmpeg
    monkeypatch.setattr(recording.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(recording, "_worker_alive", lambda state: state.get("stopping") is None)
    monkeypatch.setattr(recording, "_finish", lambda *a, **k: {"ok": True})

    assert recording.stop_recording(_setup(tmp_path), "time-sheet", fmt="gif") == {"ok": True}


def test_a_recorder_that_survives_the_stop_keeps_taking_cues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)  # the worker is alive and stays alive
    _session(session)
    monkeypatch.setattr(recording.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(recording.time, "sleep", lambda seconds: None)
    clock = iter(range(0, 10_000, 40))
    monkeypatch.setattr(recording.time, "monotonic", lambda: float(next(clock)))

    with pytest.raises(BrowserActionError, match="still runs"):
        recording.stop_recording(_setup(tmp_path), "time-sheet")

    assert "stopping" not in _state_of(session)  # the mark was taken off again
    recording.narrate_recording(_setup(tmp_path), "time-sheet", "still recording")  # accepted


def test_a_failed_export_leaves_neither_the_export_nor_the_stopping_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)
    _session(session)
    monkeypatch.setattr(recording.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(recording, "_worker_alive", lambda state: state.get("stopping") is None)

    def fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise BrowserActionError("encode failed")

    monkeypatch.setattr(recording, "_finish", fail)

    with pytest.raises(BrowserActionError, match="encode failed"):
        recording.stop_recording(_setup(tmp_path), "time-sheet")

    state = _state_of(session)
    assert "stopping" not in state and "exporting" not in state  # `record stop` can run again


def test_a_nan_narration_speed_is_refused_with_a_message_that_is_about_a_speed() -> None:
    result = CliRunner().invoke(
        main, ["browser", "record", "stop", "time-sheet", "--narration-speed", "nan"]
    )

    assert result.exit_code == 2
    assert "seconds" not in result.output and "got nan" in result.output


def test_holding_the_last_frame_for_narration_does_not_move_a_caption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the idle stretch trimmed to 2 s, a command at second 3 sits at the end of the video."""
    session = tmp_path / "s"
    _session(session, seconds=10.0)
    (session / "captions.jsonl").write_text(json.dumps({"t": 103.0, "text": 'click "Add"'}) + "\n")
    narration.record_cue(session, "z" * 40, now=lambda: 101.0)  # a 4 s cue from second 1
    _use_fake_engine(monkeypatch)
    _fake_ffmpeg(monkeypatch)

    plain = recording.encode_recording(
        session, tmp_path / "a.mp4", trim_idle_s=2.0, narration=NarrationOptions()
    )
    assert plain["seconds"] == pytest.approx(2.0)
    assert not (session / "captions.srt").exists()  # at the very end of the trimmed video: no room
    held = recording.encode_recording(
        session, tmp_path / "b.mp4", trim_idle_s=2.0, narration=NarrationOptions(hold=True)
    )
    srt = (session / "captions.srt").read_text()

    assert held["seconds"] == pytest.approx(5.0)  # the last frame was held for the cue
    assert "00:00:02,000 -->" in srt  # where the trimmed session put it...
    assert "00:00:03,000 -->" not in srt  # ...not at 3 s, where a stretched last frame would


# --------------------------------------------------------------------------- round three


def test_a_cue_after_a_torn_last_entry_is_not_swallowed_by_it(tmp_path: Path) -> None:
    (tmp_path / narration.NARRATION_FILE).write_text(
        json.dumps({"t": 1.0, "text": "kept"}) + "\n" + '{"t": 2.0, "te'  # cut short, no newline
    )

    narration.record_cue(tmp_path, "accepted", now=lambda: 3.0)

    assert [c.text for c in narration.read_cues(tmp_path)] == ["kept", "accepted"]


def test_a_cue_after_a_whole_last_entry_adds_no_blank_line(tmp_path: Path) -> None:
    narration.record_cue(tmp_path, "one", now=lambda: 1.0)
    narration.record_cue(tmp_path, "two", now=lambda: 2.0)

    lines = (tmp_path / narration.NARRATION_FILE).read_text().splitlines()

    assert len(lines) == 2 and all(line.startswith("{") for line in lines)


_SAY_LISTING = (
    "Albert              en_US    # Hello! My name is Albert.\n"
    "Eddy (English (UK)) en_GB    # Hello! My name is Eddy.\n"
    "Samantha (English (US)) en_US    # Hello! My name is Samantha.\n"
    "Ava (Premium)       en_US    # Hello! My name is Ava.\n"
    "Majed               ar_001   # Hello! My name is Majed.\n"
    "Fiona               en_GB-u-sd-gbsct    # Hello! My name is Fiona.\n"
    "Moira               en-scotland    # Hello! My name is Moira.\n"
)


def _say_lists(monkeypatch: pytest.MonkeyPatch, out: str, code: int = 0) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=code, stdout=out, stderr="")
    )


@pytest.mark.parametrize(
    "voice",
    [
        "Samantha",
        "samantha",
        "Samantha (English (US))",
        "Eddy",
        "Eddy (English (UK))",
        "Ava (Premium)",
        "Majed",
        "Fiona",
        "Moira",
    ],
)
def test_a_voice_say_lists_is_accepted_whatever_its_case_or_spaces(
    monkeypatch: pytest.MonkeyPatch, voice: str
) -> None:
    _say_lists(monkeypatch, _SAY_LISTING)

    _REAL_CHECK_VOICE(narration.SayEngine(), voice)  # no error


def test_every_line_of_a_listing_is_read_whatever_shape_its_locale_has() -> None:
    names = [
        m.group(1)
        for line in _SAY_LISTING.splitlines()
        if (m := narration._SAY_VOICE_LINE.match(line))
    ]

    assert names == [
        "Albert",
        "Eddy (English (UK))",
        "Samantha (English (US))",
        "Ava (Premium)",
        "Majed",
        "Fiona",
        "Moira",
    ]


def test_a_voice_say_does_not_have_is_refused_with_how_to_add_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _say_lists(monkeypatch, _SAY_LISTING)

    with pytest.raises(NarrationError, match="Spoken Content"):
        _REAL_CHECK_VOICE(narration.SayEngine(), "Nobody")


@pytest.mark.parametrize("failure", ["empty", "nonzero", "timeout"])
def test_a_voice_listing_that_cannot_be_read_blocks_nothing(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "timeout":

        def hang(*args: Any, **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired("say", 10)

        monkeypatch.setattr(subprocess, "run", hang)
    else:
        _say_lists(
            monkeypatch,
            "" if failure == "empty" else _SAY_LISTING,
            code=0 if failure == "empty" else 1,
        )

    # the later export reports it if it is wrong
    _REAL_CHECK_VOICE(narration.SayEngine(), "AnythingAtAll")


def test_an_unavailable_voice_is_refused_before_the_recorder_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)
    _session(session)
    narration.record_cue(session, "a cue", now=lambda: 101.0)

    class NoSuchVoice(_FakeEngine):
        def check_voice(self, voice: str) -> None:
            raise NarrationError(f"Expected a voice `say` has installed, got {voice!r}")

    monkeypatch.setattr(recording, "get_engine", lambda name: NoSuchVoice())
    signals: list[int] = []
    monkeypatch.setattr(recording.os, "kill", lambda pid, sig: signals.append(sig))

    with pytest.raises(BrowserActionError, match="Nobody"):
        recording.stop_recording(
            _setup(tmp_path), "time-sheet", narration=NarrationOptions(voice="Nobody")
        )

    assert signals == [] and "stopping" not in _state_of(session)  # still recording
    recording.narrate_recording(_setup(tmp_path), "time-sheet", "still open")


def test_the_engines_default_voice_is_what_the_preflight_checks_when_none_was_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)
    _session(session)
    narration.record_cue(session, "a cue", now=lambda: 101.0)
    checked: list[str] = []

    class Recording(_FakeEngine):
        def check_voice(self, voice: str) -> None:
            checked.append(voice)

    monkeypatch.setattr(recording, "get_engine", lambda name: Recording())
    monkeypatch.setattr(recording.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(recording, "_worker_alive", lambda state: state.get("stopping") is None)
    monkeypatch.setattr(recording, "_finish", lambda *a, **k: {"ok": True})

    recording.stop_recording(_setup(tmp_path), "time-sheet")

    assert checked == ["Fake"]


# --------------------------------------------------------------------------- round four


def test_the_way_out_of_a_missing_ffmpeg_does_not_ask_for_ffmpeg() -> None:
    """`--no-narration` still writes an MP4, which needs ffmpeg: the hint must be a GIF."""
    from blumkin.browser.recording import _require_narration_ready

    with pytest.raises(BrowserActionError) as excinfo:
        original = shutil.which
        shutil.which = lambda name: None if name == "ffmpeg" else f"/usr/bin/{name}"  # type: ignore[assignment]
        try:
            _require_narration_ready(NarrationOptions())
        finally:
            shutil.which = original  # type: ignore[assignment]

    assert "--format gif" in str(excinfo.value) and "--no-narration" not in str(excinfo.value)


def test_a_mix_that_times_out_leaves_no_partial_audio_and_keeps_the_cues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "s"
    _session(session)
    narration.record_cue(session, "a cue", now=lambda: 101.0)
    _use_fake_engine(monkeypatch)

    def hang(command: list[str], **kwargs: Any) -> Any:
        Path(command[-1]).write_bytes(b"partial")  # ffmpeg got as far as creating the file
        raise subprocess.TimeoutExpired("ffmpeg", 300)

    monkeypatch.setattr(subprocess, "run", hang)

    with pytest.raises(BrowserActionError):
        recording.encode_recording(session, tmp_path / "o.mp4", narration=NarrationOptions())

    assert not (session / "narration").exists()  # the folder the partial file was in is gone
    assert not list(session.rglob("*.m4a"))  # and no partial audio is left anywhere
    assert [c.text for c in narration.read_cues(session)] == ["a cue"]  # so the export can retry


def test_an_interrupt_before_the_worker_is_signalled_takes_the_stopping_mark_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)  # a live worker that stays alive
    _session(session)

    def interrupted(pid: int, sig: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(recording.os, "kill", interrupted)

    with pytest.raises(KeyboardInterrupt):
        recording.stop_recording(_setup(tmp_path), "time-sheet")

    assert "stopping" not in _state_of(session)
    recording.narrate_recording(_setup(tmp_path), "time-sheet", "still recording")  # accepted


def test_an_interrupt_leaves_the_mark_alone_when_the_recorder_has_already_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)
    _session(session)
    monkeypatch.setattr(recording.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(recording, "_worker_alive", lambda state: state.get("stopping") is None)

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(recording, "_claim_export", interrupted)

    with pytest.raises(KeyboardInterrupt):
        recording.stop_recording(_setup(tmp_path), "time-sheet")

    assert _state_of(session)["stopping"] is True  # the recorder is over: nothing to reopen


def test_an_interrupt_never_clears_a_mark_this_stop_did_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _running_recording(tmp_path, monkeypatch)
    _state_with = {
        "pid": 4242,
        "session": session.name,
        "started": 1.0,
        "task": "t",
        "stopping": True,
    }
    (session.parent / recording.STATE_NAME).write_text(json.dumps(_state_with))  # another stop's
    _session(session)

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(recording, "_require_narration_ready", interrupted)
    narration.record_cue(session, "a cue", now=lambda: 101.0)

    with pytest.raises(KeyboardInterrupt):
        recording.stop_recording(_setup(tmp_path), "time-sheet")  # interrupted before it marked

    assert _state_of(session)["stopping"] is True  # the other stop's mark is still there


def test_the_cue_journal_goes_even_when_the_frames_are_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261123T000000000Z-recording"
    _session(session)
    narration.record_cue(session, "private words", now=lambda: 101.0)
    _use_fake_engine(monkeypatch)
    _fake_ffmpeg(monkeypatch)

    result = recording._finish(
        directory, session, task="time-sheet", fmt="mp4", out=tmp_path / "o.mp4", speed=1.0,
        trim_idle_s=None, keep_frames=True, started=0.0, now=lambda: 3.0, action="record-stop",
        narration=NarrationOptions(),
    )  # fmt: skip

    assert result["frames_dir"] == str(session) and (session / "frames").exists()
    assert not (session / narration.NARRATION_FILE).exists()  # the words are not kept


def test_the_cue_journal_goes_with_kept_frames_even_when_narration_was_turned_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261123T000000000Z-recording"
    _session(session)
    narration.record_cue(session, "private words", now=lambda: 101.0)
    _fake_ffmpeg(monkeypatch)

    recording._finish(
        directory, session, task="time-sheet", fmt="mp4", out=tmp_path / "o.mp4", speed=1.0,
        trim_idle_s=None, keep_frames=True, started=0.0, now=lambda: 3.0, action="record-stop",
        narration=None,
    )  # fmt: skip

    assert not (session / narration.NARRATION_FILE).exists()


def test_the_cue_journal_is_kept_when_publishing_fails_so_the_stop_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = recording.captures_dir(tmp_path, "time-sheet")
    session = directory / "20261123T000000000Z-recording"
    _session(session)
    narration.record_cue(session, "a cue", now=lambda: 101.0)
    target = tmp_path / "o.mp4"
    target.write_bytes(b"already here")  # the destination exists: the publish is refused

    with pytest.raises(ValueError):
        recording._finish(
            directory, session, task="time-sheet", fmt="mp4", out=target, speed=1.0,
            trim_idle_s=None, keep_frames=True, started=0.0, now=lambda: 3.0,
            action="record-stop", narration=NarrationOptions(),
        )  # fmt: skip

    assert [c.text for c in narration.read_cues(session)] == ["a cue"]
