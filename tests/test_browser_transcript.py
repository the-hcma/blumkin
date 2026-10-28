"""Transcript coverage report, formatting and duration parsing (the page-independent part)."""

from __future__ import annotations

from pathlib import Path

import pytest

from blumkin.browser.actions import HostBlockedError, transcript_task
from blumkin.browser.cdp import BrowserActionError
from blumkin.browser.transcript import (
    Entry,
    assess,
    format_text,
    parse_duration,
)
from blumkin.config import BrowserConfig, BrowserSetup, BrowserTaskPolicy


def test_assess_flags_a_missing_middle_a_short_tail_and_an_unfinished_scroll() -> None:
    entries = [Entry("A", 24, "x"), Entry("A", 400, "y"), Entry("B", 900, "z")]

    report = assess(entries, duration_s=2000, reached_end=False, reached_top=True)

    assert not report.complete
    assert report.gaps == ((24, 400), (400, 900))
    assert any("tail missing" in problem for problem in report.problems)
    assert any("step limit" in problem for problem in report.problems)


def test_assess_ignores_tail_after_teams_logged_that_transcription_stopped() -> None:
    entries = [Entry("A", 24, "x"), Entry("A", 100, "y"), Entry("B", 400, "z")]

    report = assess(
        entries,
        duration_s=2046,
        gap_threshold_s=300,
        reached_end=True,
        reached_top=True,
        transcription_stopped=True,
    )

    assert not any("tail" in problem for problem in report.problems)
    assert report.out_of_order == 0


def test_assess_reports_entries_that_go_back_in_time() -> None:
    report = assess([Entry("A", 100, "x"), Entry("A", 90, "y")], reached_end=True, reached_top=True)

    assert report.out_of_order == 1
    assert not report.complete


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("34m06s", 2046), ("1h05m", 3900), ("34:06", 2046), ("1:02:03", 3723), ("90", 90)],
)
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


def test_parse_duration_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="Expected a duration"):
        parse_duration("soon")


def test_format_text_is_one_line_per_turn() -> None:
    assert format_text([Entry("A", 3725, "hi")]) == "[1:02:05] A: hi"


def test_transcript_task_refuses_without_allowed_hosts(tmp_path: Path) -> None:
    config = BrowserSetup(browser=BrowserConfig(), config_dir=tmp_path)

    with pytest.raises(HostBlockedError, match="allowed_hosts"):
        transcript_task(config, "teams", duration_s=None, out=None)


def test_transcript_task_never_overwrites_the_output(tmp_path: Path) -> None:
    existing = tmp_path / "t.txt"
    existing.write_text("keep")
    config = _config_with_hosts(tmp_path)

    with pytest.raises(BrowserActionError, match="new file"):
        transcript_task(config, "teams", duration_s=None, out=existing)

    assert existing.read_text() == "keep"


def _config_with_hosts(tmp_path: Path) -> BrowserSetup:
    policy = BrowserTaskPolicy(allowed_hosts=("*.sharepoint.com",))
    return BrowserSetup(browser=BrowserConfig(tasks={"teams": policy}), config_dir=tmp_path)
