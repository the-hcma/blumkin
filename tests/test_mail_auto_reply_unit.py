"""Hermetic coverage for mail auto-reply / vacation responder (issue #179)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from msgraph.generated.models.automatic_replies_status import AutomaticRepliesStatus
from msgraph.generated.models.external_audience_scope import ExternalAudienceScope

from blumkin.auth import MissingScopeError
from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig, load_config
from blumkin.exit_codes import EXIT_MISSING_SCOPE, EXIT_USAGE
from blumkin.providers.google import mail_writes as google_mail_writes
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.skills.mail import format_auto_reply_human, mail_auto_reply


def _graph(monkeypatch) -> MagicMock:
    client = MagicMock()
    client.me.mailbox_settings.get = AsyncMock(
        return_value=SimpleNamespace(automatic_replies_setting=None)
    )
    client.me.mailbox_settings.patch = AsyncMock(
        side_effect=lambda settings: SimpleNamespace(
            automatic_replies_setting=settings.automatic_replies_setting
        )
    )
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    return client


# --------------------------------------------------------------------------- Graph


def test_graph_read_returns_the_current_setting(monkeypatch) -> None:
    client = _graph(monkeypatch)
    client.me.mailbox_settings.get = AsyncMock(
        return_value=SimpleNamespace(
            automatic_replies_setting=SimpleNamespace(
                status=AutomaticRepliesStatus.AlwaysEnabled,
                internal_reply_message="brb",
                external_reply_message="brb (external)",
                external_audience=ExternalAudienceScope.ContactsOnly,
                scheduled_start_date_time=None,
                scheduled_end_date_time=None,
            )
        )
    )
    payload = asyncio.run(mail_auto_reply())
    assert payload["auto_reply"]["enabled"] is True
    assert payload["auto_reply"]["scope"] == "alwaysEnabled"
    assert payload["auto_reply"]["internal_message"] == "brb"
    assert payload["auto_reply"]["external_message"] == "brb (external)"
    assert payload["auto_reply"]["external_audience"] == "contacts"
    client.me.mailbox_settings.patch.assert_not_awaited()


def test_graph_turn_on_always_enabled(monkeypatch) -> None:
    client = _graph(monkeypatch)
    payload = asyncio.run(mail_auto_reply(enable=True, message="out today"))
    setting = client.me.mailbox_settings.patch.await_args.args[0].automatic_replies_setting
    assert setting.status == AutomaticRepliesStatus.AlwaysEnabled
    assert setting.internal_reply_message == "out today"
    assert setting.external_reply_message == "out today"
    assert setting.external_audience == ExternalAudienceScope.All
    assert payload["auto_reply"]["enabled"] is True


def test_graph_scheduled_window_and_separate_external(monkeypatch) -> None:
    client = _graph(monkeypatch)
    asyncio.run(
        mail_auto_reply(
            enable=True,
            message="internal",
            external_message="external",
            external_audience="none",
            start=date(2026, 9, 10),
            until=date(2026, 9, 15),
        )
    )
    setting = client.me.mailbox_settings.patch.await_args.args[0].automatic_replies_setting
    assert setting.status == AutomaticRepliesStatus.Scheduled
    assert setting.internal_reply_message == "internal"
    assert setting.external_reply_message == "external"
    assert setting.external_audience == ExternalAudienceScope.None_
    # Boundaries resolve in the profile tz (UTC here); --until is inclusive, so the
    # end boundary is local midnight of the day after 2026-09-15.
    assert setting.scheduled_start_date_time.date_time == "2026-09-10T00:00:00"
    assert setting.scheduled_start_date_time.time_zone == "UTC"
    assert setting.scheduled_end_date_time.date_time == "2026-09-16T00:00:00"


def test_graph_scheduled_window_resolves_the_profile_tz(monkeypatch) -> None:
    client = _graph(monkeypatch)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="America/Los_Angeles", client_id="x"),
    )
    asyncio.run(
        mail_auto_reply(enable=True, message="x", start=date(2026, 9, 10), until=date(2026, 9, 15))
    )
    setting = client.me.mailbox_settings.patch.await_args.args[0].automatic_replies_setting
    assert setting.scheduled_start_date_time.time_zone == "America/Los_Angeles"
    assert setting.scheduled_end_date_time.time_zone == "America/Los_Angeles"
    assert setting.scheduled_end_date_time.date_time == "2026-09-16T00:00:00"


def test_graph_turn_off(monkeypatch) -> None:
    client = _graph(monkeypatch)
    payload = asyncio.run(mail_auto_reply(enable=False))
    setting = client.me.mailbox_settings.patch.await_args.args[0].automatic_replies_setting
    assert setting.status == AutomaticRepliesStatus.Disabled
    assert payload["auto_reply"]["enabled"] is False


def test_graph_turn_on_needs_a_body(monkeypatch) -> None:
    _graph(monkeypatch)
    with pytest.raises(ValueError, match="needs --message"):
        asyncio.run(mail_auto_reply(enable=True))


def test_graph_message_and_file_are_mutually_exclusive(monkeypatch, tmp_path: Path) -> None:
    _graph(monkeypatch)
    body_file = tmp_path / "oof.txt"
    body_file.write_text("from a file")
    with pytest.raises(ValueError, match="only one of"):
        asyncio.run(mail_auto_reply(enable=True, message="inline", message_file=str(body_file)))


def test_graph_reads_the_body_file(monkeypatch, tmp_path: Path) -> None:
    client = _graph(monkeypatch)
    body_file = tmp_path / "oof.txt"
    body_file.write_text("away, see you soon")
    asyncio.run(mail_auto_reply(enable=True, message_file=str(body_file)))
    setting = client.me.mailbox_settings.patch.await_args.args[0].automatic_replies_setting
    assert setting.internal_reply_message == "away, see you soon"


# --------------------------------------------------------------------------- Google


def _google_cfg(config_dir: Path) -> BlumkinConfig:
    oauth = config_dir / "desktop-client.json"
    oauth.write_text('{"installed": {"client_id": "id.apps.googleusercontent.com"}}')
    return BlumkinConfig(
        client_id="id.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="UTC",
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


def _google_patched(service: MagicMock):
    return patch.multiple(
        "blumkin.providers.google.mail_writes",
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    )


def _vacation_call(service: MagicMock):
    return service.users.return_value.settings.return_value


def test_google_read_maps_to_the_graph_shape(tmp_path: Path) -> None:
    service = MagicMock()
    _vacation_call(service).getVacation.return_value.execute.return_value = {
        "enableAutoReply": True,
        "responseBodyPlainText": "on holiday",
        "restrictToContacts": True,
    }
    with _google_patched(service):
        payload = asyncio.run(GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_auto_reply())
    oof = payload["auto_reply"]
    assert oof == {
        "enabled": True,
        "scope": "alwaysEnabled",
        "start": None,
        "end": None,
        "internal_message": "on holiday",
        "external_message": "on holiday",
        "external_audience": "contacts",
    }


def test_google_turn_on_with_window(tmp_path: Path) -> None:
    service = MagicMock()
    _vacation_call(service).updateVacation.return_value.execute.return_value = {
        "enableAutoReply": True,
        "responseBodyPlainText": "back soon",
        "startTime": "1788998400000",
    }
    with _google_patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_auto_reply(
                enable=True,
                message="back soon",
                external_audience="none",
                start=date(2026, 9, 10),
            )
        )
    body = _vacation_call(service).updateVacation.call_args.kwargs["body"]
    assert body["enableAutoReply"] is True
    assert body["responseBodyPlainText"] == "back soon"
    assert body["restrictToContacts"] is False
    assert body["restrictToDomain"] is True
    assert body["startTime"] == 1788998400000


def test_google_until_is_inclusive_local_midnight_of_the_next_day(tmp_path: Path) -> None:
    service = MagicMock()
    _vacation_call(service).updateVacation.return_value.execute.return_value = {
        "enableAutoReply": True
    }
    with _google_patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_auto_reply(
                enable=True,
                message="x",
                start=date(2026, 9, 10),
                until=date(2026, 9, 15),
            )
        )
    body = _vacation_call(service).updateVacation.call_args.kwargs["body"]
    # default_tz is UTC here: start = 2026-09-10T00:00Z, end = 2026-09-16T00:00Z (inclusive).
    assert body["startTime"] == 1788998400000
    assert body["endTime"] == 1789516800000


def test_google_always_on_does_not_crash_when_default_tz_is_unset(tmp_path: Path) -> None:
    from dataclasses import replace

    service = MagicMock()
    _vacation_call(service).updateVacation.return_value.execute.return_value = {
        "enableAutoReply": True
    }
    cfg = replace(_google_cfg(tmp_path), default_tz="")
    with _google_patched(service):
        asyncio.run(google_mail_writes.mail_auto_reply(enable=True, message="brb", config=cfg))
    body = _vacation_call(service).updateVacation.call_args.kwargs["body"]
    assert body["enableAutoReply"] is True
    assert "startTime" not in body  # no schedule requested, tz never resolved


def test_google_scheduled_window_falls_back_to_utc_when_default_tz_is_unset(tmp_path: Path) -> None:
    from dataclasses import replace

    service = MagicMock()
    _vacation_call(service).updateVacation.return_value.execute.return_value = {
        "enableAutoReply": True
    }
    cfg = replace(_google_cfg(tmp_path), default_tz="")
    with _google_patched(service):
        asyncio.run(
            google_mail_writes.mail_auto_reply(
                enable=True, message="x", start=date(2026, 9, 10), config=cfg
            )
        )
    body = _vacation_call(service).updateVacation.call_args.kwargs["body"]
    assert body["startTime"] == 1788998400000  # UTC fallback


def test_google_turn_off_keeps_the_existing_body(tmp_path: Path) -> None:
    service = MagicMock()
    _vacation_call(service).getVacation.return_value.execute.return_value = {
        "enableAutoReply": True,
        "responseBodyPlainText": "away",
        "restrictToContacts": True,
        "startTime": "1788998400000",
    }
    _vacation_call(service).updateVacation.return_value.execute.return_value = {
        "enableAutoReply": False,
        "responseBodyPlainText": "away",
    }
    with _google_patched(service):
        asyncio.run(GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_auto_reply(enable=False))
    body = _vacation_call(service).updateVacation.call_args.kwargs["body"]
    assert body == {
        "enableAutoReply": False,
        "responseBodyPlainText": "away",
        "restrictToContacts": True,
        "startTime": "1788998400000",
    }


def test_google_turn_on_always_on_drops_a_stale_window(tmp_path: Path) -> None:
    service = MagicMock()
    _vacation_call(service).getVacation.return_value.execute.return_value = {
        "enableAutoReply": True,
        "startTime": "1788998400000",
        "endTime": "1789000000000",
    }
    _vacation_call(service).updateVacation.return_value.execute.return_value = {
        "enableAutoReply": True,
        "responseBodyPlainText": "back later",
    }
    with _google_patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_auto_reply(
                enable=True, message="back later"
            )
        )
    body = _vacation_call(service).updateVacation.call_args.kwargs["body"]
    assert "startTime" not in body and "endTime" not in body


def test_google_rejects_external_message(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Microsoft-only"):
        asyncio.run(
            google_mail_writes.mail_auto_reply(
                enable=True,
                message="x",
                external_message="y",
                config=_google_cfg(tmp_path),
            )
        )


def test_google_missing_scope_fails_closed(tmp_path: Path) -> None:
    with patch(
        "blumkin.providers.google.mail_writes.get_credentials",
        side_effect=MissingScopeError(
            "needs gmail.settings.basic", missing=frozenset(), current=frozenset()
        ),
    ):
        with pytest.raises(MissingScopeError):
            asyncio.run(google_mail_writes.mail_auto_reply(config=_google_cfg(tmp_path)))


# --------------------------------------------------------------------------- formatter


def test_format_auto_reply_human_off() -> None:
    assert format_auto_reply_human({"auto_reply": {"enabled": False}}) == ["auto-reply: off"]


def test_format_auto_reply_human_scheduled() -> None:
    lines = format_auto_reply_human(
        {
            "auto_reply": {
                "enabled": True,
                "scope": "scheduled",
                "start": "2026-09-10T00:00:00",
                "end": "2026-09-15T00:00:00",
                "external_audience": "contacts",
                "internal_message": "internal body\nsecond line",
                "external_message": "external body",
            }
        }
    )
    assert lines[0] == "auto-reply: scheduled"
    assert any("2026-09-10" in line and "2026-09-15" in line for line in lines)
    assert any(line.strip() == "internal: internal body" for line in lines)
    assert any(line.strip() == "external: external body" for line in lines)


# --------------------------------------------------------------------------- CLI


def _wo1162425_on(monkeypatch) -> None:
    monkeypatch.setattr(
        "blumkin.cli.load_config",
        lambda *, profile=None: replace(load_config(profile=profile), wo1162425_scopes=True),
    )


def test_cli_help_lists_the_flags() -> None:
    out = CliRunner().invoke(main, ["mail", "auto-reply", "--help"]).output
    assert "--on / --off" in out
    assert "--message-file" in out
    assert "--external" in out
    # The alias is registered.
    assert CliRunner().invoke(main, ["mail", "oof", "--help"]).exit_code == 0


def test_cli_change_without_yes_exits_usage(monkeypatch) -> None:
    _wo1162425_on(monkeypatch)
    result = CliRunner().invoke(main, ["mail", "auto-reply", "--off", "--json"])
    assert result.exit_code == EXIT_USAGE


def test_cli_change_flags_without_on_off_is_usage(monkeypatch) -> None:
    # A composed "turn on" command that forgot --on must not silently do a read.
    called = False

    async def _read(**_kwargs):
        nonlocal called
        called = True
        return {"auto_reply": {"enabled": False}}

    monkeypatch.setattr("blumkin.cli._workspace", lambda: SimpleNamespace(mail_auto_reply=_read))
    result = CliRunner().invoke(
        main, ["mail", "auto-reply", "--message", "OOO until 9/20", "--yes", "--json"]
    )
    assert result.exit_code == EXIT_USAGE
    assert called is False


def test_cli_read_path_needs_no_yes(monkeypatch) -> None:
    async def _read(**_kwargs):
        return {"auto_reply": {"enabled": False}}

    monkeypatch.setattr("blumkin.cli._workspace", lambda: SimpleNamespace(mail_auto_reply=_read))
    result = CliRunner().invoke(main, ["mail", "auto-reply", "--json"])
    assert result.exit_code == 0
    assert '"enabled": false' in result.output


def test_cli_missing_scope_routes(monkeypatch) -> None:
    _wo1162425_on(monkeypatch)

    async def _raise(**_kwargs):
        raise MissingScopeError(
            "needs gmail.settings.basic", missing=frozenset(), current=frozenset()
        )

    monkeypatch.setattr("blumkin.cli._workspace", lambda: SimpleNamespace(mail_auto_reply=_raise))
    result = CliRunner().invoke(
        main, ["mail", "auto-reply", "--on", "--message", "x", "--yes", "--json"]
    )
    assert result.exit_code == EXIT_MISSING_SCOPE
