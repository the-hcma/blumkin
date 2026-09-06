"""Hermetic coverage for ``calendar decline`` / ``calendar tentative`` (issue #174)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from msgraph.generated.models.o_data_errors.main_error import MainError
from msgraph.generated.models.o_data_errors.o_data_error import ODataError

from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_USAGE
from blumkin.providers.google import calendar as google_calendar
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.skills.calendar import CalendarEventNotFoundError
from blumkin.skills.calendar_writes import (
    calendar_accept,
    calendar_decline,
    calendar_tentative,
    format_rsvp_human,
)

_GOOGLE_CAL = "blumkin.providers.google.calendar"
_NY = "America/New_York"


# --------------------------------------------------------------------------- Graph


def _graph_client(monkeypatch, *, today_items=None) -> MagicMock:
    client = MagicMock()
    for action in ("accept", "decline", "tentatively_accept"):
        getattr(client.me.events.by_event_id.return_value, action).post = AsyncMock(
            return_value=None
        )
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz=_NY, client_id="x"),
    )
    if today_items is not None:

        async def _fake_today(**_kwargs):
            return {"items": today_items}

        monkeypatch.setattr("blumkin.skills.calendar_writes.calendar_today", _fake_today)
    return client


def _body(client: MagicMock, action: str):
    await_args = getattr(client.me.events.by_event_id.return_value, action).post.await_args
    assert await_args is not None
    return await_args.args[0]


def test_graph_decline_sends_comment_and_response(monkeypatch) -> None:
    client = _graph_client(monkeypatch)
    payload = asyncio.run(
        calendar_decline(event_id="evt-1", comment="clashes with the board call", tz_name=_NY)
    )
    assert payload == {"declined": ["evt-1"], "count": 1, "skipped": []}
    body = _body(client, "decline")
    assert body.comment == "clashes with the board call"
    assert body.send_response is True
    assert body.proposed_new_time is None


def test_graph_tentative_uses_the_tentatively_accept_endpoint(monkeypatch) -> None:
    client = _graph_client(monkeypatch)
    payload = asyncio.run(calendar_tentative(event_id="evt-1", tz_name=_NY))
    assert payload == {"tentative": ["evt-1"], "count": 1, "skipped": []}
    assert _body(client, "tentatively_accept").send_response is True


def test_graph_accept_now_carries_a_comment(monkeypatch) -> None:
    client = _graph_client(monkeypatch)
    asyncio.run(calendar_accept(event_id="evt-1", comment="on it", tz_name=_NY))
    assert _body(client, "accept").comment == "on it"


def test_graph_decline_propose_time_builds_a_time_slot(monkeypatch) -> None:
    client = _graph_client(monkeypatch)
    asyncio.run(
        calendar_decline(
            event_id="evt-1",
            propose_start="2026-09-02T15:00",
            propose_duration="45m",
            tz_name=_NY,
        )
    )
    slot = _body(client, "decline").proposed_new_time
    assert slot is not None
    assert slot.start.date_time.startswith("2026-09-02T15:00")
    assert slot.end.date_time.startswith("2026-09-02T15:45")


def test_graph_decline_propose_duration_needs_propose_time(monkeypatch) -> None:
    _graph_client(monkeypatch)
    with pytest.raises(ValueError, match="--propose-duration needs --propose-time"):
        asyncio.run(calendar_decline(event_id="evt-1", propose_duration="30m", tz_name=_NY))


def test_graph_decline_propose_time_rejects_a_bare_date(monkeypatch) -> None:
    _graph_client(monkeypatch)
    with pytest.raises(ValueError, match="--propose-time needs a time"):
        asyncio.run(calendar_decline(event_id="evt-1", propose_start="2026-09-02", tz_name=_NY))


def test_graph_decline_single_event_needs_no_timezone(monkeypatch) -> None:
    # No default_tz, no --tz: a single-event RSVP must not touch a clock.
    client = MagicMock()
    client.me.events.by_event_id.return_value.decline.post = AsyncMock(return_value=None)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="", client_id="x"),
    )
    payload = asyncio.run(calendar_decline(event_id="evt-1"))
    assert payload == {"declined": ["evt-1"], "count": 1, "skipped": []}


def test_graph_decline_propose_time_needs_single_event(monkeypatch) -> None:
    _graph_client(monkeypatch, today_items=[])
    with pytest.raises(ValueError, match="needs a single --event-id"):
        asyncio.run(
            calendar_decline(today_pending=True, propose_start="2026-09-02T15:00", tz_name=_NY)
        )


def _odata_error(status: int, code: str | None = None) -> ODataError:
    err = ODataError()
    err.response_status_code = status
    if code is not None:
        err.error = MainError(code=code)
    return err


@pytest.mark.parametrize("err", [_odata_error(404), _odata_error(400, "ErrorItemNotFound")])
def test_graph_decline_missing_event_maps_not_found(monkeypatch, err) -> None:
    client = _graph_client(monkeypatch)
    client.me.events.by_event_id.return_value.decline.post = AsyncMock(side_effect=err)
    with pytest.raises(CalendarEventNotFoundError, match="event not found: evt-1"):
        asyncio.run(calendar_decline(event_id="evt-1", tz_name=_NY))


def test_graph_decline_today_pending_reports_skips(monkeypatch) -> None:
    items = [
        {"id": "a", "is_organizer": False, "response": "ResponseType.NotResponded"},
        {"id": "b", "is_organizer": False, "response": "ResponseType.NotResponded"},
    ]
    client = _graph_client(monkeypatch, today_items=items)
    client.me.events.by_event_id.return_value.decline.post = AsyncMock(
        side_effect=[None, RuntimeError("gone")]
    )
    payload = asyncio.run(calendar_decline(today_pending=True, tz_name=_NY))
    assert payload["declined"] == ["a"]
    assert payload["skipped"] == [{"id": "b", "reason": "gone"}]


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


def _google_service(event: dict) -> MagicMock:
    service = MagicMock()
    service.events.return_value.get.return_value.execute.return_value = event
    service.events.return_value.patch.return_value.execute.return_value = {"id": event["id"]}
    return service


def _google_patched(service: MagicMock):
    return patch.multiple(
        _GOOGLE_CAL,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    )


def test_google_decline_patches_response_status_and_comment(tmp_path: Path) -> None:
    event = {
        "id": "evt-1",
        "attendees": [
            {"email": "me@example.com", "self": True, "responseStatus": "needsAction"},
            {"email": "sam@example.com", "responseStatus": "accepted"},
        ],
    }
    service = _google_service(event)
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_decline(
                event_id="evt-1", comment="no can do"
            )
        )
    assert payload == {"declined": ["evt-1"], "count": 1, "skipped": []}
    body = service.events.return_value.patch.call_args.kwargs["body"]
    mine = next(a for a in body["attendees"] if a.get("self"))
    assert mine["responseStatus"] == "declined"
    assert mine["comment"] == "no can do"
    # the other attendee is preserved unchanged
    assert {a["email"] for a in body["attendees"]} == {"me@example.com", "sam@example.com"}


def test_google_tentative_sets_tentative(tmp_path: Path) -> None:
    event = {"id": "evt-1", "attendees": [{"email": "me@e.com", "self": True}]}
    service = _google_service(event)
    with _google_patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_tentative(event_id="evt-1")
        )
    body = service.events.return_value.patch.call_args.kwargs["body"]
    assert body["attendees"][0]["responseStatus"] == "tentative"


def test_google_propose_time_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no propose-new-time"):
        asyncio.run(
            google_calendar.calendar_decline(
                event_id="evt-1",
                propose_start="2026-09-02T15:00",
                config=_google_cfg(tmp_path),
            )
        )


# --------------------------------------------------------------------------- formatter


def test_format_rsvp_human_labels_each_action() -> None:
    assert "Declined 1 event(s):" in format_rsvp_human({"declined": ["e"], "count": 1})[0]
    assert "Marked tentative on 2 event(s):" in format_rsvp_human({"tentative": ["a", "b"]})[0]
    lines = format_rsvp_human({"declined": ["a"], "skipped": [{"id": "b", "reason": "gone"}]})
    assert any("skipped b: gone" in line for line in lines)


# --------------------------------------------------------------------------- CLI


def test_cli_decline_and_tentative_help() -> None:
    for verb in ("decline", "tentative"):
        out = CliRunner().invoke(main, ["calendar", verb, "--help"]).output
        for flag in ("--event-id", "--today-pending", "--comment", "--propose-time", "--yes"):
            assert flag in out, (verb, flag)


def test_cli_decline_requires_yes() -> None:
    result = CliRunner().invoke(main, ["calendar", "decline", "--event-id", "e", "--json"])
    assert result.exit_code == EXIT_USAGE


def test_cli_decline_routes_not_found(monkeypatch) -> None:
    async def _raise(**_kwargs):
        raise CalendarEventNotFoundError("event not found: e")

    monkeypatch.setattr("blumkin.cli._workspace", lambda: SimpleNamespace(calendar_decline=_raise))
    result = CliRunner().invoke(main, ["calendar", "decline", "--event-id", "e", "--yes", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert json.loads(result.stderr)["error"] == "not_found"
