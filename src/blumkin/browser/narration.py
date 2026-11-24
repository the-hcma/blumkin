"""Narration for recordings (#816): spoken cues laid over the video as an audio track.

A cue is the agent's own words about a step, never read off the page. `blumkin browser
narrate TASK "text"` appends one to the running recording's session (`narration.jsonl`, owner
only); `record stop` speaks every cue with an offline engine, places each one on the video's
timeline, mixes them into one AAC track and muxes it into the MP4.

The module has three parts that can be tested apart: the journal (`record_cue`, `read_cues`),
the placement (`plan_cues`, pure: cue times, caption times and speech lengths in, start times
out) and the rendering (`synthesize_cues`, `mix_track`: an engine and `ffmpeg`). Speech never
changes the video's length unless the caller asks for it (`hold`): cues are played in order, so
when one runs on into the moment the next was planned for, the next waits until it ends and is
the one counted as late.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

NARRATION_FILE = "narration.jsonl"
#: The longest text one cue may hold, and the most cues one recording may hold.
NARRATION_MAX_CHARS = 400
NARRATION_MAX_CUES = 200
#: A cue that starts later than this after its planned moment (queued behind another) is late.
LATE_TOLERANCE_S = 0.25
#: A cue that ends this much past the video's end counts as cut off (a rounding is not a cut).
CUT_TOLERANCE_S = 0.05
#: How long before the command it explains an `--ahead` cue ends.
AHEAD_LEAD_S = 0.2
#: Speech faster or slower than this is refused: it stops being understandable.
SPEED_MIN = 0.5
SPEED_MAX = 2.0
#: Every cue is brought to this sample rate before it is mixed.
MIX_RATE = 24000
SPEECH_TIMEOUT_S = 60.0
MIX_TIMEOUT_S = 300.0
#: `auto` is Kokoro once it is set up (`blumkin browser narration setup`), else `say`.
ENGINE_NAMES = ("auto", "kokoro", "say")
#: A line of `say -v ?`: the voice's name, its locale (any one token: `en_US`, `ar_001`,
#: `en_GB-u-sd-gbsct`, `en-scotland`), then `# a sample sentence`.
_SAY_VOICE_LINE = re.compile(r"^(.+?)\s+\S+\s+#")
VOICE_MAX_CHARS = 60


@dataclass(frozen=True, slots=True)
class Cue:
    """One narration cue: when it was given (wall clock), its text and whether it leads."""

    t: float
    text: str
    ahead: bool = False


@dataclass(frozen=True, slots=True)
class Spoken:
    """A cue after it was synthesized: its sound file and how long that sound lasts."""

    cue: Cue
    path: Path
    seconds: float


@dataclass(frozen=True, slots=True)
class Placement:
    """Where one spoken cue lands in the video: planned, actually started, and its length."""

    planned: float
    start: float
    seconds: float

    @property
    def end(self) -> float:
        return self.start + self.seconds

    @property
    def late(self) -> float:
        return max(self.start - self.planned, 0.0)


@dataclass(frozen=True, slots=True)
class NarrationOptions:
    """What the caller asked of the narration when the recording is written."""

    engine: str = "auto"
    voice: str | None = None
    speed: float = 1.0
    #: Let the video run past the session's end (the last frame held) so the last cue is whole.
    hold: bool = False
    #: Where blumkin keeps its files (the Kokoro engine lives under it); set by `record stop`.
    config_dir: Path | None = None


class NarrationError(RuntimeError):
    """Narration could not be recorded, spoken or mixed."""


class Engine(Protocol):
    """A speech engine: text in, a WAV file out. Anything it writes is private."""

    name: str
    default_voice: str

    def ensure_ready(self) -> None:
        """Raise `NarrationError` if the engine cannot speak on this machine (checked early)."""
        ...

    def check_voice(self, voice: str) -> None:
        """Raise `NarrationError` if the engine has no voice called `voice` (checked early)."""
        ...

    def synthesize(self, text: str, *, voice: str, speed: float, out: Path) -> None: ...


class SayEngine:
    """The macOS `say` command: offline, built in, with no setup.

    The text goes in on standard input, not on the command line, so it is not in `say`'s own
    arguments (the process list shows those). It is in the arguments of the `blumkin browser
    narrate` command that was given it, like any argument.
    """

    name = "say"
    default_voice = "Samantha"
    base_wpm = 175

    def ensure_ready(self) -> None:
        if shutil.which("say") is None:
            raise NarrationError(
                "Expected the macOS `say` command for narration, got none on this machine"
            )

    def check_voice(self, voice: str) -> None:
        """Refuse a voice `say` does not list; a listing that cannot be read blocks nothing."""
        try:
            done = subprocess.run(  # noqa: S603, S607 - a fixed command
                ["say", "-v", "?"], capture_output=True, text=True, timeout=10.0, check=False
            )
        except OSError, subprocess.TimeoutExpired:
            return
        names = [
            match.group(1).strip()
            for line in (done.stdout or "").splitlines()
            if (match := _SAY_VOICE_LINE.match(line))
        ]
        if done.returncode != 0 or not names:
            return
        # `say` lists a voice as "Samantha (English (US))" yet takes `-v Samantha`: the name
        # before its parenthesis is as good as the whole one.
        known = {name.casefold() for name in names} | {
            name.split(" (")[0].casefold() for name in names
        }
        if voice.casefold() not in known:
            raise NarrationError(
                f"Expected a voice `say` has installed, got {voice!r}; `say -v ?` lists them "
                "(a Premium or Enhanced one is added in System Settings, Accessibility, Spoken "
                "Content)"
            )

    def synthesize(self, text: str, *, voice: str, speed: float, out: Path) -> None:
        self.ensure_ready()
        command = [
            "say", "-v", voice, "-r", str(round(self.base_wpm * speed)),
            "-o", str(out), f"--data-format=LEI16@{MIX_RATE}", "-f", "-",
        ]  # fmt: skip
        try:
            done = subprocess.run(  # noqa: S603, S607 - a fixed command; the text is on stdin
                command,
                input=text,
                capture_output=True,
                text=True,
                timeout=SPEECH_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NarrationError(f"Expected `say` to speak a cue, got {exc}") from exc
        if done.returncode != 0:
            # What `say` reports names the voice, never the text.
            reason = (done.stderr or "").strip().splitlines()[:1]
            raise NarrationError(
                f"Expected `say` to speak a cue, got exit {done.returncode}"
                + (f": {reason[0][:120]}" if reason else "")
            )


def get_engine(name: str, config_dir: Path | None = None) -> Engine:
    """The engine called `name`; `auto` is Kokoro when it is set up under `config_dir`, else `say`.

    Kokoro lives under the config directory, so it needs `config_dir`; asking for it by name
    without one is an error, and `auto` then falls back to `say`.
    """
    if name == "say":
        return SayEngine()
    if name in ("auto", "kokoro"):
        if config_dir is None:
            if name == "kokoro":
                raise ValueError(
                    "Expected a config directory to find the Kokoro engine in, got none"
                )
            return SayEngine()
        from blumkin.browser.narration_kokoro import KokoroEngine

        kokoro = KokoroEngine(config_dir)
        if name == "kokoro" or kokoro.is_installed():
            return kokoro
        return SayEngine()
    raise ValueError(f"Expected an engine of {', '.join(ENGINE_NAMES)}, got {name!r}")


def validate_text(text: str) -> str:
    """The cue's text, tidied, or a `ValueError` saying what is wrong with it."""
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("Expected some text to narrate, got none")
    if any(not ch.isprintable() for ch in cleaned):
        raise ValueError("Expected narration of printable characters, got a control character")
    if len(cleaned) > NARRATION_MAX_CHARS:
        raise ValueError(
            f"Expected at most {NARRATION_MAX_CHARS} characters per narration cue, got "
            f"{len(cleaned)}; say it in shorter cues"
        )
    return cleaned


def validate_voice(voice: str) -> str:
    """A voice name that cannot be taken for an option or hold a control character."""
    cleaned = voice.strip()
    if (
        not cleaned
        or len(cleaned) > VOICE_MAX_CHARS
        or cleaned.startswith("-")
        or any(not ch.isprintable() for ch in cleaned)
    ):
        raise ValueError(
            f"Expected a voice name (at most {VOICE_MAX_CHARS} characters), got {voice!r}"
        )
    return cleaned


def validate_speed(speed: float) -> float:
    if not (math.isfinite(speed) and SPEED_MIN <= speed <= SPEED_MAX):
        raise ValueError(
            f"Expected a narration speed from {SPEED_MIN:g} to {SPEED_MAX:g}, got {speed!r}"
        )
    return speed


# --------------------------------------------------------------------------- the journal


def record_cue(
    session_dir: Path, text: str, *, ahead: bool = False, now: Callable[[], float] = time.time
) -> int:
    """Append one cue to the session's journal; returns how many cues it holds now."""
    cleaned = validate_text(text)
    path = session_dir / NARRATION_FILE
    held = len(read_cues(session_dir))
    if held >= NARRATION_MAX_CUES:
        raise ValueError(f"Expected at most {NARRATION_MAX_CUES} narration cues, got more")
    line = json.dumps({"ahead": ahead, "t": now(), "text": cleaned}) + "\n"
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        if _ends_mid_line(path):
            handle.write("\n")  # a half-written last entry must not swallow this one
        handle.write(line)
    return held + 1


def _ends_mid_line(path: Path) -> bool:
    """Whether the journal's last byte is anything but a newline (a torn last entry)."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                return False
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"
    except OSError:
        return False


def read_cues(session_dir: Path) -> list[Cue]:
    """The cues a session holds, by time (a line cut short when a worker died is skipped)."""
    path = session_dir / NARRATION_FILE
    found: list[Cue] = []
    if path.is_file():
        for line in path.read_text().splitlines():
            try:
                item = json.loads(line)
                found.append(
                    Cue(t=float(item["t"]), text=str(item["text"]), ahead=bool(item.get("ahead")))
                )
            except KeyError, TypeError, ValueError:
                continue
    return sorted(found, key=lambda cue: cue.t)


# --------------------------------------------------------------------------- the placement


def plan_cues(
    spoken: Sequence[Spoken],
    caption_times: Sequence[float],
    to_video: Callable[[float], float],
) -> list[Placement]:
    """Where each spoken cue starts in the video (pure: no engine, no ffmpeg).

    The result is aligned with `spoken`: `placements[i]` is where `spoken[i]` goes, whatever
    order they are played in, so a sound file is never given another cue's start time.

    A plain cue is planned for the moment it was given. An `ahead` cue is planned to end just
    before the first command that followed it, but never to start before it was given; with no
    command after it, it is planned for the moment it was given. Cues are then played in order:
    one that cannot start on time because the one before is still speaking starts when that
    one ends, and the difference is its lateness.
    """
    planned: list[float] = []
    for item in spoken:
        start = to_video(item.cue.t)
        if item.cue.ahead:
            later = [t for t in caption_times if t > item.cue.t]
            if later:
                start = max(start, to_video(min(later)) - item.seconds - AHEAD_LEAD_S)
        planned.append(max(start, 0.0))
    placements: list[Placement | None] = [None] * len(spoken)
    free_at = 0.0
    for index in sorted(range(len(spoken)), key=lambda i: (planned[i], i)):
        actual = max(planned[index], free_at)
        placements[index] = Placement(
            planned=planned[index], start=actual, seconds=spoken[index].seconds
        )
        free_at = actual + spoken[index].seconds
    return [placement for placement in placements if placement is not None]


def summarize(placements: Sequence[Placement], total: float) -> dict[str, Any]:
    """The counts a result reports (never a word of the text).

    A cue is `cut` when its end falls past the video's end, whether it starts before it (and
    is heard only in part) or after it (and is not heard at all).
    """
    late = [p for p in placements if p.late > LATE_TOLERANCE_S]
    return {
        "cues": len(placements),
        "cut": sum(1 for p in placements if p.end > total + CUT_TOLERANCE_S),
        "late": len(late),
        "max_late_s": round(max((p.late for p in late), default=0.0), 2),
        "spoken_s": round(sum(p.seconds for p in placements), 2),
    }


def overrun(placements: Sequence[Placement], total: float) -> float:
    """How far the last cue ends past the video's end (0 when it ends inside it)."""
    return max(max((p.end for p in placements), default=0.0) - total, 0.0)


# --------------------------------------------------------------------------- the rendering


def wav_seconds(path: Path) -> float:
    """How long a WAV file plays."""
    with contextlib.closing(wave.open(str(path), "rb")) as handle:
        rate = handle.getframerate()
        if rate <= 0:
            raise NarrationError(f"Expected a WAV with a sample rate, got {rate}")
        return handle.getnframes() / rate


def synthesize_cues(
    cues: Sequence[Cue], engine: Engine, *, voice: str, speed: float, folder: Path
) -> list[Spoken]:
    """Speak every cue into its own WAV file in `folder` (private); returns what was made."""
    folder.mkdir(mode=0o700, exist_ok=True)
    spoken: list[Spoken] = []
    for number, cue in enumerate(cues, start=1):
        path = folder / f"cue{number:04d}.wav"
        engine.synthesize(cue.text, voice=voice, speed=speed, out=path)
        os.chmod(path, 0o600)
        try:
            seconds = wav_seconds(path)
        except (OSError, EOFError, wave.Error) as exc:
            raise NarrationError(f"Expected the engine to write a WAV file, got {exc}") from exc
        spoken.append(Spoken(cue=cue, path=path, seconds=seconds))
    return spoken


def mix_filter(placements: Sequence[Placement], total: float) -> str:
    """The `ffmpeg` filter graph that delays each cue to its start, mixes them and pads the
    result with silence to `total` seconds, so the sound track is as long as the video."""
    parts: list[str] = []
    for index, placement in enumerate(placements):
        delay = round(placement.start * 1000)
        parts.append(
            f"[{index}:a]aresample={MIX_RATE},aformat=channel_layouts=mono,"
            f"adelay={delay}|{delay}[c{index}]"
        )
    inputs = "".join(f"[c{index}]" for index in range(len(placements)))
    parts.append(f"{inputs}amix=inputs={len(placements)}:normalize=0:duration=longest[mixed]")
    parts.append(f"[mixed]apad=whole_dur={total:.3f}[mix]")
    return ";".join(parts)


def mix_track(
    spoken: Sequence[Spoken], placements: Sequence[Placement], total: float, target: Path
) -> None:
    """Mix the cues into one AAC file as long as the video (`total` seconds), at `target`."""
    if shutil.which("ffmpeg") is None:
        raise NarrationError("Expected ffmpeg on PATH to mix the narration, got none")
    command = ["ffmpeg", "-y", "-loglevel", "error"]
    for item in spoken:
        command += ["-i", str(item.path)]
    command += [
        "-filter_complex", mix_filter(placements, total), "-map", "[mix]",
        "-t", f"{total:.3f}", "-c:a", "aac", "-b:a", "96k", "-f", "ipod", str(target),
    ]  # fmt: skip
    try:
        done = subprocess.run(  # noqa: S603 - a fixed command with paths this module made
            command, capture_output=True, text=True, timeout=MIX_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NarrationError(f"Expected ffmpeg to mix the narration, got {exc}") from exc
    if done.returncode != 0:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        raise NarrationError(
            f"Expected ffmpeg to mix the narration, got exit {done.returncode}: "
            f"{(done.stderr or '').strip()[:200]}"
        )
    os.chmod(target, 0o600)
