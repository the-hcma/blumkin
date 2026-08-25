"""Hermetic calendar.today coverage (mocked Graph client)."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from blumkin.skills.calendar import calendar_today, format_today_human


def test_calendar_today_mocked_events(monkeypatch) -> None:
    event = SimpleNamespace(
        id="evt-1",
        subject="Standup",
        is_all_day=False,
        is_organizer=True,
        start=SimpleNamespace(date_time="2026-08-25T14:00:00Z"),
        end=SimpleNamespace(date_time="2026-08-25T14:30:00Z"),
        location=SimpleNamespace(display_name="Teams"),
        organizer=SimpleNamespace(
            email_address=SimpleNamespace(name="Me", address="me@example.com")
        ),
        response_status=SimpleNamespace(response="accepted"),
        online_meeting=SimpleNamespace(join_url="https://example.com/join"),
    )
    view = SimpleNamespace(value=[event])
    client = MagicMock()
    client.me.calendar.calendar_view.get = AsyncMock(return_value=view)
    monkeypatch.setattr(
        "blumkin.skills.calendar.create_graph_client",
        lambda _cfg: client,
    )
    monkeypatch.setattr(
        "blumkin.skills.calendar.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )

    payload = asyncio.run(calendar_today(day=date(2026, 8, 25), tz_name="UTC"))
    assert payload["date"] == "2026-08-25"
    assert payload["timezone"] == "UTC"
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["subject"] == "Standup"
    assert item["location"] == "Teams"
    assert item["start"] is not None
    lines = format_today_human(payload)
    assert any("Standup" in line for line in lines)
