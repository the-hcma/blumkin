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
from msgraph.generated.models.day_of_week import DayOfWeek
from msgraph.generated.models.o_data_errors.o_data_error import ODataError
from msgraph.generated.models.patterned_recurrence import PatternedRecurrence
from msgraph.generated.models.recurrence_pattern import RecurrencePattern
from msgraph.generated.models.recurrence_pattern_type import RecurrencePatternType
from msgraph.generated.models.recurrence_range import RecurrenceRange
from msgraph.generated.models.recurrence_range_type import RecurrenceRangeType

from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_SUCCESS
from blumkin.providers.google import calendar as google_calendar
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.skills.calendar import CalendarEventNotFoundError, calendar_get

_GOOGLE_CAL = "blumkin.providers.google.calendar"


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
        response_status=SimpleNamespace(response="organizer"),
        body=SimpleNamespace(content="Agenda: status, blockers", content_type="text"),
        attendees=[
            SimpleNamespace(
                email_address=SimpleNamespace(name="Sam", address="sam@example.com"),
                status=SimpleNamespace(response="accepted"),
                type="required",
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
    assert ev["attendees"] == [
        {"email": "sam@example.com", "name": "Sam", "response": "accepted", "type": "required"}
    ]
    assert ev["recurrence"] == {
        "freq": "weekly",
        "interval": 1,
        "days": ["mo"],
        "until": "2026-12-31",
    }


def test_graph_calendar_get_html_body(monkeypatch) -> None:
    client = _graph_client(monkeypatch, _graph_event())
    asyncio.run(calendar_get(event_id="evt-1", body_type="html"))
    cfg = client.me.events.by_event_id.return_value.get.await_args.args[0]
    header = cfg.headers.get("Prefer")
    assert header == {'outlook.body-content-type="html"'}


def test_graph_calendar_get_missing_id_raises_not_found(monkeypatch) -> None:
    err = ODataError()
    err.response_status_code = 404
    client = MagicMock()
    client.me.events.by_event_id.return_value.get = AsyncMock(side_effect=err)
    monkeypatch.setattr("blumkin.skills.calendar.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar.load_config",
        lambda: SimpleNamespace(default_tz="UTC"),
    )
    with pytest.raises(CalendarEventNotFoundError, match="event not found: nope"):
        asyncio.run(calendar_get(event_id="nope"))


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
    }
    assert {a["email"]: a["response"] for a in ev["attendees"]} == {
        "sam@example.com": "tentativelyAccepted",
        "dana@example.com": "declined",
    }
    assert ev["recurrence"] == {
        "freq": "weekly",
        "interval": 2,
        "days": ["mo", "we"],
        "count": 8,
    }


def test_google_calendar_get_missing_id_raises_not_found(tmp_path: Path) -> None:
    service = MagicMock()
    service.events.return_value.get.return_value.execute.side_effect = HttpError(
        httplib2.Response({"status": 404}), b"not found"
    )
    with _google_patched(service), pytest.raises(CalendarEventNotFoundError, match="g-missing"):
        asyncio.run(
            google_calendar.calendar_get(event_id="g-missing", config=_google_cfg(tmp_path))
        )


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
