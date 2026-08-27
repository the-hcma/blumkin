"""Rendering Graph DateTimeTimeZone values back to local ISO strings (issue #46)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from blumkin.skills.calendar import _graph_dt_to_iso, _resolve_tz
from blumkin.skills.calendar_writes import calendar_create

_NY = ZoneInfo("America/New_York")


def test_calendar_create_reports_the_submitted_wall_clock(monkeypatch) -> None:
    """POST /me/events echoes a naive dateTime plus its zone; do not read it as UTC."""
    created = SimpleNamespace(
        end=_dtz("2026-08-28T15:15:00.0000000", "America/New_York"),
        id="evt-new",
        is_all_day=False,
        is_organizer=True,
        location=None,
        online_meeting=None,
        organizer=None,
        response_status=None,
        start=_dtz("2026-08-28T14:30:00.0000000", "America/New_York"),
        subject="Test",
    )
    client = MagicMock()
    client.me.events.post = AsyncMock(return_value=created)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(client_id="x", default_tz="America/New_York"),
    )
    payload = asyncio.run(
        calendar_create(
            subject="Test",
            with_emails=["someone@example.com"],
            start_raw="2026-08-28T14:30:00",
            duration="45m",
            tz_name="America/New_York",
        )
    )
    assert payload["event"]["start"] == "2026-08-28T14:30:00-04:00"
    assert payload["event"]["end"] == "2026-08-28T15:15:00-04:00"


def test_graph_dt_to_iso_honors_iana_time_zone() -> None:
    value = _dtz("2026-08-28T14:30:00", "America/New_York")
    assert _graph_dt_to_iso(value, _NY) == "2026-08-28T14:30:00-04:00"


def test_graph_dt_to_iso_honors_windows_time_zone() -> None:
    value = _dtz("2026-08-28T14:30:00", "Eastern Standard Time")
    assert _graph_dt_to_iso(value, _NY) == "2026-08-28T14:30:00-04:00"


def test_graph_dt_to_iso_keeps_utc_when_time_zone_absent() -> None:
    # calendarView relies on the Prefer header and omits timeZone — must stay UTC.
    value = _dtz("2026-08-28T14:30:00", None)
    assert _graph_dt_to_iso(value, _NY) == "2026-08-28T10:30:00-04:00"


def test_graph_dt_to_iso_keeps_utc_for_unknown_time_zone() -> None:
    value = _dtz("2026-08-28T14:30:00", "Not/ARealZone")
    assert _graph_dt_to_iso(value, _NY) == "2026-08-28T10:30:00-04:00"


def test_graph_dt_to_iso_respects_explicit_offsets_and_z() -> None:
    # An explicit offset or Z wins over timeZone rather than being reinterpreted.
    assert _graph_dt_to_iso(_dtz("2026-08-28T18:30:00Z", "America/New_York"), _NY) == (
        "2026-08-28T14:30:00-04:00"
    )
    assert _graph_dt_to_iso(_dtz("2026-08-28T18:30:00+00:00", "Eastern Standard Time"), _NY) == (
        "2026-08-28T14:30:00-04:00"
    )


def test_graph_dt_to_iso_returns_none_without_a_value() -> None:
    assert _graph_dt_to_iso(None, _NY) is None
    assert _graph_dt_to_iso(_dtz("", "America/New_York"), _NY) is None


def test_resolve_tz_accepts_iana_windows_and_utc_spellings() -> None:
    assert _resolve_tz("America/New_York") == ZoneInfo("America/New_York")
    assert _resolve_tz("Eastern Standard Time") == ZoneInfo("America/New_York")
    assert _resolve_tz("GMT Standard Time") == ZoneInfo("Europe/London")
    assert _resolve_tz("UTC") == ZoneInfo("UTC")
    assert _resolve_tz("tzone://Microsoft/Utc") == ZoneInfo("UTC")


def test_resolve_tz_is_case_insensitive_for_windows_names() -> None:
    assert _resolve_tz("eastern standard time") == ZoneInfo("America/New_York")
    assert _resolve_tz("  Pacific Standard Time  ") == ZoneInfo("America/Los_Angeles")


def test_resolve_tz_returns_none_for_missing_or_unknown() -> None:
    assert _resolve_tz(None) is None
    assert _resolve_tz("") is None
    assert _resolve_tz("   ") is None
    assert _resolve_tz("Not/ARealZone") is None
    # Absolute paths would otherwise raise out of ZoneInfo rather than falling back.
    assert _resolve_tz("/etc/passwd") is None


def _dtz(date_time: str, time_zone: str | None) -> SimpleNamespace:
    return SimpleNamespace(date_time=date_time, time_zone=time_zone)
