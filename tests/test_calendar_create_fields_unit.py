"""Hermetic coverage for ``calendar create`` body/location/all-day/optional (issue #175)."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from msgraph.generated.models.attendee_type import AttendeeType
from msgraph.generated.models.body_type import BodyType

from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.exit_codes import EXIT_USAGE
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.skills.calendar_writes import calendar_create, resolve_event_body

_GOOGLE_CAL = "blumkin.providers.google.calendar"
_NY = "America/New_York"


# --------------------------------------------------------------------------- resolver


def test_resolve_event_body_none_when_unset() -> None:
    assert resolve_event_body(None, None, "text") == (None, BodyType.Text)


def test_resolve_event_body_rejects_both() -> None:
    with pytest.raises(ValueError, match="only one of --body or --body-file"):
        resolve_event_body("x", "y", "text")


def test_resolve_event_body_reads_file(tmp_path: Path) -> None:
    f = tmp_path / "agenda.txt"
    f.write_text("Agenda: ship it\n")
    assert resolve_event_body(None, str(f), "html") == ("Agenda: ship it\n", BodyType.Html)


def test_resolve_event_body_missing_file() -> None:
    with pytest.raises(ValueError, match="cannot read --body-file"):
        resolve_event_body(None, "/no/such/file", "text")


# --------------------------------------------------------------------------- Graph


def _graph_client(monkeypatch) -> MagicMock:
    created = SimpleNamespace(
        id="evt",
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
    client.me.events.post = AsyncMock(return_value=created)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz=_NY, client_id="x"),
    )
    return client


def test_graph_create_body_location_optional(monkeypatch) -> None:
    client = _graph_client(monkeypatch)
    asyncio.run(
        calendar_create(
            subject="Design review",
            with_emails=["sam@example.com"],
            optional_emails=["dana@example.com"],
            start_raw="2026-09-22T09:00",
            duration="1h",
            location="Room 4",
            body="Agenda: API",
            body_type="html",
            teams=False,
            tz_name=_NY,
        )
    )
    posted = client.me.events.post.await_args.args[0]
    assert posted.body.content == "Agenda: API"
    assert posted.body.content_type == BodyType.Html
    assert posted.location.display_name == "Room 4"
    by_addr = {a.email_address.address: a.type for a in posted.attendees}
    assert by_addr == {
        "sam@example.com": AttendeeType.Required,
        "dana@example.com": AttendeeType.Optional,
    }


def test_graph_create_all_day(monkeypatch) -> None:
    client = _graph_client(monkeypatch)
    asyncio.run(
        calendar_create(
            subject="OOO",
            with_emails=[],
            start_raw="2026-12-24",
            all_day=True,
            duration="3d",
            teams=False,
            tz_name=_NY,
        )
    )
    posted = client.me.events.post.await_args.args[0]
    assert posted.is_all_day is True
    assert posted.start.date_time == "2026-12-24T00:00:00"
    assert posted.end.date_time == "2026-12-27T00:00:00"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"start_raw": "2026-12-24T09:00", "all_day": True}, "needs a date"),
        ({"start_raw": "2026-12-24", "all_day": True, "duration": "90m"}, "whole days"),
    ],
)
def test_graph_create_all_day_rejects(monkeypatch, kwargs: dict, match: str) -> None:
    _graph_client(monkeypatch)
    with pytest.raises(ValueError, match=match):
        asyncio.run(
            calendar_create(subject="x", with_emails=[], teams=False, tz_name=_NY, **kwargs)
        )


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


def test_google_create_fields(tmp_path: Path) -> None:
    service = MagicMock()
    service.events.return_value.insert.return_value.execute.return_value = {
        "id": "g",
        "summary": "x",
        "start": {"dateTime": "2026-09-22T09:00:00-04:00"},
        "end": {"dateTime": "2026-09-22T10:00:00-04:00"},
    }
    with patch.multiple(
        _GOOGLE_CAL,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    ):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_create(
                subject="Design review",
                with_emails=["sam@example.com"],
                optional_emails=["dana@example.com"],
                start_raw="2026-09-22T09:00",
                duration="1h",
                location="Room 4",
                body="Agenda: API",
            )
        )
    body = service.events.return_value.insert.call_args.kwargs["body"]
    assert body["description"] == "Agenda: API"
    assert body["location"] == "Room 4"
    assert body["attendees"] == [
        {"email": "sam@example.com"},
        {"email": "dana@example.com", "optional": True},
    ]


def test_google_create_all_day(tmp_path: Path) -> None:
    service = MagicMock()
    service.events.return_value.insert.return_value.execute.return_value = {
        "id": "g",
        "summary": "OOO",
        "start": {"date": "2026-12-24"},
        "end": {"date": "2026-12-27"},
    }
    with patch.multiple(
        _GOOGLE_CAL,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    ):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_create(
                subject="OOO",
                with_emails=[],
                start_raw="2026-12-24",
                all_day=True,
                duration="3d",
            )
        )
    body = service.events.return_value.insert.call_args.kwargs["body"]
    assert body["start"] == {"date": "2026-12-24"}
    assert body["end"] == {"date": "2026-12-27"}
    assert date.fromisoformat(body["end"]["date"]) - date.fromisoformat(body["start"]["date"]) == (
        date(2026, 12, 27) - date(2026, 12, 24)
    )


# --------------------------------------------------------------------------- CLI


def test_cli_create_help_lists_new_flags() -> None:
    out = CliRunner().invoke(main, ["calendar", "create", "--help"]).output
    for flag in ("--all-day", "--location", "--optional", "--body", "--body-file"):
        assert flag in out


def test_cli_all_day_with_time_start_is_usage_error(monkeypatch) -> None:
    async def _raise(**_kwargs):
        raise ValueError("--all-day needs a date --start (YYYY-MM-DD), not a time")

    monkeypatch.setattr("blumkin.cli._workspace", lambda: SimpleNamespace(calendar_create=_raise))
    result = CliRunner().invoke(
        main,
        [
            "calendar",
            "create",
            "--subject",
            "x",
            "--start",
            "2026-12-24T09:00",
            "--all-day",
            "--yes",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_USAGE
    assert json.loads(result.stderr)["error"] == "usage_error"
