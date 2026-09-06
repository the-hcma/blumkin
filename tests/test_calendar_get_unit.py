"""Hermetic coverage for ``calendar get`` (issue #173) - both providers + CLI."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httplib2
import pytest
from click.testing import CliRunner
from googleapiclient.errors import HttpError
from msgraph.generated.models.attendee_type import AttendeeType
from msgraph.generated.models.day_of_week import DayOfWeek
from msgraph.generated.models.o_data_errors.main_error import MainError
from msgraph.generated.models.o_data_errors.o_data_error import ODataError
from msgraph.generated.models.patterned_recurrence import PatternedRecurrence
from msgraph.generated.models.recurrence_pattern import RecurrencePattern
from msgraph.generated.models.recurrence_pattern_type import RecurrencePatternType
from msgraph.generated.models.recurrence_range import RecurrenceRange
from msgraph.generated.models.recurrence_range_type import RecurrenceRangeType
from msgraph.generated.models.response_type import ResponseType

from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_SUCCESS
from blumkin.providers.google import calendar as google_calendar
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.skills.calendar import (
    CalendarEventNotFoundError,
    calendar_get,
    format_calendar_get_human,
    format_recurrence,
)

_GOOGLE_CAL = "blumkin.providers.google.calendar"


def _odata_error(*, status: int, code: str | None = None) -> ODataError:
    err = ODataError()
    err.response_status_code = status
    if code is not None:
        err.error = MainError(code=code)
    return err


def _dtz(value: str) -> SimpleNamespace:
    return SimpleNamespace(date_time=value, time_zone=None)


def _graph_event() -> SimpleNamespace:
    return SimpleNamespace(
        id="evt-1",
        subject="Weekly 1:1",
        start=_dtz("2026-09-21T13:05:00Z"),
        end=_dtz("2026-09-21T13:50:00Z"),
        is_all_day=False,
        is_cancelled=False,
        is_organizer=True,
        location=SimpleNamespace(display_name="Room 4"),
        online_meeting=SimpleNamespace(join_url="https://teams.example/join"),
        organizer=SimpleNamespace(
            email_address=SimpleNamespace(name="Me", address="me@example.com")
        ),
        response_status=SimpleNamespace(response=ResponseType.Organizer),
        body=SimpleNamespace(content="Agenda: status, blockers", content_type="text"),
        attendees=[
            SimpleNamespace(
                email_address=SimpleNamespace(name="Sam", address="sam@example.com"),
                # Real Graph events carry kiota enum members here, not strings;
                # the mapper must emit the plain .value vocabulary.
                status=SimpleNamespace(response=ResponseType.Accepted),
                type=AttendeeType.Required,
            ),
        ],
        recurrence=PatternedRecurrence(
            pattern=RecurrencePattern(
                type=RecurrencePatternType.Weekly,
                interval=1,
                days_of_week=[DayOfWeek.Monday],
            ),
            range=RecurrenceRange(type=RecurrenceRangeType.EndDate, end_date=date(2026, 12, 31)),
        ),
        series_master_id="series-1",
        web_link="https://outlook.example/evt-1",
    )


def _graph_client(monkeypatch, event) -> MagicMock:
    client = MagicMock()
    client.me.events.by_event_id.return_value.get = AsyncMock(return_value=event)
    monkeypatch.setattr("blumkin.skills.calendar.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York"),
    )
    return client


def test_graph_calendar_get_full_shape(monkeypatch) -> None:
    _graph_client(monkeypatch, _graph_event())
    payload = asyncio.run(calendar_get(event_id=" evt-1 ", tz_name="America/New_York"))
    ev = payload["event"]
    assert ev["subject"] == "Weekly 1:1"
    assert ev["body"] == "Agenda: status, blockers"
    assert ev["body_type"] == "text"
    assert ev["location"] == "Room 4"
    assert ev["online_join_url"] == "https://teams.example/join"
    assert ev["series_master_id"] == "series-1"
    assert ev["web_link"] == "https://outlook.example/evt-1"
    assert ev["is_cancelled"] is False
    assert ev["response"] == "organizer"  # kiota ResponseType -> wire vocab
    assert ev["attendees"] == [
        {"email": "sam@example.com", "name": "Sam", "response": "accepted", "type": "required"}
    ]
    assert ev["recurrence"] == {
        "freq": "weekly",
        "interval": 1,
        "days": ["mo"],
        "until": "2026-12-31",
    }


def test_graph_calendar_get_attendee_uses_wire_vocab_in_human_output(monkeypatch) -> None:
    _graph_client(monkeypatch, _graph_event())
    payload = asyncio.run(calendar_get(event_id="evt-1", tz_name="America/New_York"))
    lines = format_calendar_get_human(payload)
    assert any("Sam — accepted (required)" in line for line in lines)


def test_graph_calendar_get_cancelled_event(monkeypatch) -> None:
    event = _graph_event()
    event.is_cancelled = True
    _graph_client(monkeypatch, event)
    payload = asyncio.run(calendar_get(event_id="evt-1", tz_name="America/New_York"))
    assert payload["event"]["is_cancelled"] is True
    assert "  [cancelled]" in format_calendar_get_human(payload)


def test_graph_calendar_get_html_body(monkeypatch) -> None:
    client = _graph_client(monkeypatch, _graph_event())
    asyncio.run(calendar_get(event_id="evt-1", body_type="html"))
    cfg = client.me.events.by_event_id.return_value.get.await_args.args[0]
    header = cfg.headers.get("Prefer")
    assert header == {'outlook.body-content-type="html"'}


@pytest.mark.parametrize(
    "err",
    [
        _odata_error(status=404),
        # A malformed id: Graph answers 400 + an id-shaped error code, the arm
        # is_id_lookup_failure exists for.
        _odata_error(status=400, code="ErrorInvalidIdMalformed"),
        _odata_error(status=400, code="ErrorItemNotFound"),
    ],
)
def test_graph_calendar_get_bad_id_raises_not_found(monkeypatch, err) -> None:
    client = MagicMock()
    client.me.events.by_event_id.return_value.get = AsyncMock(side_effect=err)
    monkeypatch.setattr("blumkin.skills.calendar.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar.load_config",
        lambda: SimpleNamespace(default_tz="UTC"),
    )
    with pytest.raises(CalendarEventNotFoundError, match="event not found: nope"):
        asyncio.run(calendar_get(event_id="nope"))


def test_graph_calendar_get_query_400_still_raises(monkeypatch) -> None:
    """A query-level 400 (no id-shaped code) must not be mistaken for not_found."""
    client = MagicMock()
    client.me.events.by_event_id.return_value.get = AsyncMock(
        side_effect=_odata_error(status=400, code="ErrorInvalidUrlQuery")
    )
    monkeypatch.setattr("blumkin.skills.calendar.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar.load_config", lambda: SimpleNamespace(default_tz="UTC")
    )
    with pytest.raises(ODataError):
        asyncio.run(calendar_get(event_id="evt-1"))


def _google_cfg(config_dir: Path) -> BlumkinConfig:
    oauth = config_dir / "desktop-client.json"
    oauth.write_text('{"installed": {"client_id": "id.apps.googleusercontent.com"}}')
    return BlumkinConfig(
        client_id="id.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="America/New_York",
        email="",
        files_scopes=False,
        google_oauth_client_file=oauth,
        graph_timeout_seconds=60.0,
        legacy_flat=True,
        mail_signature=MailSignatureConfig(),
        profile="default",
        provider=ProviderKind.GOOGLE,
        tags=(),
        tenant_id="",
        wo1162425_scopes=False,
    )


_GOOGLE_EVENT = {
    "id": "g-evt",
    "summary": "Weekly 1:1",
    "description": "Agenda here",
    "status": "confirmed",
    "start": {"dateTime": "2026-09-21T13:05:00-04:00"},
    "end": {"dateTime": "2026-09-21T13:50:00-04:00"},
    "location": "Room 4",
    "htmlLink": "https://calendar.google.com/g-evt",
    "recurringEventId": "series-g",
    "hangoutLink": "https://meet.google.com/abc",
    "organizer": {"email": "me@example.com", "self": True},
    "attendees": [
        {"email": "sam@example.com", "displayName": "Sam", "responseStatus": "tentative"},
        {"email": "dana@example.com", "responseStatus": "declined", "optional": True},
        {"email": "kim@example.com", "responseStatus": "needsAction"},
        {"email": "lee@example.com", "responseStatus": "accepted"},
    ],
    "recurrence": ["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=8"],
}


def _google_patched(service: MagicMock):
    return patch.multiple(
        _GOOGLE_CAL,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    )


def test_google_calendar_get_full_shape(tmp_path: Path) -> None:
    service = MagicMock()
    service.events.return_value.get.return_value.execute.return_value = _GOOGLE_EVENT
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_get(event_id="g-evt")
        )
    ev = payload["event"]
    assert ev["body"] == "Agenda here"
    assert ev["is_cancelled"] is False
    assert ev["series_master_id"] == "series-g"
    assert ev["web_link"] == "https://calendar.google.com/g-evt"
    assert ev["online_join_url"] == "https://meet.google.com/abc"
    assert {a["email"]: a["type"] for a in ev["attendees"]} == {
        "sam@example.com": "required",
        "dana@example.com": "optional",
        "kim@example.com": "required",
        "lee@example.com": "required",
    }
    assert {a["email"]: a["response"] for a in ev["attendees"]} == {
        "sam@example.com": "tentativelyAccepted",
        "dana@example.com": "declined",
        "kim@example.com": "notResponded",
        "lee@example.com": "accepted",
    }
    assert ev["recurrence"] == {
        "freq": "weekly",
        "interval": 2,
        "days": ["mo", "we"],
        "count": 8,
    }


def test_google_calendar_get_cancelled_event(tmp_path: Path) -> None:
    service = MagicMock()
    service.events.return_value.get.return_value.execute.return_value = {
        **_GOOGLE_EVENT,
        "status": "cancelled",
    }
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_get(event_id="g-evt")
        )
    assert payload["event"]["is_cancelled"] is True
    assert "  [cancelled]" in format_calendar_get_human(payload)


@pytest.mark.parametrize(
    "rrule",
    [
        "RRULE:FREQ=MONTHLY;BYDAY=2WE",  # second Wednesday
        "RRULE:FREQ=MONTHLY;BYSETPOS=-1;BYDAY=FR",  # last Friday
        "RRULE:FREQ=DAILY;INTERVAL=2;BYDAY=MO,WE,FR",  # every other weekday subset
        "RRULE:FREQ=DAILY;BYMONTHDAY=15",  # BYMONTHDAY only means anything for monthly
        "RRULE:FREQ=WEEKLY;BYMONTHDAY=1",  # selector the weekly branch cannot hold
        "RRULE:FREQ=MONTHLY;BYMONTHDAY=1,15",  # a list, not a single day-of-month
        "RRULE:FREQ=YEARLY",
    ],
)
def test_google_calendar_get_unsupported_recurrence_is_other(tmp_path: Path, rrule: str) -> None:
    event = {**_GOOGLE_EVENT, "recurrence": [rrule]}
    service = MagicMock()
    service.events.return_value.get.return_value.execute.return_value = event
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_get(event_id="g-evt")
        )
    rec = payload["event"]["recurrence"]
    assert rec["freq"] == "other"
    assert rec["raw"] == rrule


def test_google_calendar_get_monthly_bymonthday(tmp_path: Path) -> None:
    event = {**_GOOGLE_EVENT, "recurrence": ["RRULE:FREQ=MONTHLY;BYMONTHDAY=15;COUNT=6"]}
    service = MagicMock()
    service.events.return_value.get.return_value.execute.return_value = event
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_get(event_id="g-evt")
        )
    assert payload["event"]["recurrence"] == {
        "freq": "monthly",
        "interval": 1,
        "day_of_month": 15,
        "count": 6,
    }


def test_graph_calendar_get_relative_monthly_is_other(monkeypatch) -> None:
    event = _graph_event()
    event.recurrence = PatternedRecurrence(
        pattern=RecurrencePattern(
            type=RecurrencePatternType.RelativeMonthly,
            interval=1,
            days_of_week=[DayOfWeek.Wednesday],
        ),
        range=RecurrenceRange(type=RecurrenceRangeType.NoEnd),
    )
    _graph_client(monkeypatch, event)
    payload = asyncio.run(calendar_get(event_id="evt-1"))
    assert payload["event"]["recurrence"]["freq"] == "other"


def test_graph_calendar_get_numbered_range_reads_back_count(monkeypatch) -> None:
    event = _graph_event()
    event.recurrence = PatternedRecurrence(
        pattern=RecurrencePattern(
            type=RecurrencePatternType.Weekly, interval=1, days_of_week=[DayOfWeek.Monday]
        ),
        range=RecurrenceRange(type=RecurrenceRangeType.Numbered, number_of_occurrences=8),
    )
    _graph_client(monkeypatch, event)
    payload = asyncio.run(calendar_get(event_id="evt-1"))
    assert payload["event"]["recurrence"] == {
        "freq": "weekly",
        "interval": 1,
        "days": ["mo"],
        "count": 8,
    }


def test_graph_calendar_get_absolute_monthly_reads_back_day_of_month(monkeypatch) -> None:
    event = _graph_event()
    event.recurrence = PatternedRecurrence(
        pattern=RecurrencePattern(
            type=RecurrencePatternType.AbsoluteMonthly, interval=1, day_of_month=15
        ),
        range=RecurrenceRange(type=RecurrenceRangeType.NoEnd),
    )
    _graph_client(monkeypatch, event)
    payload = asyncio.run(calendar_get(event_id="evt-1"))
    assert payload["event"]["recurrence"] == {
        "freq": "monthly",
        "interval": 1,
        "day_of_month": 15,
        "ends": "never",
    }


def test_graph_calendar_get_no_end_range_reads_back_never(monkeypatch) -> None:
    event = _graph_event()
    event.recurrence = PatternedRecurrence(
        pattern=RecurrencePattern(type=RecurrencePatternType.Daily, interval=3, days_of_week=[]),
        range=RecurrenceRange(type=RecurrenceRangeType.NoEnd),
    )
    _graph_client(monkeypatch, event)
    payload = asyncio.run(calendar_get(event_id="evt-1"))
    assert payload["event"]["recurrence"] == {"freq": "daily", "interval": 3, "ends": "never"}


def test_graph_calendar_get_non_recurring_event(monkeypatch) -> None:
    event = _graph_event()
    event.recurrence = None
    _graph_client(monkeypatch, event)
    payload = asyncio.run(calendar_get(event_id="evt-1", tz_name="America/New_York"))
    assert payload["event"]["recurrence"] is None
    assert not any("repeats:" in line for line in format_calendar_get_human(payload))


def test_google_calendar_get_non_recurring_event(tmp_path: Path) -> None:
    event = {k: v for k, v in _GOOGLE_EVENT.items() if k != "recurrence"}
    service = MagicMock()
    service.events.return_value.get.return_value.execute.return_value = event
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_get(event_id="g-evt")
        )
    assert payload["event"]["recurrence"] is None
    assert not any("repeats:" in line for line in format_calendar_get_human(payload))


def test_google_calendar_get_weekly_without_byday_recovers_days_from_start(tmp_path: Path) -> None:
    # 2026-09-21 is a Monday; recurrence_rrule omits BYDAY when --days matches the
    # DTSTART weekday, so the readback recovers days to match the create echo.
    event = {**_GOOGLE_EVENT, "recurrence": ["RRULE:FREQ=WEEKLY;INTERVAL=1;COUNT=4"]}
    service = MagicMock()
    service.events.return_value.get.return_value.execute.return_value = event
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_get(event_id="g-evt")
        )
    assert payload["event"]["recurrence"] == {
        "freq": "weekly",
        "interval": 1,
        "days": ["mo"],
        "count": 4,
    }


def test_google_calendar_get_open_ended_weekly_reads_back_never(tmp_path: Path) -> None:
    event = {**_GOOGLE_EVENT, "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO"]}
    service = MagicMock()
    service.events.return_value.get.return_value.execute.return_value = event
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_get(event_id="g-evt")
        )
    assert payload["event"]["recurrence"] == {
        "freq": "weekly",
        "interval": 1,
        "days": ["mo"],
        "ends": "never",
    }


def test_google_calendar_get_monthly_without_bymonthday_recovers_day_from_start(
    tmp_path: Path,
) -> None:
    # recurrence_rrule never emits BYMONTHDAY for a monthly rule (RFC 5545 uses
    # the DTSTART day), so the readback must recover day_of_month from the start
    # to match recurrence_payload and the Graph AbsoluteMonthly mapping.
    event = {
        **_GOOGLE_EVENT,
        "start": {"dateTime": "2026-09-15T09:00:00-04:00"},
        "recurrence": ["RRULE:FREQ=MONTHLY;INTERVAL=1;COUNT=4"],
    }
    service = MagicMock()
    service.events.return_value.get.return_value.execute.return_value = event
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_get(event_id="g-evt")
        )
    assert payload["event"]["recurrence"] == {
        "freq": "monthly",
        "interval": 1,
        "day_of_month": 15,
        "count": 4,
    }


def test_google_calendar_get_until_readback_uses_local_date(tmp_path: Path) -> None:
    # recurrence_rrule stores --until 2026-12-31 (America/New_York) as the UTC
    # end-of-day: 2027-01-01T04:59:59Z. The readback must report 2026-12-31.
    event = {
        **_GOOGLE_EVENT,
        "recurrence": ["RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=MO;UNTIL=20270101T045959Z"],
    }
    service = MagicMock()
    service.events.return_value.get.return_value.execute.return_value = event
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_get(event_id="g-evt")
        )
    assert payload["event"]["recurrence"]["until"] == "2026-12-31"


@pytest.mark.parametrize("status", [404, 410])
def test_google_calendar_get_missing_id_raises_not_found(tmp_path: Path, status: int) -> None:
    service = MagicMock()
    service.events.return_value.get.return_value.execute.side_effect = HttpError(
        httplib2.Response({"status": status}), b"not found"
    )
    with _google_patched(service), pytest.raises(CalendarEventNotFoundError, match="g-missing"):
        asyncio.run(
            google_calendar.calendar_get(event_id="g-missing", config=_google_cfg(tmp_path))
        )


def test_format_recurrence_other_shows_the_raw_rule_not_no_end() -> None:
    out = format_recurrence({"freq": "other", "raw": "RRULE:FREQ=MONTHLY;BYDAY=2WE;COUNT=12"})
    assert "no end" not in out
    assert "BYDAY=2WE" in out


def test_format_calendar_get_human_sanitizes_attacker_controlled_names() -> None:
    lines = format_calendar_get_human(
        {
            "event": {
                "id": "e",
                "subject": "sub",
                "start": "s",
                "end": "e2",
                "timezone": "UTC",
                "organizer": {"email": "x@e.com", "name": "Ev\x1b[31mil"},
                "attendees": [
                    {
                        "email": "a@e.com",
                        "name": "A\x07t",
                        "response": "acc\x1bepted",
                        "type": "required",
                    }
                ],
            }
        }
    )
    joined = "\n".join(lines)
    assert "\x1b" not in joined
    assert "\x07" not in joined


def test_cli_calendar_get_help_and_not_found(monkeypatch) -> None:
    help_out = CliRunner().invoke(main, ["calendar", "get", "--help"])
    assert help_out.exit_code == EXIT_SUCCESS
    assert "--event-id" in help_out.output

    async def _raise(**_kwargs):
        raise CalendarEventNotFoundError("event not found: x")

    monkeypatch.setattr("blumkin.cli._workspace", lambda: SimpleNamespace(calendar_get=_raise))
    result = CliRunner().invoke(main, ["calendar", "get", "--event-id", "x", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert json.loads(result.stderr)["error"] == "not_found"
