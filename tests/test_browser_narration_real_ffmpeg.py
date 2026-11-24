"""Narration through the real ffmpeg (#816): the audio track exists and is as long as the video.

Skipped where ffmpeg or ffprobe is not installed (the hermetic CI has neither);
`test_browser_narration_unit.py` covers the same logic with a fake ffmpeg.
"""

from __future__ import annotations

import json
import math
import shutil
import stat
import struct
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image

from blumkin.browser import narration, recording
from blumkin.browser.narration import NarrationOptions

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are needed to make and check the video",
)


class _ToneEngine:
    """A speech engine that makes one second of tone for every ten characters of text."""

    name = "say"
    default_voice = "Fake"

    def ensure_ready(self) -> None:
        return None

    def check_voice(self, voice: str) -> None:
        return None

    def synthesize(self, text: str, *, voice: str, speed: float, out: Path) -> None:
        rate = 24000
        with wave.open(str(out), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            frames = round(max(len(text) / 10.0, 0.1) / speed * rate)
            handle.writeframes(
                b"".join(
                    struct.pack("<h", round(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                    for i in range(frames)
                )
            )


def _session(session: Path, seconds: float) -> None:
    (session / "frames").mkdir(parents=True)
    Image.new("RGB", (64, 36), (1, 2, 3)).save(session / "frames" / "f000001.jpg", format="JPEG")
    (session / "index.jsonl").write_text(
        json.dumps({"file": "frames/f000001.jpg", "kind": "frame", "t": 100.0})
        + "\n"
        + json.dumps({"kind": "end", "t": 100.0 + seconds})
        + "\n"
    )


def _streams(path: Path) -> list[dict[str, str]]:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return json.loads(probe.stdout)["streams"]


def test_a_real_encode_has_one_audio_stream_as_long_as_the_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "s"
    _session(session, seconds=6.0)
    narration.record_cue(session, "x" * 20, now=lambda: 101.0)  # 2 s of tone from second 1
    narration.record_cue(session, "y" * 20, now=lambda: 103.0)  # 2 s from second 3
    monkeypatch.setattr(recording, "get_engine", lambda name, config_dir=None: _ToneEngine())
    target = tmp_path / "o.mp4"

    result = recording.encode_recording(session, target, narration=NarrationOptions())

    streams = _streams(target)
    audio = [s for s in streams if s["codec_type"] == "audio"]
    video = [s for s in streams if s["codec_type"] == "video"]
    assert len(audio) == 1 and len(video) == 1
    assert abs(float(audio[0]["duration"]) - float(video[0]["duration"])) < 0.3
    assert result["narration"]["cues"] == 2 and result["narration"]["late"] == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_held_video_is_as_long_as_the_narration_and_its_audio_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "s"
    _session(session, seconds=3.0)
    narration.record_cue(session, "z" * 50, now=lambda: 102.0)  # 5 s of tone from second 2
    monkeypatch.setattr(recording, "get_engine", lambda name, config_dir=None: _ToneEngine())
    target = tmp_path / "o.mp4"

    result = recording.encode_recording(session, target, narration=NarrationOptions(hold=True))

    streams = _streams(target)
    durations = {s["codec_type"]: float(s["duration"]) for s in streams}
    assert result["seconds"] == pytest.approx(7.0, abs=0.05)  # 2 s of session, then the 5 s cue
    assert durations["video"] == pytest.approx(7.0, abs=0.3)
    assert durations["audio"] == pytest.approx(7.0, abs=0.3)
