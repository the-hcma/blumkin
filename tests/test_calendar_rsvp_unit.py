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
from blumkin.config import BlumkinConfig, MailSignatureConfig, PreferencesConfig, load_config
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_USAGE
from blumkin.providers.google import calendar as google_calendar
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.read_state import record_read
from blumkin.skills.calendar import CalendarEventCancelledError, CalendarEventNotFoundError
from blumkin.skills.calendar_writes import (
    calendar_accept,
    calendar_decline,
    calendar_tentative,
    format_rsvp_human,
)

_GOOGLE_CAL = "blumkin.providers.google.calendar"
_NY = "America/New_York"


# --------------------------------------------------------------------------- Graph


def _graph_client(monkeypatch, *, today_items=None, today_complete=True) -> MagicMock:
    client = MagicMock()
    # The pre-check of #757 reads `isCancelled` before every RSVP: a live meeting by default.
    client.me.events.by_event_id.return_value.get = AsyncMock(
        return_value=SimpleNamespace(id="evt", is_cancelled=False)
    )
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
            return {"items": today_items, "complete": today_complete}

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


def test_graph_decline_empty_propose_time_is_rejected(monkeypatch) -> None:
    _graph_client(monkeypatch)
    with pytest.raises(ValueError, match="--propose-time cannot be empty"):
        asyncio.run(calendar_decline(event_id="evt-1", propose_start="  ", tz_name=_NY))


def test_graph_decline_propose_duration_zero_rejected(monkeypatch) -> None:
    _graph_client(monkeypatch)
    with pytest.raises(ValueError, match="--propose-duration must be positive"):
        asyncio.run(
            calendar_decline(
                event_id="evt-1",
                propose_start="2026-09-02T15:00",
                propose_duration="0m",
                tz_name=_NY,
            )
        )


def test_cli_decline_bad_timezone_with_propose_is_usage_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "blumkin.cli._workspace", lambda: SimpleNamespace(calendar_decline=AsyncMock())
    )
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.default]\nclient_id = "abc"\n')
    record_read(load_config(), "e")
    result = CliRunner().invoke(
        main,
        [
            "calendar",
            "decline",
            "--event-id",
            "e",
            "--propose-time",
            "2026-09-02T15:00",
            "--tz",
            "Not/AZone",
            "--yes",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_USAGE
    # Discriminate from `stale_or_unread` (same exit code) - the freshness gate
    # (fed by the `record_read` above) must have let this reach the ZoneInfo
    # precheck, not merely failed for an unrelated reason.
    assert json.loads(result.stderr)["error"] == "usage_error"


def test_graph_decline_single_event_needs_no_timezone(monkeypatch) -> None:
    # No default_tz, no --tz: a single-event RSVP must not touch a clock.
    client = MagicMock()
    client.me.events.by_event_id.return_value.get = AsyncMock(
        return_value=SimpleNamespace(id="evt-1", is_cancelled=False)
    )
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


def test_graph_decline_today_pending_all_fail_propagates(monkeypatch) -> None:
    items = [
        {"id": "a", "is_organizer": False, "response": "ResponseType.NotResponded"},
        {"id": "b", "is_organizer": False, "response": "ResponseType.NotResponded"},
    ]
    client = _graph_client(monkeypatch, today_items=items)
    client.me.events.by_event_id.return_value.decline.post = AsyncMock(
        side_effect=RuntimeError("token expired")
    )
    with pytest.raises(RuntimeError, match="token expired"):
        asyncio.run(calendar_decline(today_pending=True, tz_name=_NY))


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


def _builder(*, cancelled: bool) -> MagicMock:
    builder = MagicMock()
    builder.get = AsyncMock(return_value=SimpleNamespace(id="evt", is_cancelled=cancelled))
    for action in ("accept", "decline", "tentatively_accept"):
        getattr(builder, action).post = AsyncMock(return_value=None)
    return builder


@pytest.mark.parametrize(
    ("respond", "endpoint", "phrase"),
    [
        (calendar_accept, "accept", "cannot accept a meeting"),
        (calendar_decline, "decline", "cannot decline a meeting"),
        (calendar_tentative, "tentatively_accept", "cannot mark tentative a meeting"),
    ],
)
def test_graph_rsvp_refuses_a_cancelled_meeting_and_sends_no_response(
    monkeypatch, respond, endpoint, phrase
) -> None:
    """A cancelled meeting is refused before any response is sent (#757)."""
    client = _graph_client(monkeypatch)
    client.me.events.by_event_id.return_value.get = AsyncMock(
        return_value=SimpleNamespace(id="evt-1", is_cancelled=True)
    )
    with pytest.raises(CalendarEventCancelledError, match=f"{phrase} that was cancelled"):
        asyncio.run(respond(event_id="evt-1", tz_name=_NY))
    getattr(client.me.events.by_event_id.return_value, endpoint).post.assert_not_awaited()


def test_graph_decline_today_pending_skips_only_the_cancelled_meeting(monkeypatch) -> None:
    items = [
        {"id": "a", "is_organizer": False, "response": "ResponseType.NotResponded"},
        {"id": "b", "is_organizer": False, "response": "ResponseType.NotResponded"},
    ]
    client = _graph_client(monkeypatch, today_items=items)
    builders = {"a": _builder(cancelled=False), "b": _builder(cancelled=True)}
    client.me.events.by_event_id.side_effect = lambda event_id: builders[event_id]
    payload = asyncio.run(calendar_decline(today_pending=True, tz_name=_NY))
    assert payload["declined"] == ["a"]
    assert [entry["id"] for entry in payload["skipped"]] == ["b"]
    assert "cancelled" in payload["skipped"][0]["reason"]
    builders["b"].decline.post.assert_not_awaited()


@pytest.mark.parametrize("err", [_odata_error(404), _odata_error(400, "ErrorItemNotFound")])
def test_graph_a_lookup_failure_in_the_pre_check_maps_not_found(monkeypatch, err) -> None:
    client = _graph_client(monkeypatch)
    client.me.events.by_event_id.return_value.get = AsyncMock(side_effect=err)
    with pytest.raises(CalendarEventNotFoundError, match="event not found: evt-1"):
        asyncio.run(calendar_decline(event_id="evt-1", tz_name=_NY))
    client.me.events.by_event_id.return_value.decline.post.assert_not_awaited()


def test_cli_a_cancelled_meeting_is_event_cancelled_with_an_actionable_hint(
    tmp_path, monkeypatch
) -> None:
    async def _raise(**_kwargs):
        raise CalendarEventCancelledError("cannot decline a meeting that was cancelled: e")

    monkeypatch.setattr("blumkin.cli._workspace", lambda: SimpleNamespace(calendar_decline=_raise))
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.default]\nclient_id = "abc"\n')
    record_read(load_config(), "e")
    result = CliRunner().invoke(main, ["calendar", "decline", "--event-id", "e", "--yes", "--json"])
    assert result.exit_code == EXIT_USAGE
    payload = json.loads(result.stderr)
    assert payload["error"] == "event_cancelled"
    assert "cancelled" in payload["message"]
    assert "is_cancelled" in payload["hint"] and "calendar remove-cancelled" in payload["hint"]


def test_graph_a_day_of_only_cancelled_meetings_reports_them_and_succeeds(monkeypatch) -> None:
    """An expected per-event condition is not a failure of the batch (#757)."""
    items = [
        {"id": "a", "is_organizer": False, "response": "ResponseType.NotResponded"},
        {"id": "b", "is_organizer": False, "response": "ResponseType.NotResponded"},
    ]
    client = _graph_client(monkeypatch, today_items=items)
    client.me.events.by_event_id.side_effect = lambda _event_id: _builder(cancelled=True)
    payload = asyncio.run(calendar_decline(today_pending=True, tz_name=_NY))
    assert payload["declined"] == [] and payload["count"] == 0
    assert [entry["id"] for entry in payload["skipped"]] == ["a", "b"]
    assert all("cancelled" in entry["reason"] for entry in payload["skipped"])


def test_graph_a_cancelled_meeting_does_not_hide_a_real_failure_in_the_same_batch(
    monkeypatch,
) -> None:
    items = [
        {"id": "a", "is_organizer": False, "response": "ResponseType.NotResponded"},
        {"id": "b", "is_organizer": False, "response": "ResponseType.NotResponded"},
    ]
    client = _graph_client(monkeypatch, today_items=items)
    failing = _builder(cancelled=False)
    failing.decline.post = AsyncMock(side_effect=RuntimeError("token expired"))
    builders = {"a": _builder(cancelled=True), "b": failing}
    client.me.events.by_event_id.side_effect = lambda event_id: builders[event_id]
    with pytest.raises(RuntimeError, match="token expired"):
        asyncio.run(calendar_decline(today_pending=True, tz_name=_NY))


def test_the_human_report_lists_a_batch_of_only_skipped_meetings() -> None:
    lines = format_rsvp_human(
        {"declined": [], "count": 0, "skipped": [{"id": "a", "reason": "cancelled"}]}
    )
    assert any("skipped a: cancelled" in line for line in lines)


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
        mail_signature=MailSignatureConfig(),
        preferences=PreferencesConfig(),
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


@pytest.mark.parametrize(
    ("verb", "phrase"),
    [
        ("accept", "cannot accept a meeting"),
        ("decline", "cannot decline a meeting"),
        ("tentative", "cannot mark tentative a meeting"),
    ],
)
def test_google_rsvp_refuses_a_cancelled_meeting_and_sends_nothing(
    tmp_path: Path, verb: str, phrase: str
) -> None:
    """The Google path refuses a cancelled meeting like the Microsoft one (#757)."""
    event = {
        "id": "evt-1",
        "status": "cancelled",
        "attendees": [{"email": "me@e.com", "self": True, "responseStatus": "needsAction"}],
    }
    service = _google_service(event)
    provider = GoogleWorkspaceProvider(_google_cfg(tmp_path))
    with _google_patched(service), pytest.raises(CalendarEventCancelledError, match=phrase):
        asyncio.run(getattr(provider, f"calendar_{verb}")(event_id="evt-1"))
    service.events.return_value.patch.assert_not_called()


def test_google_today_pending_skips_only_the_cancelled_meeting(tmp_path: Path) -> None:
    """A Google batch reports a cancelled meeting under `skipped` and carries on (#757)."""
    events = {
        "a": {
            "id": "a",
            "status": "confirmed",
            "attendees": [{"email": "me@e.com", "self": True, "responseStatus": "needsAction"}],
        },
        "b": {
            "id": "b",
            "status": "cancelled",
            "attendees": [{"email": "me@e.com", "self": True, "responseStatus": "needsAction"}],
        },
    }
    service = MagicMock()

    def _get(*, calendarId, eventId):  # noqa: N803 - the Google client's keyword names
        request = MagicMock()
        request.execute.return_value = events[eventId]
        return request

    service.events.return_value.get.side_effect = _get
    service.events.return_value.patch.return_value.execute.return_value = {"id": "a"}

    async def _today(**_kwargs):
        pending = {"is_organizer": False, "response": "ResponseType.NotResponded"}
        return {"items": [{"id": "a", **pending}, {"id": "b", **pending}], "complete": True}

    with _google_patched(service), patch(f"{_GOOGLE_CAL}.calendar_today", _today):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_accept(today_pending=True)
        )
    assert payload["accepted"] == ["a"]
    assert [entry["id"] for entry in payload["skipped"]] == ["b"]
    assert "cancelled" in payload["skipped"][0]["reason"]
    assert service.events.return_value.patch.call_count == 1


def test_google_a_day_of_only_cancelled_meetings_reports_them_and_succeeds(
    tmp_path: Path,
) -> None:
    events = {
        key: {
            "id": key,
            "status": "cancelled",
            "attendees": [{"email": "me@e.com", "self": True, "responseStatus": "needsAction"}],
        }
        for key in ("a", "b")
    }
    service = MagicMock()

    def _get(*, calendarId, eventId):  # noqa: N803 - the Google client's keyword names
        request = MagicMock()
        request.execute.return_value = events[eventId]
        return request

    service.events.return_value.get.side_effect = _get

    async def _today(**_kwargs):
        pending = {"is_organizer": False, "response": "ResponseType.NotResponded"}
        return {"items": [{"id": "a", **pending}, {"id": "b", **pending}], "complete": True}

    with _google_patched(service), patch(f"{_GOOGLE_CAL}.calendar_today", _today):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_accept(today_pending=True)
        )
    assert payload["accepted"] == [] and payload["count"] == 0
    assert [entry["id"] for entry in payload["skipped"]] == ["a", "b"]
    service.events.return_value.patch.assert_not_called()


def test_google_a_live_meeting_is_still_answered(tmp_path: Path) -> None:
    event = {
        "id": "evt-1",
        "status": "confirmed",
        "attendees": [{"email": "me@e.com", "self": True, "responseStatus": "needsAction"}],
    }
    service = _google_service(event)
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_accept(event_id="evt-1")
        )
    assert payload == {"accepted": ["evt-1"], "count": 1, "skipped": []}


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


def test_cli_decline_routes_not_found(tmp_path, monkeypatch) -> None:
    async def _raise(**_kwargs):
        raise CalendarEventNotFoundError("event not found: e")

    monkeypatch.setattr("blumkin.cli._workspace", lambda: SimpleNamespace(calendar_decline=_raise))
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.default]\nclient_id = "abc"\n')
    record_read(load_config(), "e")
    result = CliRunner().invoke(main, ["calendar", "decline", "--event-id", "e", "--yes", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert json.loads(result.stderr)["error"] == "not_found"


def test_graph_decline_today_pending_refuses_a_day_that_was_not_read_in_full(
    monkeypatch,
) -> None:
    items = [{"id": "a", "is_organizer": False, "response": "ResponseType.NotResponded"}]
    client = _graph_client(monkeypatch, today_items=items, today_complete=False)

    with pytest.raises(ValueError, match="read in full.*its own --event-id"):
        asyncio.run(calendar_decline(today_pending=True, tz_name=_NY))

    client.me.events.by_event_id.return_value.decline.post.assert_not_awaited()


def test_the_incomplete_day_refusal_never_names_one_rsvp_action(monkeypatch) -> None:
    items = [{"id": "a", "is_organizer": False, "response": "ResponseType.NotResponded"}]
    _graph_client(monkeypatch, today_items=items, today_complete=False)

    for action in (calendar_accept, calendar_decline, calendar_tentative):
        with pytest.raises(ValueError) as caught:
            asyncio.run(action(today_pending=True, tz_name=_NY))
        assert "accept" not in str(caught.value).lower()
        assert "decline" not in str(caught.value).lower()
