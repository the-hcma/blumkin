"""CLI usage / exit-code mapping (no live Graph)."""

from __future__ import annotations

from click.testing import CliRunner
from kiota_abstractions.api_error import APIError

from blumkin.cli import main
from blumkin.exit_codes import EXIT_AUTH, EXIT_NOT_FOUND, EXIT_OTHER, EXIT_USAGE
from blumkin.skills.mail import MailBodyFileError, MailDraftNotFoundError


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


def test_mail_update_draft_wires_options_and_emits_json(monkeypatch) -> None:
    seen: dict[str, object] = {}

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
            "--body",
            "<p>Hi</p>",
            "--body-type",
            "html",
            "--json",
        ],
    )
    assert result.exit_code == 0
    assert seen["draft_id"] == "draft-9"
    assert seen["subject"] == "Subj"
    assert seen["to"] == "b@c.com"
    assert seen["body"] == "<p>Hi</p>"
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
