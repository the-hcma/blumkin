"""The Teams transcript page adapter: parser, merge, stop marker and its boundary (issue #543)."""

from __future__ import annotations

from pathlib import Path

import blumkin.browser
from blumkin.browser.teams import (
    has_stop_marker,
    merge_chunks,
    parse_chunk,
    transcription_stopped_after_capture,
)
from blumkin.browser.transcript import Entry, assess

PANE_TOP = """
Lovelace, Ada (Example Corp)

 started transcription
Lovelace, Ada (Example Corp)
0 minutes 24 seconds
0:24
Lovelace, Ada (Example Corp) 0 minutes 24 seconds
Alright, I think we're back.
AT
Turing, Alan (Example Corp) {Contractor}
0 minutes 25 seconds
0:25
Turing, Alan (Example Corp) {Contractor} 0 minutes 25 seconds
Okay, you working?
Lovelace, Ada (Example Corp) 0 minutes 28 seconds
So I understand from Grace, right?
"""

PANE_NEXT = """
Turing, Alan (Example Corp) {Contractor}
0 minutes 25 seconds
0:25
Turing, Alan (Example Corp) {Contractor} 0 minutes 25 seconds
Okay, you working?
Lovelace, Ada (Example Corp) 0 minutes 28 seconds
So I understand from Grace, right?
AT
Turing, Alan (Example Corp) {Contractor}
1 minutes 4 seconds
1:04
Turing, Alan (Example Corp) {Contractor} 1 minutes 4 seconds
No, we have not started.
Second line of the same turn.



Lovelace, Ada (Example Corp)

 stopped transcription
"""

SPEAKER_B = "Turing, Alan (Example Corp) {Contractor}"
SPEAKER_A = "Lovelace, Ada (Example Corp)"


def test_parse_chunk_reads_headers_and_drops_the_repeated_lines() -> None:
    entries = parse_chunk(PANE_TOP)

    assert entries == [
        Entry(SPEAKER_A, 24, "Alright, I think we're back."),
        Entry(SPEAKER_B, 25, "Okay, you working?"),
        Entry(SPEAKER_A, 28, "So I understand from Grace, right?"),
    ]


def test_merge_chunks_keeps_overlap_once_and_the_order_of_first_sight() -> None:
    merged = merge_chunks([PANE_TOP, PANE_NEXT])

    assert [(e.start_s, e.speaker) for e in merged] == [
        (24, SPEAKER_A),
        (25, SPEAKER_B),
        (28, SPEAKER_A),
        (64, SPEAKER_B),
    ]
    assert merged[-1].text == "No, we have not started. Second line of the same turn."


def test_has_stop_marker_needs_the_event_line_not_the_words_in_speech() -> None:
    assert has_stop_marker([PANE_NEXT])
    assert not has_stop_marker(["A 1 seconds\nthey stopped transcription yesterday"])
    assert not has_stop_marker([PANE_NEXT, PANE_TOP])


def test_transcription_stopped_after_capture_ignores_an_earlier_stop() -> None:
    early_stop = (
        f"{SPEAKER_A}\n0 minutes 10 seconds\n0:10\n{SPEAKER_A} 0 minutes 10 seconds\n"
        "Mid-meeting.\n\n stopped transcription\n"
    )
    continued = (
        f"{SPEAKER_A}\n0 minutes 10 seconds\n0:10\n{SPEAKER_A} 0 minutes 10 seconds\n"
        "Mid-meeting.\n"
        f"{SPEAKER_B}\n1 minutes 0 seconds\n1:00\n{SPEAKER_B} 1 minutes 0 seconds\n"
        "Still going.\n"
    )
    entries = merge_chunks([early_stop, continued])

    assert not transcription_stopped_after_capture([early_stop, continued], entries)
    assert transcription_stopped_after_capture([PANE_NEXT], merge_chunks([PANE_NEXT]))


def test_parse_chunk_keeps_a_speaker_name_in_speech_without_following_metadata() -> None:
    pane = f"""
{SPEAKER_A} 0 minutes 5 seconds
We should ask {SPEAKER_B} about it.
{SPEAKER_A} 0 minutes 8 seconds
Done.
"""
    entries = parse_chunk(pane)

    assert len(entries) == 2
    assert SPEAKER_B in entries[0].text


def test_parse_chunk_skips_continuation_speech_that_only_starts_like_a_header() -> None:
    pane = f"""
it took 2 minutes 5 seconds to run
{SPEAKER_A} 0 minutes 8 seconds
Done.
"""
    entries = parse_chunk(pane)

    assert entries == [Entry(SPEAKER_A, 8, "Done.")]


def test_parse_chunk_keeps_duration_shaped_speech_inside_the_active_entry() -> None:
    pane = f"""
{SPEAKER_A}
0 minutes 5 seconds
0:05
{SPEAKER_A} 0 minutes 5 seconds
Please wait.
give it 30 seconds
Then retry.
1 minutes 4 seconds
{SPEAKER_B}
0 minutes 40 seconds
0:40
{SPEAKER_B} 0 minutes 40 seconds
Okay.
"""
    entries = parse_chunk(pane)

    assert entries == [
        Entry(
            SPEAKER_A,
            5,
            "Please wait. give it 30 seconds Then retry. 1 minutes 4 seconds",
        ),
        Entry(SPEAKER_B, 40, "Okay."),
    ]


def test_assess_still_flags_tail_when_only_an_earlier_stop_was_seen() -> None:
    early_stop = (
        f"{SPEAKER_A}\n0 minutes 10 seconds\n0:10\n{SPEAKER_A} 0 minutes 10 seconds\n"
        "Only this turn.\n\n stopped transcription\n"
    )
    later = (
        f"{SPEAKER_A}\n0 minutes 10 seconds\n0:10\n{SPEAKER_A} 0 minutes 10 seconds\n"
        "Only this turn.\n"
    )
    entries = merge_chunks([early_stop, later])
    report = assess(
        entries,
        duration_s=2046,
        gap_threshold_s=2000,
        reached_end=True,
        reached_top=True,
        transcription_stopped=transcription_stopped_after_capture([early_stop, later], entries),
    )

    assert any("tail missing" in problem for problem in report.problems)


def test_the_teams_page_specifics_live_only_in_the_adapter() -> None:
    """A change in the Teams page is one fix: its markers appear in no other browser module."""
    browser_dir = Path(blumkin.browser.__file__).parent
    markers = ("stopped transcription", "started transcription", "minutes? ", "scrollHeight")
    for marker in markers:
        holders = {
            path.name for path in browser_dir.glob("*.py") if marker in path.read_text("utf-8")
        }
        assert holders == {"teams.py"}, (marker, holders)
