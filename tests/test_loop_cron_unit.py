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
