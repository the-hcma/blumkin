"""CLI usage / exit-code mapping (no live Graph)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner
from kiota_abstractions.api_error import APIError

from blumkin.auth import AuthTransientError, MissingScopeError
from blumkin.cli import main
from blumkin.config import load_config
from blumkin.exit_codes import (
    EXIT_AUTH,
    EXIT_MISSING_SCOPE,
    EXIT_NOT_FOUND,
    EXIT_OTHER,
    EXIT_SUCCESS,
    EXIT_USAGE,
)
from blumkin.skills.chat import (
    ChatAttachmentScopeError,
    ChatAttachmentSkippedError,
    ChatMessageNotFoundError,
)
from blumkin.skills.mail import (
    MailAttachError,
    MailAttachmentNotFoundError,
    MailAttachmentSkippedError,
    MailBodyFileError,
    MailDraftNotFoundError,
    MailFolderNotFoundError,
    MailMessageNotFoundError,
)


def _patch_wo1162425_enabled(monkeypatch) -> None:
    def _load(*, profile: str | None = None):
        return replace(load_config(profile=profile), wo1162425_scopes=True)

    monkeypatch.setattr("blumkin.cli.load_config", _load)


def test_auth_status_google_provider_ok(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'client_id = "fake-google-desktop-client.apps.googleusercontent.com"\n'
        'provider = "google"\n'
        'default_tz = "UTC"\n'
    )
    runner = CliRunner()
    result = runner.invoke(main, ["auth", "status", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert '"provider": "google"' in (result.output or "")
    assert "Traceback" not in (result.output or "") + (result.stderr or "")


def test_profiles_list_json(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_PROFILE", raising=False)
    (tmp_path / "config.toml").write_text(
        'default_profile = "work"\n'
        "\n"
        "[profiles.personal]\n"
        'provider = "google"\n'
        'client_id = "g"\n'
        'tags = ["@personal", "google"]\n'
        "\n"
        "[profiles.work]\n"
        'provider = "microsoft"\n'
        'client_id = "m"\n'
        'tags = ["@work", "microsoft"]\n'
    )
    runner = CliRunner()
    result = runner.invoke(main, ["profiles", "list", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert '"count": 2' in (result.output or "")
    assert '"default_profile": "work"' in (result.output or "")
    assert '"name": "work"' in (result.output or "")
    assert '"name": "personal"' in (result.output or "")


def test_profiles_list_empty_config_dir_reports_count_zero(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_PROFILE", raising=False)
    runner = CliRunner()
    json_result = runner.invoke(main, ["profiles", "list", "--json"])
    assert json_result.exit_code == EXIT_SUCCESS
    assert '"count": 0' in (json_result.output or "")
    human = runner.invoke(main, ["profiles", "list"])
    assert human.exit_code == EXIT_SUCCESS
    assert "(no profiles)" in (human.output or "")


def test_root_profile_flag_selects_tag(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BLUMKIN_PROFILE", "work")
    (tmp_path / "config.toml").write_text(
        'default_profile = "work"\n'
        "\n"
        "[profiles.personal]\n"
        'provider = "google"\n'
        'client_id = "fake-google-desktop-client.apps.googleusercontent.com"\n'
        'default_tz = "UTC"\n'
        'tags = ["@personal", "google"]\n'
        "\n"
        "[profiles.work]\n"
        'provider = "microsoft"\n'
        'client_id = "00000000-0000-0000-0000-000000000001"\n'
        'tenant_id = "example.onmicrosoft.com"\n'
        'tags = ["@work"]\n'
    )
    runner = CliRunner()
    result = runner.invoke(main, ["--profile", "@personal", "auth", "status", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert '"provider": "google"' in (result.output or "")


def test_calendar_accept_invalid_tz_exits_usage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'client_id = "00000000-0000-0000-0000-000000000001"\n'
        'tenant_id = "example.onmicrosoft.com"\n'
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["calendar", "accept", "--today-pending", "--yes", "--tz", "Not/ARealZone"],
    )
    assert result.exit_code == EXIT_USAGE


def test_calendar_accept_without_yes_exits_usage() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["calendar", "accept", "--event-id", "evt-1"])
    assert result.exit_code == EXIT_USAGE


def test_calendar_cancel_without_yes_exits_usage() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["calendar", "cancel", "--event-id", "evt-1"])
    assert result.exit_code == EXIT_USAGE


def test_calendar_create_without_yes_exits_usage() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "calendar",
            "create",
            "--subject",
            "x",
            "--with",
            "a@b.com",
            "--start",
            "2026-08-26T11:00",
        ],
    )
    assert result.exit_code == EXIT_USAGE


def test_mail_send_draft_without_yes_exits_usage() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "send-draft", "--id", "draft-1"])
    assert result.exit_code == EXIT_USAGE


def test_mail_attachments_list_missing_id_exits_usage() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "attachments"])
    assert result.exit_code == EXIT_USAGE


def test_mail_attachments_list_emits_json(monkeypatch) -> None:
    async def _ok(**kwargs):
        return {
            "attachments": [{"id": "att-1", "name": "a.docx"}],
            "message_id": kwargs["message_id"],
        }

    monkeypatch.setattr("blumkin.providers.microsoft.mail_attachments_list", _ok)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "attachments", "--id", "msg-1", "--json"])
    assert result.exit_code == 0
    assert '"message_id"' in (result.output or "")


def test_mail_attachments_download_missing_selector_exits_usage() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["mail", "attachments", "download", "--message-id", "msg-1", "--out", "out.bin"],
    )
    assert result.exit_code == EXIT_USAGE


def test_mail_attachments_download_wires_options_and_emits_json(tmp_path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def _ok(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {"message_id": kwargs["message_id"], "saved": [{"name": "a.docx"}], "skipped": []}

    monkeypatch.setattr("blumkin.providers.microsoft.mail_attachments_download", _ok)
    runner = CliRunner()
    out = tmp_path / "saved.docx"
    result = runner.invoke(
        main,
        [
            "mail",
            "attachments",
            "download",
            "--message-id",
            "msg-9",
            "--attachment-id",
            "att-1",
            "--out",
            str(out),
            "--json",
        ],
    )
    assert result.exit_code == 0
    assert seen["message_id"] == "msg-9"
    assert seen["attachment_id"] == "att-1"
    assert seen["out"] == str(out)
    assert seen["download_all"] is False
    assert '"saved"' in (result.output or "")


def test_chat_send_without_yes_exits_usage(monkeypatch) -> None:
    _patch_wo1162425_enabled(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, ["chat", "send", "--with", "Ada", "--text", "hi"])
    assert result.exit_code == EXIT_USAGE


def test_chat_send_ambiguous_exits_usage(monkeypatch) -> None:
    _patch_wo1162425_enabled(monkeypatch)

    async def _boom(**_kwargs):
        raise ValueError("ambiguous chat match for 'dan' (2 chats); pass --chat-id")

    monkeypatch.setattr("blumkin.providers.microsoft.chat_send", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["chat", "send", "--with", "dan", "--text", "hi", "--yes", "--json"],
    )
    assert result.exit_code == EXIT_USAGE
    assert "usage_error" in (result.output or "")


def test_chat_edit_without_yes_exits_usage(monkeypatch) -> None:
    _patch_wo1162425_enabled(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["chat", "edit", "--chat-id", "c1", "--message-id", "m1", "--text", "x"],
    )
    assert result.exit_code == EXIT_USAGE


def test_chat_delete_without_yes_exits_usage(monkeypatch) -> None:
    _patch_wo1162425_enabled(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, ["chat", "delete", "--chat-id", "c1", "--message-id", "m1"])
    assert result.exit_code == EXIT_USAGE


def test_meeting_transcription_enable_without_yes_exits_usage(monkeypatch) -> None:
    _patch_wo1162425_enabled(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, ["meeting", "transcription", "--event-id", "evt-1", "--enable"])
    assert result.exit_code == EXIT_USAGE


def test_wo1162425_scopes_disabled_allows_calendar_create_teams(
    tmp_path: Path, monkeypatch
) -> None:
    """Teams-on-event uses Calendars.ReadWrite only; WO gate must not block create."""
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_WO1162425_SCOPES", raising=False)
    (tmp_path / "config.toml").write_text('client_id = "abc"\n')

    async def _create(**kwargs):
        assert kwargs.get("teams") is True
        return {
            "event": {
                "id": "evt-1",
                "subject": "Sync",
                "start": "2026-08-27T10:00:00",
                "end": "2026-08-27T10:30:00",
                "online_join_url": "https://teams.example/join",
            }
        }

    monkeypatch.setattr("blumkin.providers.microsoft.calendar_create", _create)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "calendar",
            "create",
            "--subject",
            "Sync",
            "--with",
            "ada@example.com",
            "--start",
            "2026-08-27T10:00",
            "--yes",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert "evt-1" in (result.stdout or "")


def test_wo1162425_scopes_disabled_blocks_chat_send(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_WO1162425_SCOPES", raising=False)
    (tmp_path / "config.toml").write_text('client_id = "abc"\n')
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["chat", "send", "--with", "Ada", "--text", "hi", "--yes", "--json"],
    )
    assert result.exit_code == EXIT_USAGE
    assert "WO1162425 add-on scopes are disabled" in (result.output or "")


def test_wo1162425_scopes_disabled_blocks_people_resolve(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_WO1162425_SCOPES", raising=False)
    (tmp_path / "config.toml").write_text('client_id = "abc"\n')
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["people", "resolve", "--name", "Ada", "--json"],
    )
    assert result.exit_code == EXIT_USAGE
    assert "WO1162425 add-on scopes are disabled" in (result.output or "")
    assert "people resolve" in (result.output or "")


def test_chat_attachments_download_missing_scope_exits_missing_scope(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise ChatAttachmentScopeError("needs Files.Read — open https://example.invalid/f.docx")

    monkeypatch.setattr("blumkin.providers.microsoft.chat_attachments_download", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "chat",
            "attachments",
            "download",
            "--chat-id",
            "chat-1",
            "--message-id",
            "msg-1",
            "--attachment-id",
            "att-1",
            "--out",
            "out.docx",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_MISSING_SCOPE
    assert "missing_scope" in (result.output or "")


def test_chat_attachments_download_skipped_exits_usage(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise ChatAttachmentSkippedError("adaptive card attachment carries no file content")

    monkeypatch.setattr("blumkin.providers.microsoft.chat_attachments_download", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "chat",
            "attachments",
            "download",
            "--chat-id",
            "chat-1",
            "--message-id",
            "msg-1",
            "--attachment-id",
            "att-card",
            "--out",
            "out.json",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_USAGE
    assert "usage_error" in (result.output or "")


def test_chat_attachments_download_wires_options_and_emits_json(tmp_path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def _fake(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {
            "chat": None,
            "chat_id": "chat-1",
            "message_id": "msg-1",
            "saved": [],
            "skipped": [],
        }

    monkeypatch.setattr("blumkin.providers.microsoft.chat_attachments_download", _fake)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "chat",
            "attachments",
            "download",
            "--with",
            "Ada",
            "--latest",
            "--all",
            "--out",
            str(tmp_path / "downloads"),
            "--json",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert seen["download_all"] is True
    assert seen["latest"] is True
    assert seen["with_name"] == "Ada"
    assert seen["out"] == str(tmp_path / "downloads")


def test_chat_attachments_list_message_not_found_exits_not_found(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise ChatMessageNotFoundError("chat message not found: missing")

    monkeypatch.setattr("blumkin.providers.microsoft.chat_attachments_list", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["chat", "attachments", "--chat-id", "chat-1", "--message-id", "missing", "--json"],
    )
    assert result.exit_code == EXIT_NOT_FOUND
    assert "not_found" in (result.output or "")


def test_chat_attachments_list_missing_message_selector_exits_usage() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["chat", "attachments", "--chat-id", "chat-1", "--json"])
    assert result.exit_code == EXIT_USAGE
    assert "usage_error" in (result.output or "")


def test_chat_send_wires_options_and_emits_json(monkeypatch) -> None:
    _patch_wo1162425_enabled(monkeypatch)
    seen: dict[str, object] = {}

    async def _ok(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {
            "chat": {"id": "chat-1", "topic": "T"},
            "message": {"id": "msg-1", "body_text": kwargs["text"]},
            "partial": False,
            "query": kwargs["with_name"],
            "skipped": 0,
        }

    monkeypatch.setattr("blumkin.providers.microsoft.chat_send", _ok)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["chat", "send", "--with", "Ada", "--text", "hello", "--yes", "--json"],
    )
    assert result.exit_code == 0
    assert seen["with_name"] == "Ada"
    assert seen["text"] == "hello"
    assert '"id": "msg-1"' in (result.output or "") or '"id":"msg-1"' in (result.output or "")


def test_meeting_get_not_found_exits(monkeypatch) -> None:
    _patch_wo1162425_enabled(monkeypatch)

    async def _boom(**_kwargs):
        raise LookupError("event is not a Teams online meeting: evt-1")

    monkeypatch.setattr("blumkin.providers.microsoft.meeting_get", _boom)
    runner = CliRunner()
    result = runner.invoke(main, ["meeting", "get", "--event-id", "evt-1", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert "not_found" in (result.output or "")


def test_mail_attachments_download_all_existing_file_out_exits_usage(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise ValueError("--out must be a directory when using --all")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_attachments_download", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mail",
            "attachments",
            "download",
            "--message-id",
            "msg-1",
            "--all",
            "--out",
            "report.docx",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_USAGE
    assert "usage_error" in (result.output or "")


def test_mail_attachments_download_attachment_not_found_exits_not_found(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise MailAttachmentNotFoundError("attachment not found: missing")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_attachments_download", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mail",
            "attachments",
            "download",
            "--message-id",
            "msg-1",
            "--attachment-id",
            "missing",
            "--out",
            "out.bin",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_NOT_FOUND
    assert "not_found" in (result.output or "")


def test_mail_attachments_download_skipped_exits_usage(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise MailAttachmentSkippedError("#microsoft.graph.itemAttachment not supported in v1")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_attachments_download", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mail",
            "attachments",
            "download",
            "--message-id",
            "msg-1",
            "--attachment-id",
            "att-inline",
            "--out",
            "out.bin",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_USAGE
    assert "usage_error" in (result.output or "")


def test_mail_attachments_download_not_found_exits_not_found(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise MailMessageNotFoundError("message not found: missing")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_attachments_download", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mail",
            "attachments",
            "download",
            "--message-id",
            "missing",
            "--attachment-id",
            "att-1",
            "--out",
            "out.bin",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_NOT_FOUND
    assert "not_found" in (result.output or "")


def test_mail_attachments_list_not_found_exits_not_found(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise MailMessageNotFoundError("message not found: missing")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_attachments_list", _boom)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "attachments", "--id", "missing", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert "not_found" in (result.output or "")


def test_mail_folders_emits_json(monkeypatch) -> None:
    async def _folders(**_kwargs):
        return {"folders": [{"id": "inbox-id", "name": "Inbox", "path": "Inbox", "total": 3}]}

    monkeypatch.setattr("blumkin.providers.microsoft.mail_folders", _folders)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "folders", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert '"inbox-id"' in (result.output or "")


def test_mail_get_message_not_found_exits_not_found(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise MailMessageNotFoundError("message not found: 'nope'")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_get", _boom)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "get", "--id", "nope", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert "not_found" in (result.output or "")


def test_mail_get_rejects_an_unknown_body_type() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "get", "--id", "msg-1", "--body-type", "markdown"])
    assert result.exit_code == EXIT_USAGE


def test_mail_get_wires_options_and_emits_json(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def _get(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {"message": {"id": "msg-1", "subject": "Quarterly sync"}}

    monkeypatch.setattr("blumkin.providers.microsoft.mail_get", _get)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "get", "--id", "msg-1", "--body-type", "html", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert seen == {"body_type": "html", "message_id": "msg-1"}
    assert '"Quarterly sync"' in (result.output or "")


def test_mail_list_folder_not_found_exits_not_found(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise MailFolderNotFoundError("mail folder not found: 'nope'")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_list", _boom)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "list", "--folder", "nope", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert "not_found" in (result.output or "")


def test_mail_list_rejects_an_unknown_orderby() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "list", "--orderby", "alphabetical"])
    assert result.exit_code == EXIT_USAGE


def test_mail_list_wires_options_and_emits_json(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def _list(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {"folder": "sentitems", "items": [], "orderby": "sent", "top": 5}

    monkeypatch.setattr("blumkin.providers.microsoft.mail_list", _list)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["mail", "list", "--folder", "sent", "--orderby", "sent", "--top", "5", "--json"],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert seen == {
        "folder": "sent",
        "has_attachments": False,
        "importance": None,
        "orderby": "sent",
        "search": None,
        "sender": None,
        "since": None,
        "subject": None,
        "top": 5,
        "unread": False,
        "until": None,
    }
    assert '"sentitems"' in (result.output or "")


def test_mail_list_wires_filters_and_parses_dates(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def _list(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {"filters": {}, "folder": None, "items": [], "orderby": "received", "top": 10}

    monkeypatch.setattr("blumkin.providers.microsoft.mail_list", _list)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mail",
            "list",
            "--from",
            "Rebecca",
            "--subject",
            "budget",
            "--since",
            "2026-08-01",
            "--until",
            "2026-08-08",
            "--unread",
            "--tz",
            "UTC",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert seen["sender"] == "Rebecca"
    assert seen["subject"] == "budget"
    assert seen["unread"] is True
    assert seen["since"] == datetime(2026, 8, 1, tzinfo=ZoneInfo("UTC"))
    assert seen["until"] == datetime(2026, 8, 8, tzinfo=ZoneInfo("UTC"))


@pytest.mark.parametrize("command", ["inbox", "list"])
def test_mail_listing_wires_importance_and_attachment_flags(monkeypatch, command: str) -> None:
    """Default-only coverage would miss a dropped flag in the Click wiring or the re-pack."""
    seen: dict[str, object] = {}

    async def _capture(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {"filters": {}, "folder": None, "items": [], "orderby": "received", "top": 10}

    monkeypatch.setattr(f"blumkin.providers.microsoft.mail_{command}", _capture)
    result = CliRunner().invoke(
        main, ["mail", command, "--importance", "high", "--has-attachments", "--json"]
    )
    assert result.exit_code == EXIT_SUCCESS
    assert seen["importance"] == "high"
    assert seen["has_attachments"] is True


def test_mail_list_rejects_an_unknown_timezone(monkeypatch) -> None:
    """A flag typo is a usage error; blaming Graph would send the operator elsewhere."""

    async def _list(**_kwargs):
        raise AssertionError("should not reach Graph")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_list", _list)
    runner = CliRunner()
    result = runner.invoke(
        main, ["mail", "list", "--since", "2026-08-01", "--tz", "Not/AZone", "--json"]
    )
    assert result.exit_code == EXIT_USAGE
    assert "usage_error" in (result.output or "")


def test_mail_list_without_bounds_ignores_the_timezone(monkeypatch) -> None:
    """A plain listing has no date to localize, so it must not fail over --tz."""
    seen: dict[str, object] = {}

    async def _list(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {"filters": {}, "folder": None, "items": [], "orderby": "received", "top": 10}

    monkeypatch.setattr("blumkin.providers.microsoft.mail_list", _list)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "list", "--tz", "Not/AZone", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert seen["since"] is None


def test_mail_list_search_conflict_exits_usage(monkeypatch) -> None:
    async def _list(**_kwargs):
        raise ValueError("--search cannot be combined with --from")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_list", _list)
    runner = CliRunner()
    result = runner.invoke(
        main, ["mail", "list", "--search", "budget", "--from", "Rebecca", "--json"]
    )
    assert result.exit_code == EXIT_USAGE
    assert "usage_error" in (result.output or "")


def test_mail_list_search_since_conflict_exits_usage(monkeypatch) -> None:
    async def _list(**_kwargs):
        raise ValueError("--search cannot be combined with --since")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_list", _list)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["mail", "list", "--search", "budget", "--since", "2026-08-01", "--json"],
    )
    assert result.exit_code == EXIT_USAGE
    assert "usage_error" in (result.output or "")


def test_mail_inbox_wires_filters(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def _inbox(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {"filters": {}, "items": [], "orderby": None, "top": 10}

    monkeypatch.setattr("blumkin.providers.microsoft.mail_inbox", _inbox)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "inbox", "--search", "budget", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert seen["search"] == "budget"
    assert seen["sender"] is None


def test_mail_reply_needs_no_yes_because_it_only_drafts(monkeypatch) -> None:
    """The human checkpoint stays at send-draft, as it does for mail draft."""
    seen: dict[str, object] = {}

    async def _reply(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {"draft": {"id": "draft-1", "kind": "reply", "source_message_id": "msg-1", "to": []}}

    monkeypatch.setattr("blumkin.providers.microsoft.mail_reply", _reply)
    runner = CliRunner()
    result = runner.invoke(
        main, ["mail", "reply", "--id", "msg-1", "--body", "Thanks", "--all", "--json"]
    )
    assert result.exit_code == EXIT_SUCCESS
    assert seen == {
        "bcc": None,
        "body": "Thanks",
        "body_file": None,
        "body_type": "text",
        "cc": None,
        "message_id": "msg-1",
        "no_signature": False,
        "reply_all": True,
    }


def test_mail_compose_commands_wire_no_signature_flag(monkeypatch) -> None:
    """Click must forward --no-signature; default-False coverage alone would miss a dropped flag."""
    seen: dict[str, object] = {}

    async def _capture(**kwargs):
        seen.clear()
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {
            "draft": {
                "id": "draft-1",
                "kind": "reply",
                "source_message_id": "msg-1",
                "subject": "x",
                "to": "a@b.com",
            }
        }

    monkeypatch.setattr("blumkin.providers.microsoft.mail_reply", _capture)
    monkeypatch.setattr("blumkin.providers.microsoft.mail_forward", _capture)
    monkeypatch.setattr("blumkin.providers.microsoft.mail_draft", _capture)
    runner = CliRunner()

    reply = runner.invoke(
        main, ["mail", "reply", "--id", "msg-1", "--body", "Thanks", "--no-signature", "--json"]
    )
    assert reply.exit_code == EXIT_SUCCESS
    assert seen["no_signature"] is True

    forward = runner.invoke(
        main,
        [
            "mail",
            "forward",
            "--id",
            "msg-1",
            "--to",
            "sam@example.com",
            "--no-signature",
            "--json",
        ],
    )
    assert forward.exit_code == EXIT_SUCCESS
    assert seen["no_signature"] is True

    draft = runner.invoke(
        main,
        [
            "mail",
            "draft",
            "--to",
            "a@b.com",
            "--subject",
            "Hi",
            "--body",
            "Hello",
            "--no-signature",
            "--json",
        ],
    )
    assert draft.exit_code == EXIT_SUCCESS
    assert seen["no_signature"] is True


def test_mail_reply_message_not_found_exits_not_found(monkeypatch) -> None:
    async def _reply(**_kwargs):
        raise MailMessageNotFoundError("message not found: 'nope'")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_reply", _reply)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "reply", "--id", "nope", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert "not_found" in (result.output or "")


def test_mail_reply_passes_body_file_and_body_type_through(tmp_path, monkeypatch) -> None:
    """Dropping body_file would silently create an empty-comment draft."""
    seen: dict[str, object] = {}
    path = tmp_path / "notes.html"
    path.write_text("<p>Thanks</p>", encoding="utf-8")

    async def _reply(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {
            "draft": {
                "id": "draft-1",
                "kind": "reply",
                "source_message_id": "msg-1",
                "to": "a@b.com",
            }
        }

    monkeypatch.setattr("blumkin.providers.microsoft.mail_reply", _reply)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mail",
            "reply",
            "--id",
            "msg-1",
            "--body-file",
            str(path),
            "--body-type",
            "html",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert seen["body"] is None
    assert seen["body_file"] == str(path)
    assert seen["body_type"] == "html"


def test_mail_forward_wires_options(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def _forward(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {
            "draft": {
                "id": "draft-1",
                "kind": "forward",
                "source_message_id": "msg-1",
                "to": "sam@example.com",
            }
        }

    monkeypatch.setattr("blumkin.providers.microsoft.mail_forward", _forward)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mail",
            "forward",
            "--id",
            "msg-1",
            "--to",
            "sam@example.com",
            "--cc",
            "cc@example.com",
            "--bcc",
            "bcc@example.com",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert seen["to"] == "sam@example.com"
    assert seen["body"] is None
    assert seen["cc"] == ("cc@example.com",)
    assert seen["bcc"] == ("bcc@example.com",)


def test_mail_reply_wires_cc_and_bcc(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def _reply(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {
            "draft": {
                "id": "draft-1",
                "kind": "reply",
                "source_message_id": "msg-1",
                "to": "rebecca@example.com",
            }
        }

    monkeypatch.setattr("blumkin.providers.microsoft.mail_reply", _reply)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mail",
            "reply",
            "--id",
            "msg-1",
            "--body",
            "Thanks",
            "--cc",
            "cc@example.com",
            "--bcc",
            "bcc@example.com",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert seen["body"] == "Thanks"
    assert seen["cc"] == ("cc@example.com",)
    assert seen["bcc"] == ("bcc@example.com",)


def test_mail_forward_message_not_found_exits_not_found(monkeypatch) -> None:
    async def _forward(**_kwargs):
        raise MailMessageNotFoundError("message not found: 'nope'")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_forward", _forward)
    runner = CliRunner()
    result = runner.invoke(
        main, ["mail", "forward", "--id", "nope", "--to", "sam@example.com", "--json"]
    )
    assert result.exit_code == EXIT_NOT_FOUND
    assert "not_found" in (result.output or "")


def test_mail_forward_body_file_error_exits_usage(tmp_path, monkeypatch) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("x", encoding="utf-8")

    async def _boom(**_kwargs):
        raise MailBodyFileError(f"cannot read --body-file {path}: boom")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_forward", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mail",
            "forward",
            "--id",
            "msg-1",
            "--to",
            "sam@example.com",
            "--body-file",
            str(path),
            "--json",
        ],
    )
    assert result.exit_code == EXIT_USAGE
    assert "usage_error" in (result.output or "")


def test_mail_delete_draft_without_yes_succeeds(monkeypatch) -> None:
    async def _delete(*, draft_id: str, config=None):
        return {"deleted": draft_id}

    monkeypatch.setattr("blumkin.providers.microsoft.mail_delete_draft", _delete)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "delete-draft", "--id", "draft-1", "--json"])
    assert result.exit_code == 0
    assert '"deleted"' in (result.output or "")
    assert "draft-1" in (result.output or "")


def test_mail_delete_draft_not_draft_exits_not_found(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise MailDraftNotFoundError("message is not a draft: msg-1")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_delete_draft", _boom)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "delete-draft", "--id", "msg-1", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert "not_found" in (result.output or "")


def test_graph_404_via_api_error_exits_not_found(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise APIError("gone", response_status_code=404)

    monkeypatch.setattr("blumkin.providers.microsoft.mail_delete_draft", _boom)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "delete-draft", "--id", "gone", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert "not_found" in (result.output or "")


def test_mail_draft_body_file_oserror_exits_usage_not_auth(tmp_path, monkeypatch) -> None:
    path = tmp_path / "client_id.txt"
    path.write_text("x", encoding="utf-8")

    async def _boom(**_kwargs):
        from blumkin.skills.mail import MailBodyFileError

        raise MailBodyFileError(f"cannot read --body-file {path}: Permission denied")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_draft", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mail",
            "draft",
            "--to",
            "a@b.com",
            "--subject",
            "x",
            "--body-file",
            str(path),
            "--json",
        ],
    )
    assert result.exit_code == EXIT_USAGE
    assert result.exit_code != EXIT_AUTH
    assert "auth_required" not in (result.output or "")


def test_mail_draft_both_body_sources_exits_usage(tmp_path) -> None:
    path = tmp_path / "body.txt"
    path.write_text("from file", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mail",
            "draft",
            "--to",
            "a@b.com",
            "--subject",
            "x",
            "--body",
            "inline",
            "--body-file",
            str(path),
        ],
    )
    assert result.exit_code == EXIT_USAGE


def test_mail_draft_missing_body_exits_usage() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["mail", "draft", "--to", "a@b.com", "--subject", "x"],
    )
    assert result.exit_code == EXIT_USAGE


def test_mail_draft_passes_repeated_attachments_through(tmp_path, monkeypatch) -> None:
    seen: dict[str, object] = {}
    first = tmp_path / "a.txt"
    first.write_text("a", encoding="utf-8")
    second = tmp_path / "b.txt"
    second.write_text("b", encoding="utf-8")

    async def _ok(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {
            "draft": {
                "attachments": [],
                "body_type": "text",
                "id": "d",
                "subject": "x",
                "to": "a@b.com",
            }
        }

    monkeypatch.setattr("blumkin.providers.microsoft.mail_draft", _ok)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mail",
            "draft",
            "--to",
            "a@b.com",
            "--subject",
            "x",
            "--body",
            "hi",
            "--attach",
            str(first),
            "--attach",
            str(second),
            "--json",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert seen["attach"] == (str(first), str(second))


def test_mail_draft_wires_repeatable_recipients(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def _ok(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {
            "draft": {
                "attachments": [],
                "bcc": "secret@example.com",
                "body_type": "text",
                "cc": "sam@example.com",
                "id": "d",
                "subject": "x",
                "to": "a@b.com, c@d.com",
            }
        }

    monkeypatch.setattr("blumkin.providers.microsoft.mail_draft", _ok)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mail",
            "draft",
            "--to",
            "a@b.com",
            "--to",
            "c@d.com",
            "--cc",
            "sam@example.com",
            "--bcc",
            "secret@example.com",
            "--subject",
            "x",
            "--body",
            "hi",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert seen["to"] == ("a@b.com", "c@d.com")
    assert seen["cc"] == ("sam@example.com",)
    assert seen["bcc"] == ("secret@example.com",)


def test_mail_update_draft_accepts_attach_without_other_fields(tmp_path, monkeypatch) -> None:
    seen: dict[str, object] = {}
    source = tmp_path / "a.txt"
    source.write_text("a", encoding="utf-8")

    async def _ok(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {
            "draft": {
                "attachments": [],
                "body_type": "text",
                "id": "d",
                "subject": "x",
                "to": "a@b.com",
            }
        }

    monkeypatch.setattr("blumkin.providers.microsoft.mail_update_draft", _ok)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["mail", "update-draft", "--id", "d", "--attach", str(source), "--json"],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert seen["attach"] == (str(source),)


def test_mail_draft_missing_attachment_exits_usage(tmp_path, monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise MailAttachError(f"--attach file not found: {tmp_path / 'Missing' / 'notes.txt'}")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_draft", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mail",
            "draft",
            "--to",
            "a@b.com",
            "--subject",
            "x",
            "--body",
            "hi",
            "--attach",
            str(tmp_path / "Missing" / "notes.txt"),
            "--json",
        ],
    )
    assert result.exit_code == EXIT_USAGE
    assert "usage_error" in (result.output or "")
    assert "auth_required" not in (result.output or "")


def test_mail_update_draft_not_a_draft_exits_not_found(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise MailDraftNotFoundError("message is not a draft: msg-1")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_update_draft", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["mail", "update-draft", "--id", "msg-1", "--subject", "x", "--json"],
    )
    assert result.exit_code == EXIT_NOT_FOUND
    assert "not_found" in (result.output or "")


def test_mail_update_draft_missing_fields_exits_usage(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise ValueError("provide at least one of --subject, --body/--body-file, or --to")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_update_draft", _boom)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "update-draft", "--id", "draft-1"])
    assert result.exit_code == EXIT_USAGE


def test_mail_update_draft_body_file_error_exits_usage(tmp_path, monkeypatch) -> None:
    path = tmp_path / "body.txt"
    path.write_text("x", encoding="utf-8")

    async def _boom(**_kwargs):
        raise MailBodyFileError(f"cannot read --body-file {path}: boom")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_update_draft", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["mail", "update-draft", "--id", "draft-1", "--body-file", str(path), "--json"],
    )
    assert result.exit_code == EXIT_USAGE
    assert "usage_error" in (result.output or "")


def test_mail_update_draft_wires_options_and_emits_json(tmp_path, monkeypatch) -> None:
    seen: dict[str, object] = {}
    path = tmp_path / "upd.html"
    path.write_text("<p>Hi</p>", encoding="utf-8")

    async def _ok(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {
            "draft": {
                "body_type": kwargs.get("body_type") or "text",
                "id": kwargs["draft_id"],
                "subject": kwargs.get("subject") or "kept",
                "to": kwargs.get("to") or "a@b.com",
            }
        }

    monkeypatch.setattr("blumkin.providers.microsoft.mail_update_draft", _ok)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "mail",
            "update-draft",
            "--id",
            "draft-9",
            "--subject",
            "Subj",
            "--to",
            "b@c.com",
            "--body-file",
            str(path),
            "--body-type",
            "html",
            "--json",
        ],
    )
    assert result.exit_code == 0
    assert seen["draft_id"] == "draft-9"
    assert seen["subject"] == "Subj"
    assert seen["to"] == ("b@c.com",)
    assert seen["cc"] is None
    assert seen["bcc"] is None
    assert seen["body"] is None
    assert seen["body_file"] == str(path)
    assert seen["body_type"] == "html"
    assert '"id": "draft-9"' in (result.output or "") or '"id":"draft-9"' in (result.output or "")


def test_mail_update_draft_wires_partial_cc_omits_to(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def _ok(**kwargs):
        seen.update({k: v for k, v in kwargs.items() if k != "config"})
        return {
            "draft": {
                "body_type": "text",
                "cc": "c@d.com",
                "id": kwargs["draft_id"],
                "subject": "kept",
                "to": "a@b.com",
            }
        }

    monkeypatch.setattr("blumkin.providers.microsoft.mail_update_draft", _ok)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["mail", "update-draft", "--id", "draft-9", "--cc", "c@d.com", "--json"],
    )
    assert result.exit_code == 0
    assert seen["to"] is None
    assert seen["cc"] == ("c@d.com",)
    assert seen["bcc"] is None


def test_mail_update_draft_runtime_error_exits_other(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise RuntimeError("Graph returned no message after update-draft: draft-1")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_update_draft", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["mail", "update-draft", "--id", "draft-1", "--subject", "x", "--json"],
    )
    assert result.exit_code == EXIT_OTHER
    assert "graph_error" in (result.output or "")


def test_calendar_suggest_freebusy_failure_exits_graph_error(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise ValueError("freebusy lookup failed for: typo@example.com: Mail tip unavailable")

    monkeypatch.setattr("blumkin.providers.microsoft.calendar_suggest", _boom)
    result = CliRunner().invoke(
        main,
        [
            "calendar",
            "suggest",
            "--with",
            "typo@example.com",
            "--start",
            "2026-08-28T09:00",
            "--end",
            "2026-08-28T17:00",
            "--tz",
            "UTC",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_OTHER
    assert "graph_error" in (result.output or "")


def test_calendar_suggest_bad_window_exits_usage(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise ValueError("--window must look like HH:MM-HH:MM")

    monkeypatch.setattr("blumkin.providers.microsoft.calendar_suggest", _boom)
    result = CliRunner().invoke(
        main,
        [
            "calendar",
            "suggest",
            "--with",
            "a@example.com",
            "--start",
            "2026-08-28T09:00",
            "--end",
            "2026-08-28T17:00",
            "--window",
            "nope",
            "--tz",
            "UTC",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_USAGE
    assert "usage_error" in (result.output or "")


def test_calendar_today_auth_required_value_error_exits_auth(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise ValueError(
            "Authentication required. Run 'blumkin auth login' on a TTY "
            "(or unset BLUMKIN_NONINTERACTIVE)."
        )

    monkeypatch.setattr("blumkin.providers.microsoft.calendar_today", _boom)
    result = CliRunner().invoke(main, ["calendar", "today", "--tz", "UTC", "--json"])
    assert result.exit_code == EXIT_AUTH
    combined = (result.output or "") + (result.stderr or "")
    assert "auth_required" in combined


def test_calendar_today_auth_transient_error_exits_other(monkeypatch) -> None:
    """AuthTransientError (issue #133) is a network hiccup, not a bad grant - exit 1."""

    async def _boom(**_kwargs):
        raise AuthTransientError(
            "Microsoft token refresh hit a transient network error: timed out. Safe to retry."
        )

    monkeypatch.setattr("blumkin.providers.microsoft.calendar_today", _boom)
    result = CliRunner().invoke(main, ["calendar", "today", "--tz", "UTC", "--json"])
    assert result.exit_code == EXIT_OTHER
    combined = (result.output or "") + (result.stderr or "")
    assert "transient_error" in combined
    assert "auth_required" not in combined


def test_calendar_today_empty_default_tz_exits_usage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_TZ", raising=False)
    (tmp_path / "config.toml").write_text('client_id = "00000000-0000-0000-0000-000000000001"\n')
    result = CliRunner().invoke(main, ["calendar", "today", "--json"])
    assert result.exit_code == EXIT_USAGE
    combined = (result.output or "") + (result.stderr or "")
    assert "usage_error" in combined
    assert "auth_required" not in combined


def test_calendar_today_invalid_tz_exits_usage() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--tz", "Not/ARealZone", "calendar", "today"])
    assert result.exit_code == EXIT_USAGE


def test_calendar_today_missing_scope_error_exits_missing_scope(monkeypatch) -> None:
    """MissingScopeError (issue #133) exits 4 and surfaces the current/missing scope gap."""

    async def _boom(**_kwargs):
        raise MissingScopeError(
            "Stored Google grant is missing scopes this build needs.\n"
            "current scopes:  gmail.readonly\nmissing scopes:  calendar.events",
            current=frozenset({"https://www.googleapis.com/auth/gmail.readonly"}),
            missing=frozenset({"https://www.googleapis.com/auth/calendar.events"}),
        )

    monkeypatch.setattr("blumkin.providers.microsoft.calendar_today", _boom)
    result = CliRunner().invoke(main, ["calendar", "today", "--tz", "UTC", "--json"])
    assert result.exit_code == EXIT_MISSING_SCOPE
    combined = (result.output or "") + (result.stderr or "")
    assert "missing_scope" in combined
    assert "missing scopes:  calendar.events" in combined


def test_calendar_view_accepts_subcommand_tz() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["calendar", "view", "--from", "2026-08-25", "--to", "2026-08-26", "--tz", "Not/ARealZone"],
    )
    assert result.exit_code == EXIT_USAGE
    assert "No such option" not in (result.output or "")


def test_calendar_view_auth_required_value_error_exits_auth(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise ValueError(
            "Authentication required. Run 'blumkin auth login' on a TTY "
            "(or unset BLUMKIN_NONINTERACTIVE)."
        )

    monkeypatch.setattr("blumkin.providers.microsoft.calendar_view", _boom)
    result = CliRunner().invoke(
        main,
        ["calendar", "view", "--from", "2026-08-25", "--to", "2026-08-26", "--tz", "UTC", "--json"],
    )
    assert result.exit_code == EXIT_AUTH
    combined = (result.output or "") + (result.stderr or "")
    assert "auth_required" in combined


def test_doctor_auth_cache_incomplete_exits_auth(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'client_id = "00000000-0000-0000-0000-000000000001"\n'
        'tenant_id = "example.onmicrosoft.com"\n'
    )
    runner = CliRunner()
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == EXIT_AUTH


def test_wo1162425_gate_does_not_apply_to_a_google_profile(tmp_path, monkeypatch) -> None:
    """WO1162425 is a Microsoft Entra add-on; gating Google on it made people resolve
    unreachable no matter what the operator had consented to."""
    import json
    from unittest.mock import MagicMock, patch

    from click.testing import CliRunner

    from blumkin.cli import main

    (tmp_path / "config.toml").write_text(
        'provider = "google"\n'
        'default_tz = "UTC"\n'
        'google_oauth_client_file = "%s"\n' % (tmp_path / "client.json")
    )
    (tmp_path / "client.json").write_text(
        '{"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "s"}}'
    )
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    provider = MagicMock()

    async def _resolve(**_kwargs):
        return {"ambiguous": False, "matches": [], "person": {"email": "a@b.com"}, "query": {}}

    provider.people_resolve.side_effect = _resolve
    with patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(main, ["people", "resolve", "--name", "Ada", "--json"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["person"]["email"] == "a@b.com"
