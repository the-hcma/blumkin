"""Mutual free-slot suggestions from freebusy busy blocks."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from blumkin.skills.calendar import (
    calendar_suggest,
    find_mutual_free_slots,
    format_suggest_human,
)

_TZ = ZoneInfo("America/New_York")


def test_find_mutual_free_slots_skips_busy_and_respects_window() -> None:
    start = datetime(2026, 8, 28, 9, 0, tzinfo=_TZ)
    end = datetime(2026, 8, 28, 17, 0, tzinfo=_TZ)
    busy = [
        (
            datetime(2026, 8, 28, 10, 0, tzinfo=_TZ),
            datetime(2026, 8, 28, 11, 0, tzinfo=_TZ),
        )
    ]
    slots = find_mutual_free_slots(
        busy=busy,
        range_start=start,
        range_end=end,
        duration=timedelta(minutes=45),
        window=(datetime(2026, 8, 28, 9, 0).time(), datetime(2026, 8, 28, 12, 0).time()),
        step=timedelta(minutes=15),
        limit=5,
    )
    assert slots[0]["start"] == datetime(2026, 8, 28, 9, 0, tzinfo=_TZ).isoformat()
    assert slots[0]["end"] == datetime(2026, 8, 28, 9, 45, tzinfo=_TZ).isoformat()
    # 10:00–11:00 busy: first start after the busy block that still fits in the window.
    assert any(s["start"] == datetime(2026, 8, 28, 11, 0, tzinfo=_TZ).isoformat() for s in slots)
    assert all(
        datetime.fromisoformat(s["end"]).timetz().replace(tzinfo=None).hour < 12
        or (
            datetime.fromisoformat(s["end"]).timetz().replace(tzinfo=None).hour == 12
            and datetime.fromisoformat(s["end"]).timetz().replace(tzinfo=None).minute == 0
        )
        for s in slots
    )


def test_find_mutual_free_slots_merges_overlapping_busy() -> None:
    start = datetime(2026, 8, 28, 9, 0, tzinfo=_TZ)
    end = datetime(2026, 8, 28, 12, 0, tzinfo=_TZ)
    busy = [
        (datetime(2026, 8, 28, 9, 30, tzinfo=_TZ), datetime(2026, 8, 28, 10, 0, tzinfo=_TZ)),
        (datetime(2026, 8, 28, 9, 45, tzinfo=_TZ), datetime(2026, 8, 28, 10, 30, tzinfo=_TZ)),
    ]
    slots = find_mutual_free_slots(
        busy=busy,
        range_start=start,
        range_end=end,
        duration=timedelta(minutes=30),
        window=None,
        step=timedelta(minutes=30),
        limit=10,
    )
    starts = [s["start"] for s in slots]
    assert datetime(2026, 8, 28, 9, 0, tzinfo=_TZ).isoformat() in starts
    assert datetime(2026, 8, 28, 9, 30, tzinfo=_TZ).isoformat() not in starts
    assert datetime(2026, 8, 28, 10, 30, tzinfo=_TZ).isoformat() in starts


def test_format_suggest_human_lists_slots() -> None:
    lines = format_suggest_human(
        {
            "duration_minutes": 45,
            "end": "2026-08-28T17:00:00-04:00",
            "slots": [
                {
                    "start": "2026-08-28T09:00:00-04:00",
                    "end": "2026-08-28T09:45:00-04:00",
                }
            ],
            "start": "2026-08-28T09:00:00-04:00",
            "timezone": "America/New_York",
            "window": "09:00-17:00",
            "with": ["a@example.com", "b@example.com"],
        }
    )
    assert lines[0].startswith("Suggest 45m slots")
    assert "a@example.com" in lines[1]
    assert "09:00:00" in lines[2]


def test_calendar_suggest_uses_freebusy_and_tentative_flag(monkeypatch) -> None:
    schedule = SimpleNamespace(
        availability_view="000",
        schedule_id="a@example.com",
        schedule_items=[
            SimpleNamespace(
                start=SimpleNamespace(
                    date_time="2026-08-28T14:00:00",
                    time_zone="America/New_York",
                ),
                end=SimpleNamespace(
                    date_time="2026-08-28T15:00:00",
                    time_zone="America/New_York",
                ),
                status="tentative",
            )
        ],
        working_hours=None,
    )
    client = MagicMock()
    client.me.calendar.get_schedule.post = AsyncMock(return_value=SimpleNamespace(value=[schedule]))
    monkeypatch.setattr("blumkin.skills.calendar.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar.load_config",
        lambda: SimpleNamespace(client_id="x", default_tz="America/New_York"),
    )
    start = datetime(2026, 8, 28, 9, 0, tzinfo=_TZ)
    end = datetime(2026, 8, 28, 17, 0, tzinfo=_TZ)

    as_busy = asyncio.run(
        calendar_suggest(
            with_emails=["a@example.com"],
            start=start,
            end=end,
            duration=timedelta(hours=1),
            treat_tentative="busy",
            step=timedelta(hours=1),
            limit=20,
        )
    )
    busy_starts = {s["start"] for s in as_busy["slots"]}
    assert datetime(2026, 8, 28, 14, 0, tzinfo=_TZ).isoformat() not in busy_starts

    as_free = asyncio.run(
        calendar_suggest(
            with_emails=["a@example.com"],
            start=start,
            end=end,
            duration=timedelta(hours=1),
            treat_tentative="free",
            step=timedelta(hours=1),
            limit=20,
        )
    )
    free_starts = {s["start"] for s in as_free["slots"]}
    assert datetime(2026, 8, 28, 14, 0, tzinfo=_TZ).isoformat() in free_starts


def test_calendar_suggest_rejects_bad_window() -> None:
    start = datetime(2026, 8, 28, 9, 0, tzinfo=_TZ)
    end = datetime(2026, 8, 28, 17, 0, tzinfo=_TZ)
    with pytest.raises(ValueError, match="--window"):
        asyncio.run(
            calendar_suggest(
                with_emails=["a@example.com"],
                start=start,
                end=end,
                duration=timedelta(minutes=30),
                window="nope",
            )
        )
