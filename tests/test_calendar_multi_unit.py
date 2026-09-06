"""Hermetic coverage for ``calendar list`` + ``--calendar`` targeting (issue #176)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_USAGE
from blumkin.providers.google import calendar as google_calendar
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.skills.calendar import (
    CalendarAmbiguousError,
    CalendarNotFoundError,
    calendar_list,
    calendar_today,
    calendar_view,
    format_calendar_list_human,
)

_GOOGLE_CAL = "blumkin.providers.google.calendar"
_NY = "America/New_York"


# --------------------------------------------------------------------------- Graph


def _graph_calendar(id_: str, name: str, *, default: bool = False, can_edit: bool = True):
    return SimpleNamespace(
        id=id_,
        name=name,
        is_default_calendar=default,
        can_edit=can_edit,
        hex_color="#123456",
        color=None,
        owner=SimpleNamespace(address="me@example.com", name="Me"),
    )


def _graph_client(monkeypatch, *, calendars=None, view=None) -> MagicMock:
    client = MagicMock()
    client.me.calendars.get = AsyncMock(return_value=SimpleNamespace(value=calendars or []))
    client.me.calendar.calendar_view.get = AsyncMock(return_value=view)
    client.me.calendars.by_calendar_id.return_value.calendar_view.get = AsyncMock(return_value=view)
    monkeypatch.setattr("blumkin.skills.calendar.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar.load_config",
        lambda: SimpleNamespace(default_tz=_NY, client_id="x"),
    )
    return client


def test_graph_calendar_list_shape(monkeypatch) -> None:
    _graph_client(
        monkeypatch,
        calendars=[
            _graph_calendar("AAA", "Calendar", default=True),
            _graph_calendar("BBB", "Team", can_edit=False),
        ],
    )
    payload = asyncio.run(calendar_list())
    assert payload["count"] == 2
    assert payload["calendars"][0] == {
        "id": "AAA",
        "name": "Calendar",
        "is_default": True,
        "can_edit": True,
        "owner": "me@example.com",
        "color": "#123456",
    }
    assert payload["calendars"][1]["can_edit"] is False


def test_graph_view_default_calendar_uses_me_calendar(monkeypatch) -> None:
    client = _graph_client(monkeypatch, view=SimpleNamespace(value=[]))
    start = datetime(2026, 9, 1, tzinfo=ZoneInfo(_NY))
    asyncio.run(calendar_view(start=start, end=start.replace(day=2)))
    client.me.calendar.calendar_view.get.assert_awaited()
    client.me.calendars.by_calendar_id.assert_not_called()


def test_graph_view_named_calendar_resolves_and_targets_it(monkeypatch) -> None:
    client = _graph_client(
        monkeypatch,
        calendars=[
            _graph_calendar("AAA", "Calendar", default=True),
            _graph_calendar("TEAM", "Team"),
        ],
        view=SimpleNamespace(value=[]),
    )
    start = datetime(2026, 9, 1, tzinfo=ZoneInfo(_NY))
    asyncio.run(calendar_view(start=start, end=start.replace(day=2), calendar="team"))
    client.me.calendars.by_calendar_id.assert_called_once_with("TEAM")


def test_graph_view_unknown_calendar_raises(monkeypatch) -> None:
    _graph_client(monkeypatch, calendars=[_graph_calendar("AAA", "Calendar")], view=None)
    start = datetime(2026, 9, 1, tzinfo=ZoneInfo(_NY))
    with pytest.raises(CalendarNotFoundError, match="no calendar named 'nope'"):
        asyncio.run(calendar_view(start=start, end=start.replace(day=2), calendar="nope"))


def test_graph_view_ambiguous_calendar_name_raises(monkeypatch) -> None:
    _graph_client(
        monkeypatch,
        calendars=[_graph_calendar("A", "Projects"), _graph_calendar("B", "projects")],
        view=None,
    )
    start = datetime(2026, 9, 1, tzinfo=ZoneInfo(_NY))
    with pytest.raises(CalendarAmbiguousError, match="more than one calendar"):
        asyncio.run(calendar_view(start=start, end=start.replace(day=2), calendar="Projects"))


def test_graph_today_threads_calendar(monkeypatch) -> None:
    client = _graph_client(
        monkeypatch,
        calendars=[_graph_calendar("HOL", "Holidays")],
        view=SimpleNamespace(value=[]),
    )
    asyncio.run(calendar_today(calendar="HOL", tz_name="UTC"))  # exact id
    client.me.calendars.by_calendar_id.assert_called_once_with("HOL")


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


def _google_service(*, calendar_items=None, events_list=None) -> MagicMock:
    service = MagicMock()
    service.calendarList.return_value.list.return_value.execute.return_value = {
        "items": calendar_items or []
    }
    service.events.return_value.list.return_value.execute.return_value = events_list or {
        "items": []
    }
    return service


def _google_patched(service: MagicMock):
    return patch.multiple(
        _GOOGLE_CAL,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    )


def test_google_calendar_list_shape(tmp_path: Path) -> None:
    service = _google_service(
        calendar_items=[
            {
                "id": "primary",
                "summary": "Me",
                "primary": True,
                "accessRole": "owner",
                "backgroundColor": "#abc",
            },
            {"id": "team@group.calendar.google.com", "summary": "Team", "accessRole": "reader"},
        ]
    )
    with _google_patched(service):
        payload = asyncio.run(GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_list())
    assert [c["name"] for c in payload["calendars"]] == ["Me", "Team"]
    assert payload["calendars"][0]["is_default"] is True
    assert payload["calendars"][1]["can_edit"] is False


def test_google_view_named_calendar_targets_its_id(tmp_path: Path) -> None:
    service = _google_service(
        calendar_items=[
            {"id": "primary", "summary": "Me", "primary": True},
            {"id": "team@g.calendar.google.com", "summary": "Team", "accessRole": "writer"},
        ]
    )
    start = datetime(2026, 9, 1, tzinfo=ZoneInfo(_NY))
    with _google_patched(service):
        asyncio.run(
            google_calendar.calendar_view(
                start=start,
                end=start.replace(day=2),
                calendar="Team",
                config=_google_cfg(tmp_path),
            )
        )
    assert service.events.return_value.list.call_args.kwargs["calendarId"] == (
        "team@g.calendar.google.com"
    )


def test_google_view_default_is_primary(tmp_path: Path) -> None:
    service = _google_service()
    start = datetime(2026, 9, 1, tzinfo=ZoneInfo(_NY))
    with _google_patched(service):
        asyncio.run(
            google_calendar.calendar_view(
                start=start, end=start.replace(day=2), config=_google_cfg(tmp_path)
            )
        )
    assert service.events.return_value.list.call_args.kwargs["calendarId"] == "primary"
    service.calendarList.assert_not_called()  # no lookup for the default


def test_google_unknown_calendar_raises(tmp_path: Path) -> None:
    service = _google_service(calendar_items=[{"id": "primary", "summary": "Me", "primary": True}])
    start = datetime(2026, 9, 1, tzinfo=ZoneInfo(_NY))
    with _google_patched(service), pytest.raises(CalendarNotFoundError):
        asyncio.run(
            google_calendar.calendar_view(
                start=start,
                end=start.replace(day=2),
                calendar="nope",
                config=_google_cfg(tmp_path),
            )
        )


# --------------------------------------------------------------------------- formatter / CLI


def test_format_calendar_list_human_marks_default_and_readonly() -> None:
    lines = format_calendar_list_human(
        {
            "count": 2,
            "calendars": [
                {"id": "A", "name": "Me", "is_default": True, "can_edit": True},
                {"id": "B", "name": "Team", "is_default": False, "can_edit": False},
            ],
        }
    )
    assert "2 calendar(s):" in lines[0]
    assert any("Me  [default]" in line for line in lines)
    assert any("Team  [read-only]" in line for line in lines)


def test_cli_calendar_list_help() -> None:
    out = CliRunner().invoke(main, ["calendar", "list", "--help"]).output
    assert "calendars this account can see" in out


def test_cli_calendar_flag_in_help() -> None:
    for verb in ("today", "view", "get", "create", "update", "cancel"):
        out = CliRunner().invoke(main, ["calendar", verb, "--help"]).output
        assert "--calendar" in out, verb


def test_cli_ambiguous_calendar_is_usage_error(monkeypatch) -> None:
    async def _raise(**_kwargs):
        raise CalendarAmbiguousError("'x' matches more than one calendar: A, B")

    monkeypatch.setattr("blumkin.cli._workspace", lambda: SimpleNamespace(calendar_today=_raise))
    result = CliRunner().invoke(main, ["calendar", "today", "--calendar", "x", "--json"])
    assert result.exit_code == EXIT_USAGE


def test_cli_unknown_calendar_is_not_found(monkeypatch) -> None:
    async def _raise(**_kwargs):
        raise CalendarNotFoundError("no calendar named 'x'")

    monkeypatch.setattr("blumkin.cli._workspace", lambda: SimpleNamespace(calendar_today=_raise))
    result = CliRunner().invoke(main, ["calendar", "today", "--calendar", "x", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
