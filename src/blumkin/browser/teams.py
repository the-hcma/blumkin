"""The Teams page adapter: everything that knows what a Teams transcript pane looks like.

The browser layer is page-independent; the facts about Teams live here so a change in the page
is one fix in this module: the scripts that find and scroll the transcript pane, the parser for
its text (a header line per turn, the repeated speaker, duration and clock lines, avatar
initials) and the "started" and "stopped transcription" event lines.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from blumkin.browser.transcript import Entry, merge_entries

FRAME_READY_JS = (
    "() => { const text = document.body ? document.body.innerText : ''; "
    "return /\\d+ minutes? \\d+ seconds?/.test(text); }"
)


SCROLL_PREPARE_JS = (
    "() => { const text = document.body ? document.body.innerText : ''; "
    "const hasTurn = /\\d+ minutes? \\d+ seconds?/.test(text); "
    "const els = [...document.querySelectorAll('*')].filter(e => "
    "e.scrollHeight > e.clientHeight + 50 && "
    "['auto', 'scroll'].includes(getComputedStyle(e).overflowY)); "
    "els.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight)); "
    "let scroller = els[0] || null; "
    "if (!scroller) { const root = document.scrollingElement; "
    "if (root && root.scrollHeight > root.clientHeight + 50) scroller = root; } "
    "window.__blumkinScroller = scroller; "
    "if (scroller) return 'scroll'; "
    "if (hasTurn) return 'fixed'; "
    "return 'missing'; }"
)


SCROLL_STEP_JS = (
    "() => { const e = window.__blumkinScroller; if (!e || !e.isConnected) return null; "
    "if (e.scrollTop + e.clientHeight >= e.scrollHeight - 2) return true; "
    "e.scrollTop = Math.min(e.scrollTop + Math.max(e.clientHeight * 0.7, 50), e.scrollHeight); "
    "return false; }"
)


SCROLL_TO_TOP_JS = (
    "() => { const e = window.__blumkinScroller; if (!e || !e.isConnected) return false; "
    "e.scrollTop = 0; return e.scrollTop === 0; }"
)


def has_stop_marker(chunks: Iterable[str]) -> bool:
    """Whether the last scroll chunk shows Teams' "stopped transcription" event line."""
    chunks_list = tuple(chunks)
    if not chunks_list:
        return False
    return _STOPPED.search(chunks_list[-1]) is not None


def merge_chunks(chunks: Iterable[str]) -> list[Entry]:
    """Parse every scroll chunk and merge them, keeping the order each entry first appeared in."""
    return merge_entries(parse_chunk(chunk) for chunk in chunks)


def parse_chunk(text: str) -> list[Entry]:
    """Parse the visible text of the transcript pane into entries.

    Each entry carries a header line, `Speaker 4 minutes 12 seconds`, followed by the words
    spoken. Teams also renders the speaker, the duration and a `4:12` clock as separate
    lines before the first entry of a run and an avatar's initials before a new speaker;
    those lines carry nothing the header does not, so they end an entry's text and are dropped.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    speakers = _metadata_speakers(lines)
    allow_unknown_headers = not speakers
    entries: list[Entry] = []
    index = 0
    while index < len(lines):
        header = _HEADER.fullmatch(lines[index])
        bare = _BARE_DURATION.fullmatch(lines[index]) is not None
        index += 1
        if header is None or bare:
            continue
        speaker = header.group("speaker")
        if not _accept_header_speaker(speaker, speakers, allow_unknown_headers):
            continue
        speakers.add(speaker)
        spoken: list[str] = []
        while index < len(lines) and not _is_boundary(
            lines, index, speakers, allow_unknown_headers
        ):
            spoken.append(lines[index])
            index += 1
        if spoken:
            entries.append(
                Entry(
                    speaker=speaker,
                    start_s=_duration_seconds(header.group("duration")),
                    text=" ".join(spoken),
                )
            )
    return entries


def transcription_stopped_after_capture(chunks: Iterable[str], entries: Sequence[Entry]) -> bool:
    """Whether transcription stopped after the last captured turn, not at an earlier stop."""
    if not entries:
        return False
    chunks_list = tuple(chunks)
    if not chunks_list:
        return False
    last_chunk = chunks_list[-1]
    stop = _STOPPED.search(last_chunk)
    if stop is None:
        return False
    last_entry = entries[-1]
    last_turn_at = -1
    for match in _HEADER.finditer(last_chunk):
        if match.group("speaker") != last_entry.speaker:
            continue
        if _duration_seconds(match.group("duration")) != last_entry.start_s:
            continue
        last_turn_at = match.start()
    if last_turn_at < 0:
        return False
    return stop.start() > last_turn_at


_DURATION = r"(?:\d+ hours? )?(?:\d+ minutes? )?\d+ seconds?"
_BARE_CLOCK = re.compile(r"\d{1,2}(?::\d{2}){1,2}")
_BARE_DURATION = re.compile(_DURATION)
_HEADER = re.compile(rf"(?P<speaker>.+?) (?P<duration>{_DURATION})")
_INITIALS = re.compile(r"[A-Z]{1,3}")
_STARTED = re.compile(r"^\s*started transcription\s*$", re.IGNORECASE)
_STOPPED = re.compile(r"^\s*stopped transcription\s*$", re.MULTILINE | re.IGNORECASE)
_UNIT_SECONDS = {"hour": 3600, "minute": 60, "second": 1}


def _accept_header_speaker(speaker: str, speakers: set[str], allow_unknown_headers: bool) -> bool:
    if _duration_shaped_speaker(speaker):
        return False
    if speaker in speakers:
        return True
    return allow_unknown_headers


def _duration_seconds(text: str) -> int:
    return sum(
        int(number) * _UNIT_SECONDS[unit]
        for number, unit in re.findall(r"(\d+) (hour|minute|second)", text)
    )


def _duration_shaped_speaker(speaker: str) -> bool:
    return (
        _BARE_DURATION.fullmatch(speaker) is not None
        or re.fullmatch(r"\d+(?: hours?| minutes?| seconds?)+", speaker) is not None
    )


def _follows_speaker_metadata(lines: Sequence[str], speaker_index: int) -> bool:
    if speaker_index + 1 >= len(lines):
        return False
    next_line = lines[speaker_index + 1]
    if _STARTED.fullmatch(next_line) or _STOPPED.fullmatch(next_line):
        return True
    if _BARE_CLOCK.fullmatch(next_line):
        return True
    if _BARE_DURATION.fullmatch(next_line) is None:
        return False
    # A bare duration alone can be speech ("1 minutes 4 seconds"); Teams chrome pairs it
    # with a clock or the same speaker's combined header on the next line.
    if speaker_index + 2 >= len(lines):
        return False
    after = lines[speaker_index + 2]
    if _BARE_CLOCK.fullmatch(after):
        return True
    header = _HEADER.fullmatch(after)
    return header is not None and header.group("speaker") == lines[speaker_index]


def _is_boundary(
    lines: Sequence[str],
    index: int,
    speakers: set[str],
    allow_unknown_headers: bool,
) -> bool:
    line = lines[index]
    header = _HEADER.fullmatch(line)
    if header is not None and _accept_header_speaker(
        header.group("speaker"), speakers, allow_unknown_headers
    ):
        return True
    if _BARE_CLOCK.fullmatch(line) or _STARTED.fullmatch(line) or _STOPPED.fullmatch(line):
        return True
    if line in speakers and _follows_speaker_metadata(lines, index):
        return True
    if (
        _INITIALS.fullmatch(line) is not None
        and index + 2 < len(lines)
        and lines[index + 1] in speakers
        and _follows_speaker_metadata(lines, index + 1)
    ):
        return True
    return False


def _is_chrome_line(line: str) -> bool:
    return (
        _BARE_DURATION.fullmatch(line) is not None
        or _BARE_CLOCK.fullmatch(line) is not None
        or _STARTED.fullmatch(line) is not None
        or _STOPPED.fullmatch(line) is not None
        or _INITIALS.fullmatch(line) is not None
        or _HEADER.fullmatch(line) is not None
    )


def _metadata_speakers(lines: Sequence[str]) -> set[str]:
    return {
        line
        for index, line in enumerate(lines)
        if not _is_chrome_line(line) and _follows_speaker_metadata(lines, index)
    }
