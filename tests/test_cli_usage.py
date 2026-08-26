"""CLI usage / exit-code mapping (no live Graph)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from click.testing import CliRunner
from kiota_abstractions.api_error import APIError

from blumkin.cli import main
from blumkin.config import load_config
from blumkin.exit_codes import EXIT_AUTH, EXIT_NOT_FOUND, EXIT_OTHER, EXIT_USAGE
from blumkin.skills.mail import (
    MailAttachmentNotFoundError,
    MailAttachmentSkippedError,
    MailBodyFileError,
    MailDraftNotFoundError,
    MailMessageNotFoundError,
)


def _patch_wo1162425_enabled(monkeypatch) -> None:
    def _load():
        return replace(load_config(), wo1162425_scopes=True)

    monkeypatch.setattr("blumkin.cli.load_config", _load)


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

    monkeypatch.setattr("blumkin.cli.mail_attachments_list", _ok)
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
        seen.update(kwargs)
        return {"message_id": kwargs["message_id"], "saved": [{"name": "a.docx"}], "skipped": []}

    monkeypatch.setattr("blumkin.cli.mail_attachments_download", _ok)
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

    monkeypatch.setattr("blumkin.cli.chat_send", _boom)
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


def test_wo1162425_scopes_disabled_blocks_calendar_create_teams(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_WO1162425_SCOPES", raising=False)
    (tmp_path / "config.toml").write_text('client_id = "abc"\n')
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
            "--teams",
            "--yes",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_USAGE
    assert "WO1162425 add-on scopes are disabled" in (result.output or "")


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


def test_chat_send_wires_options_and_emits_json(monkeypatch) -> None:
    _patch_wo1162425_enabled(monkeypatch)
    seen: dict[str, object] = {}

    async def _ok(**kwargs):
        seen.update(kwargs)
        return {
            "chat": {"id": "chat-1", "topic": "T"},
            "message": {"id": "msg-1", "body_text": kwargs["text"]},
            "partial": False,
            "query": kwargs["with_name"],
            "skipped": 0,
        }

    monkeypatch.setattr("blumkin.cli.chat_send", _ok)
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

    monkeypatch.setattr("blumkin.cli.meeting_get", _boom)
    runner = CliRunner()
    result = runner.invoke(main, ["meeting", "get", "--event-id", "evt-1", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert "not_found" in (result.output or "")


def test_mail_attachments_download_all_existing_file_out_exits_usage(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise ValueError("--out must be a directory when using --all")

    monkeypatch.setattr("blumkin.cli.mail_attachments_download", _boom)
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

    monkeypatch.setattr("blumkin.cli.mail_attachments_download", _boom)
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

    monkeypatch.setattr("blumkin.cli.mail_attachments_download", _boom)
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

    monkeypatch.setattr("blumkin.cli.mail_attachments_download", _boom)
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

    monkeypatch.setattr("blumkin.cli.mail_attachments_list", _boom)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "attachments", "--id", "missing", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert "not_found" in (result.output or "")


def test_mail_delete_draft_without_yes_succeeds(monkeypatch) -> None:
    async def _delete(*, draft_id: str):
        return {"deleted": draft_id}

    monkeypatch.setattr("blumkin.cli.mail_delete_draft", _delete)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "delete-draft", "--id", "draft-1", "--json"])
    assert result.exit_code == 0
    assert '"deleted"' in (result.output or "")
    assert "draft-1" in (result.output or "")


def test_mail_delete_draft_not_draft_exits_not_found(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise MailDraftNotFoundError("message is not a draft: msg-1")

    monkeypatch.setattr("blumkin.cli.mail_delete_draft", _boom)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "delete-draft", "--id", "msg-1", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert "not_found" in (result.output or "")


def test_graph_404_via_api_error_exits_not_found(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise APIError("gone", response_status_code=404)

    monkeypatch.setattr("blumkin.cli.mail_delete_draft", _boom)
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

    monkeypatch.setattr("blumkin.cli.mail_draft", _boom)
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


def test_mail_update_draft_not_a_draft_exits_not_found(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise MailDraftNotFoundError("message is not a draft: msg-1")

    monkeypatch.setattr("blumkin.cli.mail_update_draft", _boom)
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

    monkeypatch.setattr("blumkin.cli.mail_update_draft", _boom)
    runner = CliRunner()
    result = runner.invoke(main, ["mail", "update-draft", "--id", "draft-1"])
    assert result.exit_code == EXIT_USAGE


def test_mail_update_draft_body_file_error_exits_usage(tmp_path, monkeypatch) -> None:
    path = tmp_path / "body.txt"
    path.write_text("x", encoding="utf-8")

    async def _boom(**_kwargs):
        raise MailBodyFileError(f"cannot read --body-file {path}: boom")

    monkeypatch.setattr("blumkin.cli.mail_update_draft", _boom)
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
        seen.update(kwargs)
        return {
            "draft": {
                "body_type": kwargs.get("body_type") or "text",
                "id": kwargs["draft_id"],
                "subject": kwargs.get("subject") or "kept",
                "to": kwargs.get("to") or "a@b.com",
            }
        }

    monkeypatch.setattr("blumkin.cli.mail_update_draft", _ok)
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
    assert seen["to"] == "b@c.com"
    assert seen["body"] is None
    assert seen["body_file"] == str(path)
    assert seen["body_type"] == "html"
    assert '"id": "draft-9"' in (result.output or "") or '"id":"draft-9"' in (result.output or "")


def test_mail_update_draft_runtime_error_exits_other(monkeypatch) -> None:
    async def _boom(**_kwargs):
        raise RuntimeError("Graph returned no message after update-draft: draft-1")

    monkeypatch.setattr("blumkin.cli.mail_update_draft", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["mail", "update-draft", "--id", "draft-1", "--subject", "x", "--json"],
    )
    assert result.exit_code == EXIT_OTHER
    assert "graph_error" in (result.output or "")


def test_calendar_today_invalid_tz_exits_usage() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--tz", "Not/ARealZone", "calendar", "today"])
    assert result.exit_code == EXIT_USAGE


def test_calendar_view_accepts_subcommand_tz() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["calendar", "view", "--from", "2026-08-25", "--to", "2026-08-26", "--tz", "Not/ARealZone"],
    )
    assert result.exit_code == EXIT_USAGE
    assert "No such option" not in (result.output or "")


def test_doctor_auth_cache_incomplete_exits_auth(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'client_id = "00000000-0000-0000-0000-000000000001"\n'
        'tenant_id = "example.onmicrosoft.com"\n'
    )
    runner = CliRunner()
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == EXIT_AUTH
