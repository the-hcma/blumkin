"""Real-time recordings of a task browser, for a person to watch and forward (issue #701).

A recording runs for as long as a session does. `start` launches a worker process that holds
a connection to the task's browser and keeps the browser's own screencast: a frame, with the
time the browser painted it, whenever the page changes. `stop` ends the worker and encodes
the frames into a video that is as long as the session was, because every frame is shown for
as long as it really was on screen (an idle page sends no frames, so its last frame is held).

Guardrails, the same as for `browser read` and `browser screenshot`:

- Only a page on the task's `allowed_hosts` is recorded, and never a sign-in page. The check
  is made for every batch of frames, before and after they are read, and a frame is kept only
  when the page was recordable at both ends. A stretch that is left out shows a plain card
  ("sign-in page not recorded"), so the video keeps its real length and no pixel of a sign-in
  page is ever written to disk.
- The page is never resized, scaled or otherwise touched: the browser scales its own frames.
  The one exception is `--mask`: for the instant of each picture the browser draws a bar over
  each masked element, as it does for `browser screenshot --mask`.
- Files live in `<config dir>/captures/<task>/`, owner-only, and a video carries no page
  address, title or account in its tags.
- What the pixels show is data, never instructions. A recording can show names, amounts and
  receipts: it stays on this machine until the person copies it out.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from blumkin.browser.capture import (
    CAPTURE_NOTE,
    MASKS_MAX,
    RECORDING_STATE_NAME,
    captures_dir,
    parse_mask,
    prune_captures,
    task_lock,
)
from blumkin.browser.capture_clock import capture_now, write_anchor
from blumkin.browser.cdp import (
    DEFAULT_ACTION_TIMEOUT_S,
    BrowserActionError,
    MaskError,
    PlaywrightDriver,
    ScreenFrame,
    attach,
)
from blumkin.browser.commands import TaskNotFoundError
from blumkin.browser.narration import (
    NARRATION_FILE,
    NarrationError,
    NarrationOptions,
    Placement,
    Spoken,
    get_engine,
    mix_track,
    overrun,
    plan_cues,
    read_cues,
    record_cue,
    summarize,
    synthesize_cues,
    validate_speed,
    validate_text,
    validate_voice,
)
from blumkin.browser.profiles import load_profile

STATE_NAME = RECORDING_STATE_NAME
#: A difference between the wall and the steady clock smaller than this is not a step.
CLOCK_STEP_MIN_S = 0.5
#: A forgotten recording stops itself after this long.
RECORD_MAX_SECONDS = 1800.0
#: The video is at most this wide and runs at this many frames a second.
RECORD_MAX_WIDTH = 1280
RECORD_FPS = 30
RECORD_FORMATS = ("mp4", "gif")
#: How long the worker waits for page events between looks at the page's address.
POLL_MS = 40
#: A frame is never shown for less than this (a burst of frames in one instant).
MIN_FRAME_S = 0.001
#: A GIF with more frames than this is refused: it would be huge, and an MP4 is the right file.
GIF_MAX_DELAY_S = 600.0
GIF_MAX_SECONDS = 600.0
GIF_MAX_FRAMES = 600
FFMPEG_TIMEOUT_S = 600.0
START_WAIT_S = 25.0
STOP_WAIT_S = 30.0
KILL_WAIT_S = 5.0
#: A caption is shown for this long unless the next command comes sooner.
CAPTION_MIN_GAP_S = 0.05
CAPTION_SHOW_S = 3.0

#: How long a page that has just come back to a recordable address gets to paint before its
#: keyframe is taken: the browser keeps showing the old page's pixels until the new one paints.
SETTLE_MS = 300

_ANY_WEB_PAGE = "https://page.invalid/"

CARD_TEXT = {
    "off-hosts": "page outside the task's hosts - not recorded",
    "sign-in": "sign-in page - not recorded",
    "mask": "masked element not found - not recorded",
    "unstable": "page changing - not recorded",
}


@dataclass(frozen=True, slots=True)
class Entry:
    """One thing on the timeline: a frame (a file) or a card (a stretch left out)."""

    t: float
    kind: str
    file: str | None = None
    reason: str | None = None


# --------------------------------------------------------------------------- policy


def classify(
    url: str,
    hosts: tuple[str, ...],
    sign_in_hosts: tuple[str, ...],
    frame_hosts: tuple[str, ...] = (),
) -> str:
    """`ok` when the page may be recorded, else `sign-in` or `off-hosts`.

    A frame of the page on a sign-in host (an identity provider embedded in an allowed page)
    makes it a sign-in page too: its pixels are part of what is on screen.
    """
    from blumkin.browser.actions import host_allowed

    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host:
        return "off-hosts"
    if host_allowed(host, sign_in_hosts) or any(
        name and host_allowed(name, sign_in_hosts) for name in frame_hosts
    ):
        return "sign-in"
    if hosts and not host_allowed(host, hosts):
        return "off-hosts"
    return "ok"


# --------------------------------------------------------------------------- recorder


def run_recorder(
    driver: PlaywrightDriver,
    session_dir: Path,
    *,
    hosts: tuple[str, ...],
    sign_in_hosts: tuple[str, ...],
    stop: Callable[[], bool],
    max_seconds: float = RECORD_MAX_SECONDS,
    clock: Callable[[], float] = time.time,
    tick: Callable[[], float] = time.monotonic,
    on_ready: Callable[[], None] | None = None,
    masks: Sequence[tuple[str, str | None]] = (),
) -> str:
    """Record `driver`'s page into `session_dir` until `stop()` or `max_seconds`.

    With `masks`, every picture kept is a masked screenshot (the screencast only says when the
    page changed), and a stretch where a mask matches nothing, or a different number of
    elements while the picture is made, is left out as a card: the thing it should hide is
    never saved in view.

    Returns why it ended: `stopped`, `time limit` or `browser closed`. Frames go to
    `frames/` and one line per frame or card to `index.jsonl`, written as they happen, so a
    recording whose worker died can still be encoded up to that point.

    A batch of frames is kept only when the page was recordable at both ends of the read and
    every top-level document it committed meanwhile was too (so app, sign-in, app within one
    read keeps nothing). A frame does not say which document painted it, so this narrows the
    window around a navigation rather than proving each frame.
    """
    frames_dir = session_dir / "frames"
    frames_dir.mkdir(mode=0o700, exist_ok=True)
    index = _open_private(session_dir / "index.jsonl", "a")
    count = 0
    on_card = False
    need_fresh = False  # a step was seen and no picture has been taken since
    floor = 0.0
    began = tick()  # the time limit runs on a clock that never steps back
    reason = "stopped"

    anchor: list[float] = []  # [journal time of the first entry, steady clock at that moment]
    wall_at_anchor: list[float] = []  # the wall clock then: what a step is measured from
    last: list[float] = []  # the time of the entry logged last
    drift = [0.0]  # how far the wall clock was ahead of the steady one at the last look
    stepped_by = [0.0]  # the sum of the steps seen since the first entry

    def watch_step() -> bool:
        """Look at the two clocks; True when the wall clock stepped since the last look.

        A step is a jump between two looks (a poll apart), so the slow drift of a clock that is
        being slewed never adds up to one. Paint times are on the wall clock and the journal's
        axis is steady, so a frame is moved back by the steps seen: after one the pictures stay
        where the cards, the captions and the cues are, instead of a whole step away from them.
        """
        if not anchor:
            return False
        now_drift = (clock() - wall_at_anchor[0]) - (tick() - anchor[1])
        jump = now_drift - drift[0]
        drift[0] = now_drift
        if abs(jump) < CLOCK_STEP_MIN_S:
            return False
        stepped_by[0] += jump
        return True

    def on_axis(painted: float) -> float:
        """A paint time (or a keyframe's) on the journal's axis, before any clamping."""
        return painted - stepped_by[0]

    def moment() -> float:
        """Now on the journal's axis: the first entry's time plus steady time since it.

        The browser's own paint times and this clock agree at the first entry; after it, cards
        and the end marker advance on the steady clock, so a wall clock that steps back cannot
        put the end before the first picture.
        """
        return anchor[0] + (tick() - anchor[1]) if anchor else clock()

    def log(entry: dict[str, Any]) -> None:
        if not anchor and entry.get("kind") in ("frame", "card"):
            anchor.extend((float(entry["t"]), tick()))
            wall_at_anchor.append(clock())
            drift[0] = 0.0
            with contextlib.suppress(OSError):  # without it they fall back to the wall clock
                write_anchor(session_dir, anchor[0], anchor[1])  # captions and cues stamp from it
        if entry.get("kind") in ("frame", "card"):
            last[:] = [float(entry["t"])]
        index.write(json.dumps(entry) + "\n")
        index.flush()

    def add_frame(frame: ScreenFrame) -> None:
        nonlocal count
        if not _valid_jpeg(frame.data):
            return  # a frame that does not decode is not written
        count += 1
        name = f"frames/f{count:06d}.jpg"
        _write_private(session_dir / name, frame.data)
        # The correction is the step as of now, so a frame painted just before a step and read
        # just after it is moved a whole step the wrong way. A painted frame cannot be later than
        # now, nor earlier than the entry logged last: when a step is seen, hold it in between.
        moved = on_axis(frame.timestamp)
        if stepped_by[0] != 0.0:
            moved = min(moved, moment())
        log({"file": name, "kind": "frame", "t": max(moved, last[0]) if last else moved})

    def add_card(why: str) -> None:
        nonlocal on_card
        on_card = True
        log({"kind": "card", "reason": why, "t": moment()})

    def state_now() -> str:
        return classify(driver.url, hosts, sign_in_hosts, driver.frame_hosts())

    def route_state(urls: list[str], inner: tuple[str, ...] = ()) -> str:
        """`ok`, or the first reason a document committed on the way was not recordable.

        `inner` are the hosts of frames inside the page that came and went: only a sign-in one
        counts, as an advertisement frame on another host does not change what the page is.
        """
        if any(
            name and classify(_ANY_WEB_PAGE, (), sign_in_hosts, (name,)) == "sign-in"
            for name in inner
        ):
            return "sign-in"
        for url in urls:
            seen = classify(url, hosts, sign_in_hosts)
            if seen != "ok":
                return seen
        return "ok"

    why_not = "unstable"

    def steady() -> bool:
        """Whether the page is recordable now and committed nothing that was not meanwhile."""
        nonlocal why_not
        route = route_state(driver.take_committed_urls(), driver.take_committed_frame_hosts())
        now_state = state_now()
        problem = next((s for s in (now_state, route) if s != "ok"), None)
        if problem is not None:
            why_not = problem
        return problem is None

    def take_keyframe() -> ScreenFrame | None:
        """A frame of the page as it is, or None when the page is not steadily recordable."""
        nonlocal floor, why_not
        why_not = "unstable"
        driver.take_committed_urls()
        driver.take_committed_frame_hosts()
        if state_now() != "ok":
            why_not = state_now()
            return None
        try:
            driver.screencast_pump(SETTLE_MS)  # frames that arrive now may be the old page's
        except MaskError:  # a masked pump takes a picture too, and its mask may have nothing
            why_not = "mask"
            return None
        if not steady():
            return None
        try:
            key = driver.screencast_keyframe()
        except MaskError:
            why_not = "mask"
            return None
        if watch_step() or not steady() or not _valid_jpeg(key.data):
            return None  # a step now: its paint time is on one side or the other, try again
        floor = on_axis(key.timestamp)  # frames painted before it, still queued, are stale
        return key

    try:
        driver.screencast_start(max_width=RECORD_MAX_WIDTH, masks=tuple(masks))
        first = state_now()
        key = take_keyframe() if first == "ok" else None
        if key is not None:
            add_frame(key)
        else:
            add_card(first if first != "ok" else why_not)
        if on_ready is not None:
            on_ready()
        while not stop():
            if tick() - began >= max_seconds:
                reason = "time limit"
                break
            before = state_now()
            driver.take_committed_urls()
            driver.take_committed_frame_hosts()
            mask_missing = False
            try:
                frames = driver.screencast_pump(POLL_MS)
            except MaskError:
                frames, mask_missing = [], True
            step_seen = watch_step()
            after = state_now()
            route = route_state(driver.take_committed_urls(), driver.take_committed_frame_hosts())
            if before == "ok" and after == "ok" and route == "ok" and not mask_missing:
                if on_card:
                    # The page is recordable again, but an idle page sends no frame: take one
                    # (when it has settled; else the card goes on and the next look tries again).
                    resumed = take_keyframe()
                    if resumed is not None:
                        on_card = False
                        need_fresh = False
                        add_frame(resumed)
                elif step_seen or need_fresh:
                    # The clock stepped: a frame read now cannot be told from one queued before
                    # the last keyframe (its paint time is on either side of the step), so
                    # frames are left out and a fresh picture is tried for on every look until
                    # one is taken, which also compares later frames from there and shows a page
                    # that goes idle as it is now.
                    need_fresh = True
                    again = take_keyframe()
                    if again is not None:
                        need_fresh = False
                        add_frame(again)
                    elif why_not == "mask" and not on_card:
                        add_card("mask")  # nothing to cover: leave the stretch out, as a card
                else:
                    for frame in frames:
                        if on_axis(frame.timestamp) >= floor:
                            add_frame(frame)
            elif not on_card:
                add_card(
                    next(
                        (s for s in (after, route, before) if s != "ok"),
                        "mask" if mask_missing else "unstable",
                    )
                )
    except BrowserActionError:
        reason = "browser closed"
    finally:
        try:
            try:
                log({"kind": "end", "t": moment()})
            finally:
                index.close()
        finally:
            driver.screencast_stop()
    return reason


def _valid_jpeg(data: bytes) -> bool:
    """Whether `data` is a JPEG that decodes (a truncated or empty frame is not written)."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG":
                return False
            image.load()  # decodes every pixel: a truncated scan fails here, not later
            return True
    except Exception:  # noqa: BLE001 - anything that is not a decodable JPEG is refused
        return False


# --------------------------------------------------------------------------- timeline


def read_index(session_dir: Path) -> tuple[list[Entry], float]:
    """The timeline entries (by time) and the moment the recording ended."""
    entries: list[Entry] = []
    ended: float | None = None
    path = session_dir / "index.jsonl"
    if not path.is_file():
        raise BrowserActionError(f"Expected a recording index in {session_dir}, got none")
    for line in path.read_text().splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue  # a line cut short when the worker died
        if item.get("kind") == "end":
            ended = float(item["t"])
        elif item.get("kind") in ("frame", "card"):
            entries.append(
                Entry(
                    t=float(item["t"]),
                    kind=str(item["kind"]),
                    file=item.get("file"),
                    reason=item.get("reason"),
                )
            )
    entries.sort(key=lambda entry: entry.t)
    if not entries:
        raise BrowserActionError("Expected at least one recorded frame, got none")
    if ended is None:
        ended = max(entry.t for entry in entries)
    return entries, max(ended, entries[-1].t)


def durations(
    entries: list[Entry], ended: float, *, speed: float = 1.0, trim_idle_s: float | None = None
) -> list[float]:
    """How long each entry is shown in the video.

    Real time by default: an entry lasts until the next one (the last until `ended`).
    `speed` divides every duration; `trim_idle_s` caps any one of them (a long wait).
    """
    shown: list[float] = []
    for index, entry in enumerate(entries):
        end = entries[index + 1].t if index + 1 < len(entries) else ended
        length = max(end - entry.t, 0.0)
        if trim_idle_s is not None:
            length = min(length, trim_idle_s)
        shown.append(max(length / speed, MIN_FRAME_S))
    return shown


def output_times(entries: list[Entry], shown: list[float]) -> list[float]:
    """Where each entry starts in the video."""
    starts: list[float] = []
    at = 0.0
    for length in shown:
        starts.append(at)
        at += length
    return starts


def map_time(t: float, entries: list[Entry], shown: list[float], speed: float) -> float:
    """A wall-clock moment as a position in the video (clamped into the entry it falls in)."""
    starts = output_times(entries, shown)
    if t <= entries[0].t:
        return 0.0
    for index in range(len(entries) - 1, -1, -1):
        if t >= entries[index].t:
            inside = min((t - entries[index].t) / speed, shown[index])
            return starts[index] + inside
    return 0.0


def concat_listing(
    entries: list[Entry], shown: list[float], paths: dict[str, Path], cards: dict[str, Path]
) -> str:
    """The ffmpeg concat-demuxer file: each image with its own duration.

    The demuxer holds the last image one extra duration unless it is listed again with none,
    so the last one is repeated bare and the output is cut to the real total.
    """
    lines: list[str] = []
    last = None
    for entry, length in zip(entries, shown, strict=True):
        image = (
            cards[entry.reason or "off-hosts"] if entry.kind == "card" else paths[entry.file or ""]
        )
        last = image
        lines.append(f"file '{_quote(image)}'")
        lines.append(f"duration {length:.6f}")
    if last is not None:
        lines.append(f"file '{_quote(last)}'")
    return "\n".join(lines) + "\n"


def srt_text(
    captions: list[tuple[float, str]],
    entries: list[Entry],
    shown: list[float],
    speed: float,
    total: float,
) -> str:
    """SubRip text for `captions` (wall-clock moment, text), placed on the video's timeline."""
    return "\n".join(srt_cues(captions, entries, shown, speed, total))


def srt_cues(
    captions: list[tuple[float, str]],
    entries: list[Entry],
    shown: list[float],
    speed: float,
    total: float,
) -> list[str]:
    """The SubRip cues themselves, so a caller counts what was emitted, not what a text holds."""
    placed = [
        (begin, text)
        for begin, text in (
            (map_time(moment, entries, shown, speed), text) for moment, text in captions
        )
        if begin < total
    ]
    # Commands that land on the same instant (a trimmed or sped-up stretch) keep the last one.
    kept = [
        item for index, item in enumerate(placed)
        if index + 1 == len(placed) or placed[index + 1][0] - item[0] >= CAPTION_MIN_GAP_S
    ]  # fmt: skip
    cues: list[str] = []
    for number, (begin, text) in enumerate(kept, start=1):
        following = kept[number][0] if number < len(kept) else total
        end = min(max(begin + 0.5, begin + CAPTION_SHOW_S), begin + CAPTION_SHOW_S + 2.0, total)
        end = min(end, following)  # a cue never runs into the next one
        if end <= begin:
            continue
        cues.append(f"{number}\n{_srt_time(begin)} --> {_srt_time(end)}\n{text}\n")
    return cues


def narration_srt_cues(
    spoken: Sequence[Spoken], placements: Sequence[Placement], total: float
) -> list[str]:
    """The narration as SubRip cues: each shown while it is heard, so a muted viewer reads it.

    Cues are played in order and never overlap; one that starts past the video's end is left
    out and one that runs past it ends with it.
    """
    cues: list[str] = []
    for item, where in zip(spoken, placements, strict=True):
        end = min(where.end, total)
        if where.start >= total or end <= where.start:
            continue
        cues.append(
            f"{len(cues) + 1}\n{_srt_time(where.start)} --> {_srt_time(end)}\n{item.cue.text}\n"
        )
    return cues


# --------------------------------------------------------------------------- encoding


def encode_recording(
    session_dir: Path,
    target: Path,
    *,
    fmt: str = "mp4",
    speed: float = 1.0,
    trim_idle_s: float | None = None,
    narration: NarrationOptions | None = None,
) -> dict[str, Any]:
    """Write the session's frames as `target` (an MP4 or a GIF) at real speed.

    The narration cues of the session (see `narration.py`) are spoken, mixed and muxed into an
    MP4 when `narration` is given; a GIF has no audio. Returns what was written: seconds, bytes,
    frame and card counts, and a `narration` summary (counts only) when the session had cues.
    """
    if fmt not in RECORD_FORMATS:
        raise ValueError(f"Expected a format of {', '.join(RECORD_FORMATS)}, got {fmt!r}")
    if speed <= 0:
        raise ValueError(f"Expected a speed above 0, got {speed:g}")
    if trim_idle_s is not None and trim_idle_s <= 0:
        raise ValueError(f"Expected an idle limit above 0 seconds, got {trim_idle_s:g}")
    entries, ended = read_index(session_dir)
    shown = durations(entries, ended, speed=speed, trim_idle_s=trim_idle_s)
    total = sum(shown)
    paths = {item.file: session_dir / item.file for item in entries if item.file}
    width, height = _video_size(entries, paths)
    paths = _same_size(entries, paths, width, height, session_dir)
    cards = _write_cards(session_dir, entries, width, height)
    captions = read_captions(session_dir)
    cues = 0
    told: dict[str, Any] | None = None
    held_cues = read_cues(session_dir)
    if held_cues and (fmt == "gif" or narration is None):
        told = {
            "cues": len(held_cues),
            "skipped": "a GIF has no audio" if fmt == "gif" else "narration was turned off",
        }
    audio: Path | None = None
    spoken_text: list[str] = []
    video_shown = shown  # what the frames last: longer in the last one when it is held
    work = session_dir / "narration"
    try:
        if held_cues and fmt == "mp4" and narration is not None:
            audio, told, video_shown, total, spoken_text = _narrate(
                session_dir, work, held_cues, captions, entries, shown, speed, total, narration
            )
        if fmt == "gif":
            total = _encode_gif(entries, shown, paths, cards, width, height, target)
        else:
            cues = _encode_mp4(
                session_dir,
                entries,
                video_shown,
                paths,
                cards,
                captions,
                speed=speed,
                width=width,
                height=height,
                total=total,
                target=target,
                audio=audio,
                caption_shown=shown,
                narration_cues=spoken_text,
            )
            if told is not None and spoken_text:
                told = {**told, "subtitles": len(spoken_text)}
    finally:
        shutil.rmtree(work, ignore_errors=True)  # the sound files go with the encode
        if audio is not None:
            with contextlib.suppress(OSError):
                audio.unlink(missing_ok=True)
    os.chmod(target, 0o600)
    return {
        "bytes": target.stat().st_size,
        "captions": cues,
        "cards": sum(1 for entry in entries if entry.kind == "card"),
        "format": fmt,
        "frames": sum(1 for entry in entries if entry.kind == "frame"),
        "height": height,
        **({"narration": told} if told is not None else {}),
        "seconds": round(total, 3),
        "width": width,
    }


def _narrate(
    session_dir: Path,
    work: Path,
    cues: list[Any],
    captions: list[tuple[float, str]],
    entries: list[Entry],
    shown: list[float],
    speed: float,
    total: float,
    options: NarrationOptions,
) -> tuple[Path, dict[str, Any], list[float], float, list[str]]:
    """Speak the cues, place them, and mix them into one audio file for the video.

    Returns the audio file, what to report, the frame durations and total (which grow by the
    overrun, the last frame held, only when `options.hold` asks for it) and the narration as
    SubRip cues for the subtitle track.
    """
    try:
        engine = get_engine(options.engine, options.config_dir)
        voice = validate_voice(options.voice or engine.default_voice)
        engine.check_voice(voice)  # a direct export is checked too, not only `record stop`
        spoken = synthesize_cues(
            cues, engine, voice=voice, speed=validate_speed(options.speed), folder=work
        )
        placements = plan_cues(
            spoken,
            [moment for moment, _ in captions],
            lambda moment: map_time(moment, entries, shown, speed),
        )
        extra = overrun(placements, total) if options.hold else 0.0
        if extra > 0:
            shown = [*shown[:-1], shown[-1] + extra]
            total += extra
        audio = work / "mix.m4a"  # in the folder that is removed whether or not the mix works
        mix_track(spoken, placements, total, audio)
        written = narration_srt_cues(spoken, placements, total)
        told = {
            **summarize(placements, total),
            "engine": engine.name,
            "hold_s": round(extra, 2),
            "voice": voice,
        }
    except (NarrationError, ValueError) as exc:
        raise BrowserActionError(str(exc)) from exc
    return audio, told, shown, total, written


def read_captions(session_dir: Path) -> list[tuple[float, str]]:
    """The command captions written during the recording, by time."""
    path = session_dir / "captions.jsonl"
    found: list[tuple[float, str]] = []
    if path.is_file():
        for line in path.read_text().splitlines():
            try:
                item = json.loads(line)
                found.append((float(item["t"]), str(item["text"])))
            except KeyError, TypeError, ValueError:
                continue
    return sorted(found)


def _encode_gif(
    entries: list[Entry],
    shown: list[float],
    paths: dict[str, Path],
    cards: dict[str, Path],
    width: int,
    height: int,
    target: Path,
) -> float:
    """Write the GIF; returns the seconds it really plays (each delay is at least 20 ms)."""
    from PIL import Image

    if len(entries) > GIF_MAX_FRAMES:
        raise ValueError(
            f"Expected at most {GIF_MAX_FRAMES} frames for a GIF, got {len(entries)}; "
            "use --format mp4 for a long recording"
        )
    if max(shown, default=0.0) > GIF_MAX_DELAY_S or sum(shown) > GIF_MAX_SECONDS:
        raise ValueError(
            f"Expected a GIF of at most {GIF_MAX_SECONDS:g} seconds, got {sum(shown):.0f}; "
            "a GIF cannot hold so long a wait, use --format mp4 or --trim-idle"
        )
    images = []
    for entry in entries:
        source = (
            cards[entry.reason or "off-hosts"] if entry.kind == "card" else paths[entry.file or ""]
        )
        with Image.open(source) as raw:
            frame = raw.convert("RGB").resize(
                (min(width, 800), round(height * min(width, 800) / width))
            )
            images.append(frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=128))
    delays = [max(20, round(length * 1000)) for length in shown]
    descriptor = os.open(target, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        images[0].save(
            handle,
            format="GIF",
            save_all=True,
            append_images=images[1:],
            duration=delays,
            loop=0,
            optimize=False,
        )
    return sum(delays) / 1000.0


def _encode_mp4(
    session_dir: Path,
    entries: list[Entry],
    shown: list[float],
    paths: dict[str, Path],
    cards: dict[str, Path],
    captions: list[tuple[float, str]],
    *,
    speed: float,
    width: int,
    height: int,
    total: float,
    target: Path,
    audio: Path | None = None,
    caption_shown: list[float] | None = None,
    narration_cues: list[str] | None = None,
) -> int:
    """Write the MP4 and return how many command caption cues it holds.

    The narration track (`narration_cues`) is counted by the caller, not here.

    `caption_shown` is the frame durations the captions are placed through: the session's own,
    even when the last frame is held for narration, so a hold never moves a command.

    With `audio` (the mixed narration, AAC) it is muxed in as the video's sound track.
    """
    if shutil.which("ffmpeg") is None:
        raise ValueError(
            "Expected ffmpeg on PATH to write an MP4, got none; install it or use --format gif"
        )
    listing = session_dir / "frames.txt"
    _write_private(listing, concat_listing(entries, shown, paths, cards).encode())
    scale = (
        f"fps={RECORD_FPS},scale={width}:{height}:force_original_aspect_ratio=decrease:"
        f"in_range=full:out_range=limited,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    )
    command = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(listing),
    ]  # fmt: skip
    subtitles = None
    cues = 0
    if captions:
        found = srt_cues(captions, entries, caption_shown or shown, speed, total)
        text = "\n".join(found)
        cues = len(found)
        if text:
            subtitles = session_dir / "captions.srt"
            _write_private(subtitles, text.encode())
            command += ["-i", str(subtitles)]
    spoken_track = None
    if narration_cues:
        # in the folder `encode_recording` removes, so the words are not kept with the frames
        spoken_track = session_dir / "narration" / "narration.srt"
        _write_private(spoken_track, "\n".join(narration_cues).encode())
        command += ["-i", str(spoken_track)]
    next_input = 1 + (subtitles is not None) + (spoken_track is not None)
    audio_input = None
    if audio is not None:
        audio_input = next_input
        command += ["-i", str(audio)]
    command += ["-vf", scale, "-map", "0:v"]
    tracks = 0
    if spoken_track is not None:  # the narration first: it is the track a muted viewer is shown
        command += [
            "-map", f"{1 + (subtitles is not None)}:0",
            f"-metadata:s:s:{tracks}", "language=eng", f"-metadata:s:s:{tracks}", "title=Narration",
            f"-disposition:s:{tracks}", "default",
        ]  # fmt: skip
        tracks += 1
    if subtitles is not None:
        command += ["-map", "1:0", f"-metadata:s:s:{tracks}", "language=eng"]
        if spoken_track is not None:
            command += [
                f"-metadata:s:s:{tracks}",
                "title=Commands",
                f"-disposition:s:{tracks}",
                "0",
            ]
        tracks += 1
    if tracks:
        command += ["-c:s", "mov_text"]
    if audio_input is not None:
        command += ["-map", f"{audio_input}:a", "-c:a", "copy"]
    command += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
        "-t", f"{total:.3f}",
        "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact",
        "-movflags", "+faststart", "-f", "mp4",
        str(target),
    ]  # fmt: skip
    try:
        done = subprocess.run(  # noqa: S603 - a fixed command with paths this module made
            command, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BrowserActionError(f"Expected ffmpeg to write the MP4, got {exc}") from exc
    if done.returncode != 0:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        raise BrowserActionError(
            f"Expected ffmpeg to write the MP4, got exit {done.returncode}: "
            f"{(done.stderr or '').strip()[:200]}"
        )
    return cues


def _video_size(entries: list[Entry], paths: dict[str, Path]) -> tuple[int, int]:
    """The size most frames have, made even and no wider than the video limit.

    The start keyframe and the browser's scaled frames can differ in size, so the common one
    (the screencast's) is the video's, and the rest are fitted to it by `_same_size`.
    """
    from collections import Counter

    from PIL import Image

    found: Counter[tuple[int, int]] = Counter()
    for entry in entries:
        if entry.kind == "frame" and entry.file:
            with Image.open(paths[entry.file]) as image:
                found[image.size] += 1
    width, height = found.most_common(1)[0][0] if found else (1280, 720)
    if width > RECORD_MAX_WIDTH:
        height = round(height * RECORD_MAX_WIDTH / width)
        width = RECORD_MAX_WIDTH
    return max(2, width - width % 2), max(2, height - height % 2)


def _same_size(
    entries: list[Entry], paths: dict[str, Path], width: int, height: int, session_dir: Path
) -> dict[str, Path]:
    """The frames as files that are all `width` by `height`.

    ffmpeg drops the frame it is holding when the size changes mid-stream, which cut seconds
    out of a video, so a frame of another size is fitted (centered, never stretched) into a
    copy of that size; a frame that is already right is used as it is.
    """
    from PIL import Image

    fixed: dict[str, Path] = {}
    folder = session_dir / "fitted"
    for entry in entries:
        if entry.kind != "frame" or not entry.file:
            continue
        source = paths[entry.file]
        with Image.open(source) as image:
            if image.size == (width, height):
                fixed[entry.file] = source
                continue
            frame = image.convert("RGB")
            scale = min(width / frame.width, height / frame.height)
            fitted = frame.resize(
                (max(1, round(frame.width * scale)), max(1, round(frame.height * scale))),
                Image.Resampling.LANCZOS,
            )
        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        canvas.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
        folder.mkdir(mode=0o700, exist_ok=True)
        target = folder / Path(entry.file).name
        buffer = io.BytesIO()
        canvas.save(buffer, format="JPEG", quality=85)
        _write_private(target, buffer.getvalue())
        fixed[entry.file] = target
    return fixed


def _write_cards(
    session_dir: Path, entries: list[Entry], width: int, height: int
) -> dict[str, Path]:
    """One plain card image per reason a stretch was left out."""
    from PIL import Image, ImageDraw, ImageFont

    made: dict[str, Path] = {}
    for reason in sorted(
        {entry.reason or "off-hosts" for entry in entries if entry.kind == "card"}
    ):
        image = Image.new("RGB", (width, height), (32, 36, 44))
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=max(18, width // 40))
        text = CARD_TEXT.get(reason, CARD_TEXT["off-hosts"])
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(
            ((width - (box[2] - box[0])) / 2, (height - (box[3] - box[1])) / 2),
            text,
            fill=(220, 224, 232),
            font=font,
        )
        # A JPEG like every frame: the concat demuxer decodes each file with the first file's
        # codec, so a PNG among JPEGs is dropped and the frame before it stays on screen.
        path = session_dir / f"card-{reason}.jpg"
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        _write_private(path, buffer.getvalue())
        made[reason] = path
    return made


# --------------------------------------------------------------------------- captions

#: How each browser command is captioned: the parameters it names, in order, as `(name, how)`.
#: Control labels, key names and hosts only. A typed or chosen value, a file path, an address's
#: path or an amount never reaches a caption: `fill` shows the field, not what went into it.
CAPTION_PARTS: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "cell": ("cell", (("row", "{}"), ("column", '"{}"'))),
    "click": ("click", (("name", '"{}"'),)),
    "dialog": ("dialog", (("open_specs", "opens {}"),)),
    "download": ("download", (("control", '"{}"'),)),
    "fill": ("fill", (("label", '"{}"'),)),
    "frames": ("frames", ()),
    "goto": ("goto", (("url", "{}"),)),
    "grid": ("grid", ()),
    "inspect": ("inspect", (("label", '"{}"'),)),
    "options": ("options", (("label", '"{}"'),)),
    "pick": ("pick", ()),
    "press": ("press", (("label", '"{}"'), ("key", "{}"))),
    "read": ("read", ()),
    "refresh-tab": ("refresh-tab", (("tab", '"{}"'),)),
    "select": ("select", (("label", '"{}"'),)),
    "upload": ("upload", (("label", '"{}"'),)),
    "verify": ("verify", (("label", '"{}"'),)),
}
CAPTION_KEYS = frozenset(
    {"ArrowDown", "ArrowLeft", "ArrowRight", "ArrowUp", "Backspace", "Delete", "End", "Enter",
     "Escape", "Home", "PageDown", "PageUp", "Tab", "Shift+Tab"}
)  # fmt: skip
CAPTION_MAX_CHARS = 80


def caption_for(command: str, params: dict[str, Any]) -> str | None:
    """The caption for a browser command with these parameters, or None for one not shown."""
    entry = CAPTION_PARTS.get(command)
    if entry is None:
        return None
    words = [entry[0]]
    for name, shape in entry[1]:
        value = params.get(name)
        if value in (None, "", ()):
            continue
        if name == "key" and str(value) not in CAPTION_KEYS:
            continue  # only a key that types nothing is named: `Shift+A` is the letter A
        if name == "url":
            value = urlsplit(str(value)).hostname or ""  # the host only: no path, no query
        elif isinstance(value, (tuple, list)):
            value = ", ".join(str(item) for item in value)
        text = "".join(ch if ch.isprintable() else " " for ch in str(value)).strip()
        if text:
            words.append(shape.format(text))
    return " ".join(words)[:CAPTION_MAX_CHARS]


def note_command(
    config_dir: Path,
    task: str,
    command: str,
    params: dict[str, Any],
    *,
    now: Callable[[], float] | None = None,
) -> None:
    """Add a caption to the task's running recording, if there is one; never raises.

    A caption is a read of a small state file and one appended line, so it adds nothing a
    command could notice, and a failure to write it must never fail the command.
    """
    try:
        text = caption_for(command, params)
        if text is None or not task or task != Path(task).name:
            return
        directory = config_dir / "captures" / task
        state = _read_state(directory)
        if state is None:
            return
        session_dir = directory / str(state["session"])
        if not session_dir.is_dir() or (session_dir / "reason").exists():
            return
        with _open_private(session_dir / "captions.jsonl", "a") as handle:
            moment = now() if now is not None else capture_now(session_dir)
            handle.write(json.dumps({"t": moment, "text": text}) + "\n")
    except Exception:  # noqa: BLE001, S110 - a caption must never fail the command it describes
        return


def narrate_recording(
    config: Any,
    task: str,
    text: str,
    *,
    ahead: bool = False,
    now: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Add one narration cue to the task's running recording; refuses when none is running.

    Unlike a caption, which is a courtesy and never fails its command, a cue that cannot be
    recorded is an error: the caller meant it to be heard. The text is the caller's own, never
    read off the page, and is kept only in the session's private journal until `record stop`.

    The check and the append are made under the task's lock, and `record stop` marks the
    recording as stopping under it before it reads the journal: a cue that is accepted is
    always one the encode will see.
    """
    cleaned = validate_text(text)
    directory = captures_dir(config.config_dir, task)
    with task_lock(directory):
        state = _read_state(directory)
        if state is not None and state.get("kind") != "clip" and state.get("stopping"):
            raise BrowserActionError(
                f"Expected a running recording of task {task!r} to narrate, got one that is "
                "being stopped"  # checked first: the stop has ended its worker by now
            )
        if state is None or state.get("kind") == "clip" or not _worker_alive(state):
            raise BrowserActionError(
                f"Expected a running recording of task {task!r} to narrate, got none; start one "
                f"with `blumkin browser record start {task}`"
            )
        session_dir = directory / str(state["session"])
        if not session_dir.is_dir() or (session_dir / "reason").exists():
            raise BrowserActionError(
                f"Expected a running recording of task {task!r} to narrate, got one that has ended"
            )
        try:
            held = record_cue(
                session_dir,
                cleaned,
                ahead=ahead,
                now=now if now is not None else (lambda: capture_now(session_dir)),
            )
        except ValueError as exc:
            raise BrowserActionError(str(exc)) from exc
    return {"action": "narrate", "ahead": ahead, "cues": held, "ok": True, "task": task}


def _require_narration_ready(options: NarrationOptions) -> None:
    """Refuse, before a recording is stopped, narration that could not be made."""
    try:
        engine = get_engine(options.engine, options.config_dir)
        engine.ensure_ready()
        engine.check_voice(validate_voice(options.voice or engine.default_voice))
    except NarrationError as exc:
        raise BrowserActionError(str(exc)) from exc
    if shutil.which("ffmpeg") is None:
        raise BrowserActionError(
            "Expected ffmpeg on PATH to mix the narration, got none; install it (an MP4 needs it "
            "with or without narration), or stop with --format gif, which has no audio"
        )


# --------------------------------------------------------------------------- the worker


def _checked_masks(specs: Sequence[str]) -> tuple[tuple[str, str | None], ...]:
    """`ROLE[:NAME]` options as (role, name) pairs, refusing a bad one before anything starts."""
    masks = tuple(parse_mask(spec) for spec in specs)
    if len(masks) > MASKS_MAX:
        raise ValueError(f"Expected at most {MASKS_MAX} --mask options, got {len(masks)}")
    return masks


def start_recording(
    config: Any,
    task: str,
    *,
    now: Callable[[], float] = time.time,
    masks: Sequence[str] = (),
) -> dict[str, Any]:
    """Start recording the task's page in a worker process; returns once it is recording.

    With `masks` (`ROLE[:NAME]`), the named elements are covered in every picture kept.
    """
    from blumkin.browser.actions import _declared_hosts, sign_in_hosts_for

    covered = _checked_masks(masks)

    profile = load_profile(config.config_dir, task)
    if profile is None:
        raise TaskNotFoundError(f"Expected an existing browser profile for task {task!r}, got none")
    directory = captures_dir(config.config_dir, task)
    prune_captures(directory, 24 * 3600.0)
    state: dict[str, Any] = {"pid": 0, "session": "", "started": now(), "task": task}
    with task_lock(directory):
        _refuse_if_recording(directory, task, now)
        session_dir = directory / f"{_stamp(now())}-recording"
        session_dir.mkdir(mode=0o700)
        state["session"] = session_dir.name
        # The task is reserved before anything is launched: a second `start` (or a clip) sees
        # this file and refuses, and a cleanup leaves the session alone.
        try:
            _reserve_state(directory, state)
        except BaseException:
            shutil.rmtree(session_dir, ignore_errors=True)
            raise
    meta = {
        "config_dir": str(config.config_dir),
        "hosts": list(_declared_hosts(config, task)),
        "masks": [list(mask) for mask in covered],
        "max_seconds": RECORD_MAX_SECONDS,
        "sign_in_hosts": list(sign_in_hosts_for(config, task)),
        "task": task,
    }
    worker: subprocess.Popen[bytes] | None = None
    try:
        _write_private(session_dir / "meta.json", json.dumps(meta).encode())
        log = _open_private(session_dir / "worker.log", "w")
        try:
            worker = subprocess.Popen(  # noqa: S603 - this interpreter and a path this module made
                [sys.executable, "-m", "blumkin.browser.recording", str(session_dir)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log.close()
        _write_state(directory, {**state, "pid": worker.pid})
        deadline = time.monotonic() + START_WAIT_S
        while time.monotonic() < deadline:
            if (session_dir / "ready").exists():
                return {
                    "action": "record-start",
                    "ok": True,
                    "masks": len(covered),
                    "recording": True,
                    "session": session_dir.name,
                    "task": task,
                }
            if worker.poll() is not None:
                break
            time.sleep(0.1)
        problem = _worker_problem(session_dir)
    except BaseException:
        _stop_worker(worker)
        _discard(directory, session_dir)
        raise
    _stop_worker(worker)
    _discard(directory, session_dir)
    raise BrowserActionError(f"Expected the recording to start, got: {problem}")


def stop_recording(
    config: Any,
    task: str,
    *,
    fmt: str = "mp4",
    out: Path | None = None,
    speed: float = 1.0,
    trim_idle_s: float | None = None,
    keep_frames: bool = False,
    now: Callable[[], float] = time.time,
    narration: NarrationOptions | None = NarrationOptions(),
) -> dict[str, Any]:
    """Stop the task's recording, encode it, and return the file and what it holds.

    The narration cues given with `narrate` are spoken into the video (`narration` is None to
    leave them out).
    """
    directory = captures_dir(config.config_dir, task)
    if narration is not None:
        # The engine looks for Kokoro under the config directory.
        narration = replace(narration, config_dir=config.config_dir)
        # Before anything is stopped: a bad option must not leave a half-finished stop.
        get_engine(narration.engine, narration.config_dir)
        validate_voice(narration.voice or "x")
        validate_speed(narration.speed)
    marked = False  # whether this stop wrote the mark, so only its own is ever taken off
    state: dict[str, Any] = {}
    try:
        with task_lock(directory):
            found = _read_state(directory)
            if found is None:
                raise BrowserActionError(f"Expected a recording of task {task!r} to stop, got none")
            state = found
            if state.get("kind") == "clip" and _worker_alive(state):
                raise BrowserActionError(
                    f"Expected a recording to stop, got a `record clip` of task {task!r}, "
                    "which ends by itself"
                )
            session_dir = directory / str(state["session"])
            if int(state["pid"]) <= 0 and now() - float(state["started"]) < START_WAIT_S * 2:
                raise BrowserActionError(
                    f"Expected a recording of task {task!r} that is running, got one still "
                    "starting; try again in a few seconds"
                )
            if not _worker_alive(state) and not _has_entries(session_dir):
                shutil.rmtree(session_dir, ignore_errors=True)
                _release_state_locked(directory, session_dir.name)
                raise BrowserActionError(
                    f"Expected a recording of task {task!r} to stop, got one whose recorder died "
                    "before it wrote anything; it was cleared, so `record start` can run again"
                )
            if narration is not None and fmt == "mp4" and read_cues(session_dir):
                # Before anything is stopped (a GIF has no audio): the engine, the voice, ffmpeg.
                _require_narration_ready(narration)
            # From here a cue is no longer accepted: `narrate` checks this under the same lock, so
            # one it said yes to is always in the journal the encode reads, never lost to it.
            state = {**state, "stopping": True}
            _write_state_locked(directory, state)
            marked = True
        if _worker_alive(state):
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(state["pid"]), signal.SIGTERM)
            deadline = time.monotonic() + STOP_WAIT_S
            while time.monotonic() < deadline and _worker_alive(state):
                time.sleep(0.1)
            if _worker_alive(state):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(state["pid"]), signal.SIGKILL)
                deadline = time.monotonic() + KILL_WAIT_S
                while time.monotonic() < deadline and _worker_alive(state):
                    time.sleep(0.1)
            if _worker_alive(state):
                # The journal is read only once the worker has stopped writing it; the recording
                # goes on, so it takes cues again.
                _clear_stopping(directory, state)
                raise BrowserActionError(
                    f"Expected the recorder of task {task!r} to have stopped, got a process that "
                    "still runs; nothing was encoded and the recording is kept, so try again"
                )
        _claim_export(directory, state)
    except BaseException:
        # Interrupted (Ctrl+C) before the export was claimed: a recorder that is still
        # running goes on taking cues. One that has stopped is over, whatever the mark says.
        if marked and _worker_alive(state):
            _clear_stopping(directory, state)
        raise
    try:
        if not _has_entries(session_dir):
            _discard(directory, session_dir)
            raise BrowserActionError(
                f"Expected a recording of task {task!r} with a picture in it, got none; it was "
                "cleared, so `record start` can run again"
            )
        return _finish(
            directory,
            session_dir,
            task=task,
            fmt=fmt,
            out=out,
            speed=speed,
            trim_idle_s=trim_idle_s,
            keep_frames=keep_frames,
            started=float(state["started"]),
            now=now,
            action="record-stop",
            narration=narration,
        )
    except BaseException:
        _unclaim_export(directory, state)
        raise


def clip_recording(
    config: Any,
    task: str,
    *,
    seconds: float,
    fmt: str = "mp4",
    out: Path | None = None,
    speed: float = 1.0,
    trim_idle_s: float | None = None,
    keep_frames: bool = False,
    now: Callable[[], float] = time.time,
    tick: Callable[[], float] = time.monotonic,
    masks: Sequence[str] = (),
) -> dict[str, Any]:
    """Record the task's page for `seconds` seconds in this process and encode it.

    With `masks` (`ROLE[:NAME]`), the named elements are covered in every picture kept.

    What the file needs (a format, ffmpeg for an MP4, a destination that does not exist) is
    checked first, so a clip is not recorded only to fail at the end. A browser that stops
    answering holds a call for at most its own deadline, so the clip can run past `seconds` by
    that long before it gives up.
    """
    from blumkin.browser.actions import _declared_hosts, sign_in_hosts_for

    if not 1.0 <= seconds <= RECORD_MAX_SECONDS:
        raise ValueError(f"Expected 1 to {RECORD_MAX_SECONDS:g} seconds, got {seconds:g}")
    covered = _checked_masks(masks)
    _check_output(fmt, out, speed, trim_idle_s)
    profile = load_profile(config.config_dir, task)
    if profile is None:
        raise TaskNotFoundError(f"Expected an existing browser profile for task {task!r}, got none")
    directory = captures_dir(config.config_dir, task)
    prune_captures(directory, 24 * 3600.0)
    started = now()
    own_start = _process_started(os.getpid())
    ready_at: list[float] = []  # the clip's time runs from its first picture, not from attaching
    with task_lock(directory):
        _refuse_if_recording(directory, task, now)
        session_dir = directory / f"{_stamp(now())}-recording"
        session_dir.mkdir(mode=0o700)
        try:
            _reserve_state(
                directory,
                {"kind": "clip", "pid": os.getpid(), "process_started": own_start,
                 "session": session_dir.name, "started": started, "task": task},
            )  # fmt: skip
        except BaseException:
            shutil.rmtree(session_dir, ignore_errors=True)
            raise
    try:
        driver = attach(profile, open_page=False, timeout_s=DEFAULT_ACTION_TIMEOUT_S)
        try:
            reason = run_recorder(
                driver,
                session_dir,
                hosts=tuple(_declared_hosts(config, task)),
                sign_in_hosts=tuple(sign_in_hosts_for(config, task)),
                stop=lambda: bool(ready_at) and tick() - ready_at[0] >= seconds,
                on_ready=lambda: ready_at.append(tick()),
                max_seconds=seconds + START_WAIT_S,
                clock=now,
                tick=tick,
                masks=covered,
            )
            _write_private(session_dir / "reason", reason.encode())
        finally:
            driver.close()
    except BaseException:
        if not _has_entries(session_dir):
            _discard(directory, session_dir)  # nothing worth keeping
        raise
    try:
        return _finish(
            directory,
            session_dir,
            task=task,
            fmt=fmt,
            out=out,
            speed=speed,
            trim_idle_s=trim_idle_s,
            keep_frames=keep_frames,
            started=started,
            now=now,
            action="record-clip",
        )
    except BaseException as exc:
        # The clip itself was recorded: its state stays (this process is about to end, so the
        # worker looks gone), and `record stop` writes the video again under a free name.
        raise BrowserActionError(
            f"Expected the clip of task {task!r} to be written, got: {exc}; what was recorded "
            f"is kept, and `blumkin browser record stop {task} --out FILE` writes it again"
        ) from exc


def _check_output(fmt: str, out: Path | None, speed: float, trim_idle_s: float | None) -> None:
    """Refuse, before anything is recorded, a file that could not be written at the end."""
    if fmt not in RECORD_FORMATS:
        raise ValueError(f"Expected a format of {', '.join(RECORD_FORMATS)}, got {fmt!r}")
    if speed <= 0:
        raise ValueError(f"Expected a speed above 0, got {speed:g}")
    if trim_idle_s is not None and trim_idle_s <= 0:
        raise ValueError(f"Expected an idle limit above 0 seconds, got {trim_idle_s:g}")
    if fmt == "mp4" and shutil.which("ffmpeg") is None:
        raise ValueError(
            "Expected ffmpeg on PATH to write an MP4, got none; install it or use --format gif"
        )
    if out is not None and os.path.lexists(out):
        raise ValueError(f"Expected a file that does not exist yet, got {out}")
    if out is not None and not Path(out).absolute().parent.is_dir():
        raise ValueError(f"Expected an existing folder for {out}, got none")


def _finish(
    directory: Path,
    session_dir: Path,
    *,
    task: str,
    fmt: str,
    out: Path | None,
    speed: float,
    trim_idle_s: float | None,
    keep_frames: bool,
    started: float,
    now: Callable[[], float],
    action: str,
    narration: NarrationOptions | None = None,
) -> dict[str, Any]:
    """Encode a finished session, clear its working files, and describe the result.

    The video is written to a private temporary file first and published under its name only
    if that name is still free, so nothing this command did not create is overwritten or
    deleted, and no byte of it is ever readable by anyone else.
    """
    stem = session_dir.name.removesuffix("-recording")
    target = out if out is not None else directory / f"{stem}.{fmt}"
    _check_output(fmt, target if out is not None else None, speed, trim_idle_s)
    if os.path.lexists(target):
        raise ValueError(f"Expected a file that does not exist yet, got {target}")
    folder = Path(os.path.realpath(Path(target).absolute().parent))
    inside = Path(os.path.realpath(session_dir))
    if folder == inside or inside in folder.parents:
        raise ValueError(
            f"Expected a destination outside the recording's own folder, got {target}; the "
            "folder is removed once the video is written"
        )
    # In the recording's own folder, which cleanup never touches while it is reserved.
    descriptor, temporary = tempfile.mkstemp(dir=session_dir, prefix=".encoding-", suffix=f".{fmt}")
    os.close(descriptor)  # mkstemp made it 0600
    work = Path(temporary)
    reason = _read_reason(session_dir)  # read before publishing: nothing after it can fail
    try:
        written = encode_recording(
            session_dir,
            work,
            fmt=fmt,
            speed=speed,
            trim_idle_s=trim_idle_s,
            narration=narration,
        )
        _publish(work, target)
    finally:
        with contextlib.suppress(OSError):
            work.unlink(missing_ok=True)
    kept = None
    with contextlib.suppress(OSError):  # the video exists: housekeeping never fails the export
        if keep_frames:
            kept = str(session_dir)
            (session_dir / NARRATION_FILE).unlink(missing_ok=True)  # the words are not kept
            _release_state(directory, session_dir.name)
        else:
            _discard(directory, session_dir)
    return {
        "action": action,
        "ended": reason,
        "frames_dir": kept,
        "ok": True,
        "path": str(target),
        "speed": speed,
        "task": task,
        "untrusted": CAPTURE_NOTE,
        "wall_seconds": round(max(now() - started, 0.0), 1),
        **written,
    }


def _publish(work: Path, target: Path) -> None:
    """Put the finished file at `target` unless something is already there."""
    try:
        os.link(work, target)  # fails if `target` exists: nothing is ever replaced
        return
    except FileExistsError:
        raise ValueError(f"Expected a file that does not exist yet, got {target}") from None
    except OSError:
        pass  # another filesystem, or one without links: copy into a file created for it
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError(f"Expected a file that does not exist yet, got {target}") from None
    try:
        with os.fdopen(descriptor, "wb") as handle, work.open("rb") as source:
            shutil.copyfileobj(source, handle)
    except BaseException:
        target.unlink(missing_ok=True)  # created above, so it is ours to remove
        raise


def recording_status(
    config: Any, task: str, *, now: Callable[[], float] = time.time
) -> dict[str, Any]:
    """Whether the task is being recorded, and for how long."""
    state = _read_state(captures_dir(config.config_dir, task))
    if state is None:
        return {"action": "record-status", "ok": True, "recording": False, "task": task}
    alive = _worker_alive(state)
    return {
        "action": "record-status",
        "ok": True,
        "recording": alive,
        "seconds": round(max(now() - float(state["started"]), 0.0), 1),
        "task": task,
        "unfinished": not alive,
    }


def worker_main(session_dir: Path) -> int:
    """Entry point of the worker process: record until told to stop."""
    meta = json.loads((session_dir / "meta.json").read_text())
    profile = load_profile(Path(meta["config_dir"]), meta["task"])
    if profile is None:
        print(f"no browser profile for task {meta['task']!r}", file=sys.stderr)
        return 2
    stopping = {"now": False}

    def on_term(_signal: int, _frame: Any) -> None:
        stopping["now"] = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    driver = attach(profile, open_page=False, timeout_s=DEFAULT_ACTION_TIMEOUT_S)
    try:
        reason = run_recorder(
            driver,
            session_dir,
            hosts=tuple(meta["hosts"]),
            sign_in_hosts=tuple(meta["sign_in_hosts"]),
            stop=lambda: stopping["now"],
            max_seconds=float(meta["max_seconds"]),
            on_ready=lambda: _write_private(session_dir / "ready", b"1"),
            masks=tuple((str(role), name) for role, name in meta.get("masks", [])),
        )
        _write_private(session_dir / "reason", reason.encode())
    finally:
        driver.close()
    return 0


# --------------------------------------------------------------------------- small helpers


def _ago(state: dict[str, Any], now: Callable[[], float]) -> str:
    return f"{max(now() - float(state['started']), 0.0):.0f} s ago"


def _discard(directory: Path, session_dir: Path) -> None:
    shutil.rmtree(session_dir, ignore_errors=True)
    _release_state(directory, session_dir.name)


def _refuse_if_recording(directory: Path, task: str, now: Callable[[], float]) -> None:
    state = _read_state(directory)
    if state is None:
        return
    if int(state["pid"]) <= 0 and now() - float(state["started"]) < START_WAIT_S * 2:
        raise BrowserActionError(
            f"Expected no recording of task {task!r} to be starting, got one that is"
        )
    if _worker_alive(state):
        raise BrowserActionError(
            f"Expected no recording of task {task!r} to be running, got one since "
            f"{_ago(state, now)}; run `blumkin browser record stop {task}` first"
        )
    raise BrowserActionError(
        f"Expected no unfinished recording of task {task!r}, got one whose worker is gone; "
        f"run `blumkin browser record stop {task}` to keep what it recorded"
    )


def _exporter_alive(exporter: dict[str, Any]) -> bool:
    pid = int(exporter.get("pid", 0))
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError, PermissionError:
        return False
    known = exporter.get("process_started")
    return known is None or _process_started(pid) in (None, known)


def _claim_export(directory: Path, state: dict[str, Any]) -> None:
    """Make this command the one that writes the finished session out (refusing a second)."""
    with task_lock(directory):
        current = _read_state(directory)
        if current is None or current.get("session") != state["session"]:
            raise BrowserActionError(
                f"Expected the recording of task {state['task']!r} to still be there, got none"
            )
        other = current.get("exporting")
        if isinstance(other, dict) and other.get("pid") != os.getpid() and _exporter_alive(other):
            raise BrowserActionError(
                f"Expected no other `record stop` of task {state['task']!r} to be writing the "
                "video, got one that is; wait for it"
            )
        mine = {"pid": os.getpid(), "process_started": _process_started(os.getpid())}
        _write_state_locked(directory, {**current, "exporting": mine})


def _unclaim_export(directory: Path, state: dict[str, Any]) -> None:
    """Give the session back after a failed export, keeping every frame for another try."""
    with task_lock(directory), contextlib.suppress(OSError, BrowserActionError):
        current = _read_state(directory)
        if current is not None and current.get("session") == state["session"]:
            _write_state_locked(
                directory,
                {k: v for k, v in current.items() if k not in ("exporting", "stopping")},
            )


def _clear_stopping(directory: Path, state: dict[str, Any]) -> None:
    """Take the stopping mark off a recording that is still running."""
    with task_lock(directory), contextlib.suppress(OSError, BrowserActionError):
        current = _read_state(directory)
        if current is not None and current.get("session") == state["session"]:
            _write_state_locked(directory, {k: v for k, v in current.items() if k != "stopping"})


def _has_entries(session_dir: Path) -> bool:
    """Whether the session's journal holds at least one frame or card to make a video of."""
    try:
        read_index(session_dir)
    except BrowserActionError:
        return False
    return True


def _release_state(directory: Path, session: str) -> None:
    """Remove the state file, but only if it still names `session` (never another's)."""
    with task_lock(directory):
        _release_state_locked(directory, session)


def _release_state_locked(directory: Path, session: str) -> None:
    state = _read_state(directory)
    if state is not None and state.get("session") == session:
        with contextlib.suppress(OSError):
            (directory / STATE_NAME).unlink()


def _reserve_state(directory: Path, state: dict[str, Any]) -> None:
    """Create the state file for a new recording, failing if one already exists."""
    try:
        descriptor = os.open(directory / STATE_NAME, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise BrowserActionError(
            f"Expected no recording of task {state['task']!r} to exist, got one that was just "
            "started by another command"
        ) from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(state).encode())
    except BaseException:
        (directory / STATE_NAME).unlink(missing_ok=True)  # created above, so it is ours
        raise


def _stop_worker(worker: subprocess.Popen[bytes] | None) -> None:
    """Make sure a worker this command started is gone."""
    if worker is None:
        return
    with contextlib.suppress(OSError):
        worker.kill()
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        worker.wait(timeout=KILL_WAIT_S)


def _open_private(path: Path, mode: str) -> Any:
    descriptor = os.open(
        path, os.O_CREAT | os.O_WRONLY | (os.O_APPEND if mode == "a" else os.O_TRUNC), 0o600
    )
    return os.fdopen(descriptor, mode)


def _quote(path: Path) -> str:
    # ffmpeg resolves a relative entry against the playlist, not the working directory.
    return os.path.abspath(path).replace("'", "'\\''")


def _read_reason(session_dir: Path) -> str:
    path = session_dir / "reason"
    return path.read_text().strip() if path.is_file() else "worker ended early"


def _read_state(directory: Path) -> dict[str, Any] | None:
    path = directory / STATE_NAME
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text())
    except ValueError:
        return None
    return (
        state if isinstance(state, dict) and {"pid", "session", "started"} <= set(state) else None
    )


def _srt_time(seconds: float) -> str:
    whole = int(seconds)
    millis = round((seconds - whole) * 1000)
    if millis == 1000:
        whole, millis = whole + 1, 0
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d},{millis:03d}"


def _stamp(moment: float) -> str:
    from datetime import UTC, datetime

    when = datetime.fromtimestamp(moment, UTC)
    return when.strftime("%Y%m%dT%H%M%S") + f"{when.microsecond // 1000:03d}Z"


def _process_started(pid: int) -> str | None:
    """When the process began, as `ps` prints it (None when that cannot be read)."""
    try:
        done = subprocess.run(  # noqa: S603, S607 - fixed command
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return done.stdout.strip() or None


def _worker_alive(state: dict[str, Any]) -> bool:
    """Whether the recorded pid is still this module's worker (a reused pid is not)."""
    pid = int(state["pid"])
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    if state.get("kind") == "clip":
        # A `record clip` is the command's own process, not the worker module; the pid alone
        # could be another process by now, so its start time must match too.
        known = state.get("process_started")
        return known is None or _process_started(pid) in (None, known)
    try:
        done = subprocess.run(  # noqa: S603, S607 - fixed command
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return True
    return "blumkin.browser.recording" in done.stdout and str(state["session"]) in done.stdout


def _worker_problem(session_dir: Path) -> str:
    log = session_dir / "worker.log"
    if log.is_file():
        lines = [line for line in log.read_text().splitlines() if line.strip()]
        if lines:
            return lines[-1][:200]
    return "the recorder did not start in time"


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def _write_state(directory: Path, state: dict[str, Any]) -> None:
    """Replace the state file with `state`, only while it still names the same session.

    The new content is written to a private sibling first, so a failed write leaves the
    reservation as it was.
    """
    with task_lock(directory):
        _write_state_locked(directory, state)


def _write_state_locked(directory: Path, state: dict[str, Any]) -> None:
    current = _read_state(directory)
    if current is None or current.get("session") != state["session"]:
        raise BrowserActionError(
            f"Expected the recording of task {state['task']!r} to still be ours, got none"
        )
    scratch = directory / f"{STATE_NAME}.{os.getpid()}.new"
    try:
        _write_private(scratch, json.dumps(state).encode())
        os.replace(scratch, directory / STATE_NAME)
    finally:
        with contextlib.suppress(OSError):
            scratch.unlink()


if __name__ == "__main__":
    raise SystemExit(worker_main(Path(sys.argv[1])))
