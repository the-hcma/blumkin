"""`calendar remove-cancelled`: an attendee clears a meeting the organizer cancelled (#757)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from googleapiclient.errors import HttpError
from msgraph.generated.models.o_data_errors.main_error import MainError
from msgraph.generated.models.o_data_errors.o_data_error import ODataError

from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig, PreferencesConfig, load_config
from blumkin.exit_codes import EXIT_USAGE
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.read_state import record_read
from blumkin.skills.calendar import CalendarEventNotCancelledError, CalendarEventNotFoundError
from blumkin.skills.calendar_writes import calendar_remove_cancelled, format_remove_cancelled_human
from blumkin.skills.errors import classify_exception

_GOOGLE_CAL = "blumkin.providers.google.calendar"


# --------------------------------------------------------------------------- Graph


def _graph(monkeypatch, *, cancelled: bool, etag: str | None = None) -> MagicMock:
    client = MagicMock()
    builder = client.me.events.by_event_id.return_value
    extra = {"@odata.etag": etag} if etag else {}
    builder.get = AsyncMock(
        return_value=SimpleNamespace(id="evt-1", is_cancelled=cancelled, additional_data=extra)
    )
    builder.delete = AsyncMock(return_value=None)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    return client


def _odata_error(status: int, code: str | None = None) -> ODataError:
    err = ODataError()
    err.response_status_code = status
    if code is not None:
        err.error = MainError(code=code)
    return err


def test_graph_removes_a_cancelled_meeting(monkeypatch) -> None:
    client = _graph(monkeypatch, cancelled=True)
    payload = asyncio.run(calendar_remove_cancelled(event_id="evt-1"))
    assert payload == {"removed": "evt-1"}
    client.me.events.by_event_id.return_value.delete.assert_awaited_once()


def test_graph_delete_is_conditional_on_the_etag_that_was_read(monkeypatch) -> None:
    client = _graph(monkeypatch, cancelled=True, etag='W/"e1"')
    asyncio.run(calendar_remove_cancelled(event_id="evt-1"))
    config = client.me.events.by_event_id.return_value.delete.await_args.args[0]
    assert config.headers.get("If-Match") == {'W/"e1"'}


def test_graph_a_meeting_restored_after_the_read_is_not_deleted(monkeypatch) -> None:
    client = _graph(monkeypatch, cancelled=True, etag='W/"e1"')
    client.me.events.by_event_id.return_value.delete = AsyncMock(side_effect=_odata_error(412))
    with pytest.raises(CalendarEventNotCancelledError, match="changed while it was being removed"):
        asyncio.run(calendar_remove_cancelled(event_id="evt-1"))


def test_graph_refuses_a_meeting_that_is_still_on_and_deletes_nothing(monkeypatch) -> None:
    client = _graph(monkeypatch, cancelled=False)
    with pytest.raises(CalendarEventNotCancelledError, match="still on, only a cancelled one"):
        asyncio.run(calendar_remove_cancelled(event_id="evt-1"))
    client.me.events.by_event_id.return_value.delete.assert_not_awaited()


@pytest.mark.parametrize("err", [_odata_error(404), _odata_error(400, "ErrorItemNotFound")])
def test_graph_a_missing_event_is_not_found(monkeypatch, err) -> None:
    client = _graph(monkeypatch, cancelled=True)
    client.me.events.by_event_id.return_value.get = AsyncMock(side_effect=err)
    with pytest.raises(CalendarEventNotFoundError, match="event not found: evt-1"):
        asyncio.run(calendar_remove_cancelled(event_id="evt-1"))
    client.me.events.by_event_id.return_value.delete.assert_not_awaited()


def test_graph_an_empty_event_id_is_a_usage_error(monkeypatch) -> None:
    _graph(monkeypatch, cancelled=True)
    with pytest.raises(ValueError, match="--event-id is required"):
        asyncio.run(calendar_remove_cancelled(event_id="  "))


# --------------------------------------------------------------------------- Google


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
        mail_signature=MailSignatureConfig(),
        preferences=PreferencesConfig(),
        profile="default",
        provider=ProviderKind.GOOGLE,
        tags=(),
        tenant_id="",
        wo1162425_scopes=False,
    )


def _google_patched(service: MagicMock):
    return patch.multiple(
        _GOOGLE_CAL,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    )


def _google_service(event: dict) -> MagicMock:
    service = MagicMock()
    service.events.return_value.get.return_value.execute.return_value = event
    return service


def test_google_removes_a_cancelled_meeting_without_notifying_anyone(tmp_path: Path) -> None:
    service = _google_service({"id": "evt-1", "status": "cancelled"})
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_remove_cancelled(
                event_id="evt-1"
            )
        )
    assert payload == {"removed": "evt-1"}
    delete = service.events.return_value.delete
    assert delete.call_args.kwargs["sendUpdates"] == "none"
    assert delete.call_args.kwargs["calendarId"] == "primary"


def test_google_delete_is_conditional_on_the_etag_that_was_read(tmp_path: Path) -> None:
    service = _google_service({"id": "evt-1", "status": "cancelled", "etag": '"e1"'})
    delete_request = service.events.return_value.delete.return_value
    delete_request.headers = {}
    with _google_patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_remove_cancelled(
                event_id="evt-1"
            )
        )
    assert delete_request.headers["If-Match"] == '"e1"'


def test_google_a_meeting_restored_after_the_read_is_not_deleted(tmp_path: Path) -> None:
    service = _google_service({"id": "evt-1", "status": "cancelled", "etag": '"e1"'})
    delete_request = service.events.return_value.delete.return_value
    delete_request.headers = {}
    delete_request.execute.side_effect = HttpError(
        SimpleNamespace(status=412, reason="Precondition Failed"), b"{}"
    )
    with (
        _google_patched(service),
        pytest.raises(CalendarEventNotCancelledError, match="changed while it was being removed"),
    ):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_remove_cancelled(
                event_id="evt-1"
            )
        )


def test_google_an_already_deleted_meeting_counts_as_removed(tmp_path: Path) -> None:
    service = _google_service({"id": "evt-1", "status": "cancelled", "etag": '"e1"'})
    delete_request = service.events.return_value.delete.return_value
    delete_request.headers = {}
    delete_request.execute.side_effect = HttpError(
        SimpleNamespace(status=410, reason="Gone"), b"{}"
    )
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_remove_cancelled(
                event_id="evt-1"
            )
        )
    assert payload == {"removed": "evt-1"}


def test_google_refuses_a_meeting_that_is_still_on_and_deletes_nothing(tmp_path: Path) -> None:
    service = _google_service({"id": "evt-1", "status": "confirmed"})
    with (
        _google_patched(service),
        pytest.raises(CalendarEventNotCancelledError, match="still on, only a cancelled one"),
    ):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_remove_cancelled(
                event_id="evt-1"
            )
        )
    service.events.return_value.delete.assert_not_called()


def test_google_a_missing_event_is_not_found(tmp_path: Path) -> None:
    service = MagicMock()
    gone = HttpError(SimpleNamespace(status=404, reason="Not Found"), b"{}")
    service.events.return_value.get.return_value.execute.side_effect = gone
    with (
        _google_patched(service),
        pytest.raises(CalendarEventNotFoundError, match="event not found: evt-1"),
    ):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_remove_cancelled(
                event_id="evt-1"
            )
        )
    service.events.return_value.delete.assert_not_called()


# --------------------------------------------------------------------------- CLI and errors


def _cli_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.default]\nclient_id = "abc"\n')


def test_cli_help_names_the_command_and_its_limits() -> None:
    out = CliRunner().invoke(main, ["calendar", "remove-cancelled", "--help"]).output
    for text in ("--event-id", "--yes", "Remove from calendar", "still on"):
        assert text in out, text


def test_cli_requires_yes(tmp_path, monkeypatch) -> None:
    _cli_config(tmp_path, monkeypatch)
    result = CliRunner().invoke(main, ["calendar", "remove-cancelled", "--event-id", "e", "--json"])
    assert result.exit_code == EXIT_USAGE
    assert json.loads(result.stderr)["error"] == "usage_error"


def test_cli_needs_a_fresh_calendar_get_first(tmp_path, monkeypatch) -> None:
    _cli_config(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        main, ["calendar", "remove-cancelled", "--event-id", "e", "--yes", "--json"]
    )
    assert result.exit_code == EXIT_USAGE
    assert json.loads(result.stderr)["error"] == "stale_or_unread"


def test_cli_removes_a_cancelled_meeting_after_a_fresh_read(tmp_path, monkeypatch) -> None:
    async def _remove(**kwargs):
        return {"removed": kwargs["event_id"]}

    monkeypatch.setattr(
        "blumkin.cli._workspace", lambda: SimpleNamespace(calendar_remove_cancelled=_remove)
    )
    _cli_config(tmp_path, monkeypatch)
    record_read(load_config(), "e")
    result = CliRunner().invoke(
        main, ["calendar", "remove-cancelled", "--event-id", "e", "--yes", "--json"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"ok": True, "removed": "e"}
    human = CliRunner().invoke(main, ["calendar", "remove-cancelled", "--event-id", "e", "--yes"])
    assert human.exit_code == 0
    assert "Removed cancelled event 'e' from your calendar" in human.output


def test_cli_a_meeting_that_is_still_on_is_a_usage_error_with_a_hint(tmp_path, monkeypatch) -> None:
    async def _refuse(**_kwargs):
        raise CalendarEventNotCancelledError("cannot remove a meeting that is still on: e")

    monkeypatch.setattr(
        "blumkin.cli._workspace", lambda: SimpleNamespace(calendar_remove_cancelled=_refuse)
    )
    _cli_config(tmp_path, monkeypatch)
    record_read(load_config(), "e")
    result = CliRunner().invoke(
        main, ["calendar", "remove-cancelled", "--event-id", "e", "--yes", "--json"]
    )
    assert result.exit_code == EXIT_USAGE
    payload = json.loads(result.stderr)
    assert payload["error"] == "usage_error"
    assert "calendar decline" in payload["hint"] and "calendar cancel" in payload["hint"]


def test_the_classifier_and_the_formatter_agree() -> None:
    info = classify_exception(CalendarEventNotCancelledError("still on"))
    assert (info.slug, info.exit_code) == ("usage_error", EXIT_USAGE)
    assert info.hint is not None and "is_cancelled" in info.hint
    assert format_remove_cancelled_human({"removed": "e"}) == [
        "Removed cancelled event 'e' from your calendar"
    ]
