"""Turn the text a scrolled transcript pane yields into ordered entries and a coverage report.

Teams renders a meeting transcript in a virtualized list: only the rows near the viewport
exist in the page, so one read sees a fraction of a long meeting. The browser layer scrolls
the pane and hands over every chunk of text it saw; this module parses the chunks, merges
the overlap between them and says what is still missing, so a gap is reported instead of
being silently absent from the result.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: A silence this long between two entries is reported as a possible gap.
DEFAULT_GAP_THRESHOLD_S = 180


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """What the capture covers and where it may be incomplete."""

    complete: bool
    duration_s: int | None
    entries: int
    first_s: int | None
    gaps: tuple[tuple[int, int], ...]
    last_s: int | None
    out_of_order: int
    problems: tuple[str, ...]
    speakers: tuple[str, ...]
    #: Teams logged "stopped transcription" at the end, so a tail shorter than the meeting is real.
    transcription_stopped: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "duration_s": self.duration_s,
            "entries": self.entries,
            "first_s": self.first_s,
            "gaps": [{"from_s": a, "to_s": b} for a, b in self.gaps],
            "last_s": self.last_s,
            "out_of_order": self.out_of_order,
            "problems": list(self.problems),
            "speakers": list(self.speakers),
            "transcription_stopped": self.transcription_stopped,
        }


@dataclass(frozen=True, slots=True)
class Entry:
    """One spoken turn: who, when (seconds from the start) and what was said."""

    speaker: str
    start_s: int
    text: str


def assess(
    entries: Sequence[Entry],
    *,
    duration_s: int | None = None,
    gap_threshold_s: int = DEFAULT_GAP_THRESHOLD_S,
    reached_end: bool,
    reached_top: bool,
    transcription_stopped: bool = False,
) -> CoverageReport:
    """Check the merged entries for gaps against what the scroll saw and the meeting length."""
    problems: list[str] = []
    if not entries:
        problems.append("no transcript entries were captured")
    if not reached_top:
        problems.append("the pane was not scrolled back to its first entry")
    if not reached_end:
        problems.append("the scroll stopped before the end of the pane (step limit or timeout)")
    times = [entry.start_s for entry in entries]
    out_of_order = sum(1 for before, after in zip(times, times[1:], strict=False) if after < before)
    if out_of_order:
        problems.append(f"{out_of_order} entries are earlier than the one before them")
    gaps = tuple(
        (before, after)
        for before, after in zip(times, times[1:], strict=False)
        if after - before > gap_threshold_s
    )
    if gaps:
        problems.append(f"{len(gaps)} silences longer than {gap_threshold_s}s (possible gaps)")
    first = times[0] if times else None
    last = times[-1] if times else None
    if duration_s is not None and last is not None:
        if first is not None and first > gap_threshold_s:
            problems.append(f"the first entry is at {first}s, not near the start")
        if duration_s - last > gap_threshold_s and not transcription_stopped:
            problems.append(
                f"the last entry is at {last}s but the meeting ran {duration_s}s (tail missing?)"
            )
    speakers = tuple(sorted({entry.speaker for entry in entries}))
    return CoverageReport(
        complete=not problems,
        duration_s=duration_s,
        entries=len(entries),
        first_s=first,
        gaps=gaps,
        last_s=last,
        out_of_order=out_of_order,
        problems=tuple(problems),
        speakers=speakers,
        transcription_stopped=transcription_stopped,
    )


def format_clock(seconds: int) -> str:
    """`m:ss`, or `h:mm:ss` from one hour on."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_text(entries: Iterable[Entry]) -> str:
    """One line per entry: `[m:ss] Speaker: text`."""
    return "\n".join(f"[{format_clock(e.start_s)}] {e.speaker}: {e.text}" for e in entries)


def has_stop_marker(chunks: Iterable[str]) -> bool:
    """Whether any chunk shows Teams' own "stopped transcription" event (the real end)."""
    return any(_STOPPED.search(chunk) for chunk in chunks)


def merge_chunks(chunks: Iterable[str]) -> list[Entry]:
    """Parse every scroll chunk and merge them, keeping the order each entry first appeared in.

    Adjacent chunks overlap, so an entry seen twice is kept once. Entries are keyed on
    start time, speaker and text; a different utterance by the same person in the same
    second is therefore kept, and the same utterance seen again is not.
    """
    seen: set[tuple[int, str, str]] = set()
    merged: list[Entry] = []
    for chunk in chunks:
        for entry in parse_chunk(chunk):
            key = (entry.start_s, entry.speaker, entry.text)
            if key not in seen:
                seen.add(key)
                merged.append(entry)
    return merged


def parse_chunk(text: str) -> list[Entry]:
    """Parse the visible text of the transcript pane into entries.

    Each entry carries a header line, `Speaker 4 minutes 12 seconds`, followed by the words
    spoken. Teams also renders the speaker, the duration and a `4:12` clock as separate
    lines before the first entry of a run and an avatar's initials before a new speaker;
    those lines carry nothing the header does not, so they end an entry's text and are dropped.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    speakers = {m.group("speaker") for line in lines if (m := _HEADER.match(line))}
    entries: list[Entry] = []
    index = 0
    while index < len(lines):
        header = _HEADER.match(lines[index])
        bare = _BARE_DURATION.fullmatch(lines[index]) is not None
        index += 1
        if header is None or bare:
            continue
        spoken: list[str] = []
        while index < len(lines) and not _is_boundary(lines, index, speakers):
            spoken.append(lines[index])
            index += 1
        if spoken:
            entries.append(
                Entry(
                    speaker=header.group("speaker"),
                    start_s=_duration_seconds(header.group("duration")),
                    text=" ".join(spoken),
                )
            )
    return entries


def parse_duration(text: str) -> int:
    """Seconds in `34m`, `1h05m`, `34:06`, `1:02:03` or a bare number of seconds."""
    clock = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", text.strip())
    if clock:
        return int(clock.group(1) or 0) * 3600 + int(clock.group(2)) * 60 + int(clock.group(3))
    words = re.fullmatch(
        r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", text.strip(), re.IGNORECASE
    )
    if words and any(words.groups()):
        hours, minutes, seconds = (int(part or 0) for part in words.groups())
        return hours * 3600 + minutes * 60 + seconds
    if text.strip().isdigit():
        return int(text.strip())
    raise ValueError(f"Expected a duration like 34m, 1h05m, 34:06 or seconds, got {text!r}")


_DURATION = r"(?:\d+ hours? )?(?:\d+ minutes? )?\d+ seconds?"
_BARE_CLOCK = re.compile(r"\d{1,2}(?::\d{2}){1,2}")
_BARE_DURATION = re.compile(_DURATION)
_HEADER = re.compile(rf"(?P<speaker>.+?) (?P<duration>{_DURATION})")
_INITIALS = re.compile(r"[A-Z]{1,3}")
_STOPPED = re.compile(r"^\s*stopped transcription\s*$", re.MULTILINE)
_UNIT_SECONDS = {"hour": 3600, "minute": 60, "second": 1}


def _duration_seconds(text: str) -> int:
    return sum(
        int(number) * _UNIT_SECONDS[unit]
        for number, unit in re.findall(r"(\d+) (hour|minute|second)", text)
    )


def _is_boundary(lines: Sequence[str], index: int, speakers: set[str]) -> bool:
    line = lines[index]
    if _HEADER.fullmatch(line) or _BARE_DURATION.fullmatch(line) or _BARE_CLOCK.fullmatch(line):
        return True
    if line in speakers:
        return True
    return (
        _INITIALS.fullmatch(line) is not None
        and index + 1 < len(lines)
        and lines[index + 1] in speakers
    )
