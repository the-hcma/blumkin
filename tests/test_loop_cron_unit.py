"""Cron due-time evaluation and catch-up (issue #502)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from blumkin.loop.config import LoopConfigError, LoopSetup, parse_loop_setup
from blumkin.loop.cron import due_fires, mark_due_consumed, next_due_at_or_before
from blumkin.loop.handlers import default_handlers
from blumkin.loop.runner import run_trigger
from blumkin.loop.types import RunOutcome, TriggerKind, TriggerSpec


def _eod(tmp_path: Path, *, hours: float | None = 8.0) -> LoopSetup:
    table: dict = {
        "task": "time-sheet",
        "instance": "current",
        "schedule": "0 18 * * 1-5",
        "timezone": "UTC",
        "working_days_only": True,
    }
    if hours is not None:
        table["default_hours"] = hours
    return parse_loop_setup(tmp_path, {"cron": {"eod": table}})


def test_next_due_finds_weekday_1800_utc() -> None:
    spec = TriggerSpec(
        instance="current",
        kind=TriggerKind.CRON,
        name="eod",
        schedule="0 18 * * 1-5",
        task="time-sheet",
        timezone="UTC",
        working_days_only=True,
    )
    # Wednesday 2026-10-21 19:00 UTC — previous due was 18:00 same day.
    now = datetime(2026, 10, 21, 19, 0, tzinfo=UTC)
    due = next_due_at_or_before(spec, now, after=None)
    assert due == datetime(2026, 10, 21, 18, 0, tzinfo=UTC)


def test_working_days_only_skips_weekend() -> None:
    spec = TriggerSpec(
        instance="current",
        kind=TriggerKind.CRON,
        name="eod",
        schedule="0 18 * * *",
        task="time-sheet",
        timezone="UTC",
        working_days_only=True,
    )
    # Sunday evening — latest working-day due is Friday 18:00.
    now = datetime(2026, 10, 25, 20, 0, tzinfo=UTC)
    due = next_due_at_or_before(spec, now, after=None)
    assert due == datetime(2026, 10, 23, 18, 0, tzinfo=UTC)


def test_catch_up_once_then_no_repeat(tmp_path: Path) -> None:
    setup = _eod(tmp_path)
    now = datetime(2026, 10, 21, 19, 0, tzinfo=UTC)
    fires = due_fires(setup.triggers, config_dir=tmp_path, now=now)
    assert len(fires) == 1
    mark_due_consumed(tmp_path, "eod", fires[0].due_at)
    assert due_fires(setup.triggers, config_dir=tmp_path, now=now) == []


def test_paused_trigger_not_in_due_fires(tmp_path: Path) -> None:
    setup = _eod(tmp_path)
    now = datetime(2026, 10, 21, 19, 0, tzinfo=UTC)
    fires = due_fires(setup.triggers, config_dir=tmp_path, now=now, paused=frozenset({"eod"}))
    assert fires == []


def test_time_sheet_needs_user_without_hours(tmp_path: Path) -> None:
    setup = _eod(tmp_path, hours=None)
    result = run_trigger(setup, "eod", handlers=default_handlers())
    assert result.record is not None
    assert result.record.outcome is RunOutcome.NEEDS_USER


def test_time_sheet_done_with_declared_hours(tmp_path: Path) -> None:
    setup = _eod(tmp_path, hours=8.0)
    result = run_trigger(setup, "eod", handlers=default_handlers())
    assert result.record is not None
    assert result.record.outcome is RunOutcome.DONE


def test_invalid_cron_rejected_at_parse(tmp_path: Path) -> None:
    with pytest.raises(LoopConfigError):
        parse_loop_setup(
            tmp_path,
            {
                "cron": {
                    "bad": {
                        "task": "time-sheet",
                        "instance": "current",
                        "schedule": "not a cron",
                        "timezone": "UTC",
                    }
                }
            },
        )


def test_monthly_schedule_catch_up_beyond_eight_days() -> None:
    spec = TriggerSpec(
        instance="current",
        kind=TriggerKind.CRON,
        name="monthly",
        schedule="0 18 1 * *",
        task="time-sheet",
        timezone="UTC",
        working_days_only=False,
    )
    # Mid-month: latest due is the 1st at 18:00.
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    due = next_due_at_or_before(spec, now, after=None)
    assert due == datetime(2026, 10, 1, 18, 0, tzinfo=UTC)


def test_dom_or_dow_when_both_restricted() -> None:
    spec = TriggerSpec(
        instance="current",
        kind=TriggerKind.CRON,
        name="either",
        schedule="0 18 1 * 1",
        task="time-sheet",
        timezone="UTC",
        working_days_only=False,
    )
    # Monday 2026-10-19 is not the 1st — still due under OR rule.
    now = datetime(2026, 10, 19, 19, 0, tzinfo=UTC)
    due = next_due_at_or_before(spec, now, after=None)
    assert due == datetime(2026, 10, 19, 18, 0, tzinfo=UTC)


def test_sunday_seven_accepted() -> None:
    spec = TriggerSpec(
        instance="current",
        kind=TriggerKind.CRON,
        name="sun",
        schedule="0 18 * * 7",
        task="time-sheet",
        timezone="UTC",
        working_days_only=False,
    )
    # Sunday 2026-10-25 19:00 UTC.
    now = datetime(2026, 10, 25, 19, 0, tzinfo=UTC)
    due = next_due_at_or_before(spec, now, after=None)
    assert due == datetime(2026, 10, 25, 18, 0, tzinfo=UTC)


def test_impossible_dom_rejected(tmp_path: Path) -> None:
    with pytest.raises(LoopConfigError, match="never occurs"):
        parse_loop_setup(
            tmp_path,
            {
                "cron": {
                    "bad": {
                        "task": "time-sheet",
                        "instance": "current",
                        "schedule": "0 18 31 2 *",
                        "timezone": "UTC",
                    }
                }
            },
        )


def test_impossible_dom_allowed_when_dow_also_restricted(tmp_path: Path) -> None:
    # DOM 31 Feb never occurs, but DOW alternative can still fire (POSIX OR).
    setup = parse_loop_setup(
        tmp_path,
        {
            "cron": {
                "either": {
                    "task": "time-sheet",
                    "instance": "current",
                    "schedule": "0 18 31 2 1",
                    "timezone": "UTC",
                }
            }
        },
    )
    assert "either" in setup.triggers


def test_mark_due_keeps_later_timestamp(tmp_path: Path) -> None:
    later = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
    earlier = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)
    mark_due_consumed(tmp_path, "eod", later)
    mark_due_consumed(tmp_path, "eod", earlier)
    from blumkin.loop.cron import load_last_due

    assert load_last_due(tmp_path)["eod"] == later


def test_default_hours_rejects_nan(tmp_path: Path) -> None:
    with pytest.raises(LoopConfigError, match="finite"):
        parse_loop_setup(
            tmp_path,
            {
                "cron": {
                    "eod": {
                        "task": "time-sheet",
                        "instance": "current",
                        "schedule": "0 18 * * 1-5",
                        "timezone": "UTC",
                        "default_hours": float("nan"),
                    }
                }
            },
        )


def test_timezone_shifts_local_wall_clock() -> None:
    spec = TriggerSpec(
        instance="current",
        kind=TriggerKind.CRON,
        name="eod",
        schedule="0 18 * * 1-5",
        task="time-sheet",
        timezone="America/Toronto",
        working_days_only=False,
    )
    # 2026-10-21 22:00 UTC = 18:00 America/Toronto (EDT, UTC-4).
    now = datetime(2026, 10, 21, 22, 30, tzinfo=UTC)
    due = next_due_at_or_before(spec, now, after=None)
    assert due is not None
    assert due.astimezone(ZoneInfo("America/Toronto")).hour == 18
