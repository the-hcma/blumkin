"""Hermetic coverage for ``calendar update`` full editing (issue #172)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httplib2
import pytest
from click.testing import CliRunner
from googleapiclient.errors import HttpError
from msgraph.generated.models.attendee_type import AttendeeType

from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_USAGE
from blumkin.providers.google import calendar as google_calendar
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.skills.calendar import CalendarEventNotFoundError
from blumkin.skills.calendar_writes import calendar_update

_GOOGLE_CAL = "blumkin.providers.google.calendar"
_NY = "America/New_York"


def _dtz(value: str) -> SimpleNamespace:
    return SimpleNamespace(date_time=value, time_zone="UTC")


def _graph_client(monkeypatch, *, existing=None) -> MagicMock:
    updated = SimpleNamespace(
        id="evt-1",
        subject="x",
        start=None,
        end=None,
        is_all_day=False,
        is_organizer=True,
        location=None,
        organizer=None,
        response_status=None,
        online_meeting=None,
    )
    client = MagicMock()
    client.me.events.by_event_id.return_value.patch = AsyncMock(return_value=updated)
    client.me.events.by_event_id.return_value.get = AsyncMock(return_value=existing)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz=_NY, client_id="x"),
    )
    return client


def _posted(client: MagicMock):
    pa = client.me.events.by_event_id.return_value.patch.await_args
    assert pa is not None
    return pa.args[0]


def test_graph_update_subject_location_body_and_with(monkeypatch) -> None:
    client = _graph_client(monkeypatch)
    asyncio.run(
        calendar_update(
            event_id="evt-1",
            subject="New title",
            location="Room 7",
            body="Fresh agenda",
            body_type="html",
            with_emails=["sam@example.com"],
            tz_name=_NY,
        )
    )
    patched = _posted(client)
    assert patched.subject == "New title"
    assert patched.location.display_name == "Room 7"
    assert patched.body.content == "Fresh agenda"
    assert [a.email_address.address for a in patched.attendees] == ["sam@example.com"]
    assert patched.attendees[0].type == AttendeeType.Required
    client.me.events.by_event_id.return_value.get.assert_not_awaited()  # no time change


def test_graph_update_start_keeps_existing_length(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="evt-1",
        is_all_day=False,
        start=_dtz("2026-09-21T13:00:00"),
        end=_dtz("2026-09-21T13:45:00"),
    )
    client = _graph_client(monkeypatch, existing=existing)
    asyncio.run(calendar_update(event_id="evt-1", start_raw="2026-09-23T15:00", tz_name=_NY))
    patched = _posted(client)
    assert patched.start.date_time == "2026-09-23T15:00:00"
    assert patched.end.date_time == "2026-09-23T15:45:00"  # 45-minute span preserved


def test_graph_update_convert_to_all_day(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="evt-1",
        is_all_day=False,
        start=_dtz("2026-09-21T13:00:00"),
        end=_dtz("2026-09-21T14:00:00"),
    )
    client = _graph_client(monkeypatch, existing=existing)
    asyncio.run(
        calendar_update(
            event_id="evt-1", all_day=True, start_raw="2026-12-24", duration="2d", tz_name=_NY
        )
    )
    patched = _posted(client)
    assert patched.is_all_day is True
    assert patched.start.date_time == "2026-12-24T00:00:00"
    assert patched.end.date_time == "2026-12-26T00:00:00"


def test_graph_update_end_and_duration_mutually_exclusive(monkeypatch) -> None:
    _graph_client(monkeypatch)
    with pytest.raises(ValueError, match="only one of --end or --duration"):
        asyncio.run(
            calendar_update(
                event_id="evt-1", end_raw="2026-09-21T14:00", duration="1h", tz_name=_NY
            )
        )


def test_graph_update_nothing_to_update(monkeypatch) -> None:
    _graph_client(monkeypatch)
    with pytest.raises(ValueError, match="nothing to update"):
        asyncio.run(calendar_update(event_id="evt-1", tz_name=_NY))


def test_graph_update_missing_event_on_time_change(monkeypatch) -> None:
    _graph_client(monkeypatch, existing=None)
    with pytest.raises(CalendarEventNotFoundError, match="event not found: evt-1"):
        asyncio.run(calendar_update(event_id="evt-1", start_raw="2026-09-23T15:00", tz_name=_NY))


# --------------------------------------------------------------------------- Google


def _google_cfg(config_dir: Path) -> BlumkinConfig:
    oauth = config_dir / "desktop-client.json"
    oauth.write_text('{"installed": {"client_id": "id.apps.googleusercontent.com"}}')
    return BlumkinConfig(
        client_id="id.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz=_NY,
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


def _google_service(existing: dict | None = None) -> MagicMock:
    service = MagicMock()
    events = service.events.return_value
    events.get.return_value.execute.return_value = existing or {"id": "evt-1"}
    events.patch.return_value.execute.return_value = {"id": "evt-1", "summary": "x"}
    return service


def _google_patched(service: MagicMock):
    return patch.multiple(
        _GOOGLE_CAL,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    )


def test_google_update_fields(tmp_path: Path) -> None:
    service = _google_service()
    with _google_patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_update(
                event_id="evt-1",
                subject="New",
                location="Room 7",
                body="Agenda",
                with_emails=["sam@example.com"],
            )
        )
    body = service.events.return_value.patch.call_args.kwargs["body"]
    assert body == {
        "summary": "New",
        "location": "Room 7",
        "description": "Agenda",
        "attendees": [{"email": "sam@example.com"}],
    }


def test_google_update_start_keeps_length(tmp_path: Path) -> None:
    existing = {
        "id": "evt-1",
        "start": {"dateTime": "2026-09-21T13:00:00-04:00"},
        "end": {"dateTime": "2026-09-21T13:45:00-04:00"},
    }
    service = _google_service(existing)
    with _google_patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_update(
                event_id="evt-1", start_raw="2026-09-23T15:00"
            )
        )
    body = service.events.return_value.patch.call_args.kwargs["body"]
    assert body["start"]["dateTime"].startswith("2026-09-23T15:00:00")
    assert body["end"]["dateTime"].startswith("2026-09-23T15:45:00")


def test_google_update_missing_event(tmp_path: Path) -> None:
    service = _google_service()
    service.events.return_value.patch.return_value.execute.side_effect = HttpError(
        httplib2.Response({"status": 404}), b"gone"
    )
    with _google_patched(service), pytest.raises(CalendarEventNotFoundError):
        asyncio.run(
            google_calendar.calendar_update(
                event_id="evt-1", subject="x", config=_google_cfg(tmp_path)
            )
        )


# --------------------------------------------------------------------------- CLI


def test_cli_update_help_and_requires_yes() -> None:
    out = CliRunner().invoke(main, ["calendar", "update", "--help"]).output
    for flag in ("--subject", "--start", "--location", "--body", "--all-day", "--no-teams"):
        assert flag in out
    no_yes = CliRunner().invoke(
        main, ["calendar", "update", "--event-id", "e", "--subject", "x", "--json"]
    )
    assert no_yes.exit_code == EXIT_USAGE


def test_cli_update_not_found(monkeypatch) -> None:
    async def _raise(**_kwargs):
        raise CalendarEventNotFoundError("event not found: e")

    monkeypatch.setattr("blumkin.cli._workspace", lambda: SimpleNamespace(calendar_update=_raise))
    result = CliRunner().invoke(
        main, ["calendar", "update", "--event-id", "e", "--subject", "x", "--yes", "--json"]
    )
    assert result.exit_code == EXIT_NOT_FOUND
    assert json.loads(result.stderr)["error"] == "not_found"
