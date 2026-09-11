"""Hermetic coverage for mail triage: move / mark / delete (issue #177)."""

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
from msgraph.generated.models.followup_flag_status import FollowupFlagStatus
from msgraph.generated.models.importance import Importance
from msgraph.generated.models.o_data_errors.main_error import MainError
from msgraph.generated.models.o_data_errors.o_data_error import ODataError

from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.exit_codes import EXIT_MISSING_SCOPE, EXIT_NOT_FOUND, EXIT_USAGE
from blumkin.providers.google import mail_writes as google_mail_writes
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.skills.mail import (
    MailFolderNotFoundError,
    MailMessageNotFoundError,
    format_triage_human,
    mail_delete,
    mail_mark,
    mail_move,
)


def _graph(monkeypatch) -> MagicMock:
    client = MagicMock()
    by_id = client.me.messages.by_message_id.return_value
    by_id.patch = AsyncMock(return_value=SimpleNamespace(id="m"))
    by_id.delete = AsyncMock(return_value=None)
    by_id.move.post = AsyncMock(return_value=SimpleNamespace(id="m"))
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    return client


def _odata(status: int, code: str | None = None) -> ODataError:
    err = ODataError()
    err.response_status_code = status
    if code:
        err.error = MainError(code=code)
    return err


# --------------------------------------------------------------------------- Graph


def test_graph_mark_builds_the_patch(monkeypatch) -> None:
    client = _graph(monkeypatch)
    asyncio.run(mail_mark(message_ids=["m1"], read=True, flagged=True, importance="high"))
    patch_body = client.me.messages.by_message_id.return_value.patch.await_args.args[0]
    assert patch_body.is_read is True
    assert patch_body.flag.flag_status == FollowupFlagStatus.Flagged
    assert patch_body.importance == Importance.High


def test_graph_mark_needs_at_least_one_field(monkeypatch) -> None:
    _graph(monkeypatch)
    with pytest.raises(ValueError, match="at least one of"):
        asyncio.run(mail_mark(message_ids=["m1"]))


def test_graph_move_resolves_well_known_and_returns_to(monkeypatch) -> None:
    client = _graph(monkeypatch)
    payload = asyncio.run(mail_move(message_ids=["m1"], to="archive"))
    body = client.me.messages.by_message_id.return_value.move.post.await_args.args[0]
    assert body.destination_id == "archive"
    assert payload == {"moved": ["m1"], "count": 1, "skipped": [], "to": "archive"}


def test_graph_move_resolves_a_display_name_to_a_folder_id(monkeypatch) -> None:
    client = _graph(monkeypatch)
    monkeypatch.setattr(
        "blumkin.skills.mail._resolve_folder_fallback",
        AsyncMock(return_value=("AAMkFOLDERID", None, False)),
    )
    payload = asyncio.run(mail_move(message_ids=["m1"], to="Receipts"))
    body = client.me.messages.by_message_id.return_value.move.post.await_args.args[0]
    assert body.destination_id == "AAMkFOLDERID"
    assert payload["to"] == "AAMkFOLDERID"


def test_graph_move_none_response_is_not_found(monkeypatch) -> None:
    # Graph .move.post returns None for a missing message; the move-specific
    # _apply branch turns that into MailMessageNotFoundError.
    client = _graph(monkeypatch)
    client.me.messages.by_message_id.return_value.move.post = AsyncMock(return_value=None)
    with pytest.raises(MailMessageNotFoundError):
        asyncio.run(mail_move(message_ids=["gone"], to="archive"))


def test_graph_mark_direction_branches(monkeypatch) -> None:
    client = _graph(monkeypatch)
    asyncio.run(mail_mark(message_ids=["m1"], read=False, flagged=False, importance="low"))
    patch_body = client.me.messages.by_message_id.return_value.patch.await_args.args[0]
    assert patch_body.is_read is False
    assert patch_body.flag.flag_status == FollowupFlagStatus.NotFlagged
    assert patch_body.importance == Importance.Low


def test_graph_mark_importance_normal(monkeypatch) -> None:
    client = _graph(monkeypatch)
    asyncio.run(mail_mark(message_ids=["m1"], importance="normal"))
    patch_body = client.me.messages.by_message_id.return_value.patch.await_args.args[0]
    assert patch_body.importance == Importance.Normal


def test_graph_delete_calls_delete(monkeypatch) -> None:
    client = _graph(monkeypatch)
    payload = asyncio.run(mail_delete(message_ids=["m1"]))
    client.me.messages.by_message_id.return_value.delete.assert_awaited_once()
    assert payload == {"deleted": ["m1"], "count": 1, "skipped": []}


def test_graph_single_missing_message_is_not_found(monkeypatch) -> None:
    client = _graph(monkeypatch)
    client.me.messages.by_message_id.return_value.delete = AsyncMock(side_effect=_odata(404))
    with pytest.raises(MailMessageNotFoundError):
        asyncio.run(mail_delete(message_ids=["gone"]))


def test_graph_batch_reports_skips_but_all_fail_propagates(monkeypatch) -> None:
    client = _graph(monkeypatch)
    client.me.messages.by_message_id.return_value.delete = AsyncMock(
        side_effect=[None, _odata(404)]
    )
    payload = asyncio.run(mail_delete(message_ids=["ok", "gone"]))
    assert payload["deleted"] == ["ok"]
    assert payload["skipped"] == [{"id": "gone", "reason": "message not found: gone"}]

    # Every id fails: _mail_triage_batch re-raises the first error so the batch
    # cannot report exit 0 with an empty result set.
    client.me.messages.by_message_id.return_value.delete = AsyncMock(
        side_effect=[_odata(404), _odata(404)]
    )
    with pytest.raises(ODataError):
        asyncio.run(mail_delete(message_ids=["a", "b"]))


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


def test_google_move_archive_removes_inbox(tmp_path: Path) -> None:
    service = MagicMock()
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_move(
                message_ids=["m1"], to="archive"
            )
        )
    body = service.users.return_value.messages.return_value.modify.call_args.kwargs["body"]
    assert body == {"addLabelIds": [], "removeLabelIds": ["INBOX"]}
    assert payload["to"] == "archive"


def test_google_mark_maps_flag_and_importance(tmp_path: Path) -> None:
    service = MagicMock()
    with _google_patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_mark(
                message_ids=["m1"], read=True, flagged=True, importance="low"
            )
        )
    body = service.users.return_value.messages.return_value.modify.call_args.kwargs["body"]
    assert set(body["addLabelIds"]) == {"STARRED"}
    assert set(body["removeLabelIds"]) == {"UNREAD", "IMPORTANT"}


def test_google_mark_direction_branches(tmp_path: Path) -> None:
    service = MagicMock()
    with _google_patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_mark(
                message_ids=["m1"], read=False, flagged=False, importance="high"
            )
        )
    body = service.users.return_value.messages.return_value.modify.call_args.kwargs["body"]
    assert set(body["addLabelIds"]) == {"UNREAD", "IMPORTANT"}
    assert set(body["removeLabelIds"]) == {"STARRED"}


def test_google_mark_importance_normal(tmp_path: Path) -> None:
    service = MagicMock()
    with _google_patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_mark(
                message_ids=["m1"], importance="normal"
            )
        )
    body = service.users.return_value.messages.return_value.modify.call_args.kwargs["body"]
    assert "IMPORTANT" not in body["addLabelIds"]
    assert "IMPORTANT" in body["removeLabelIds"]


def test_google_move_to_inbox_keeps_the_inbox_label(tmp_path: Path) -> None:
    service = MagicMock()
    with _google_patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_move(message_ids=["m1"], to="inbox")
        )
    body = service.users.return_value.messages.return_value.modify.call_args.kwargs["body"]
    assert body == {"addLabelIds": ["INBOX"], "removeLabelIds": []}


def test_google_move_resolves_a_label_name_to_its_id(tmp_path: Path) -> None:
    service = MagicMock()
    service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
        "labels": [{"id": "Label_42", "name": "Receipts"}]
    }
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_move(
                message_ids=["m1"], to="receipts"
            )
        )
    body = service.users.return_value.messages.return_value.modify.call_args.kwargs["body"]
    assert body == {"addLabelIds": ["Label_42"], "removeLabelIds": ["INBOX"]}
    assert payload["to"] == "Receipts"


def test_google_move_unknown_label_name_is_not_found(tmp_path: Path) -> None:
    service = MagicMock()
    service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
        "labels": [{"id": "Label_1", "name": "Work"}]
    }
    with _google_patched(service), pytest.raises(MailFolderNotFoundError):
        asyncio.run(
            google_mail_writes.mail_move(
                message_ids=["m1"], to="Nope", config=_google_cfg(tmp_path)
            )
        )


def test_google_triage_batch_skips_and_propagates_total_failure(tmp_path: Path) -> None:
    service = MagicMock()
    modify = service.users.return_value.messages.return_value.modify.return_value
    modify.execute.side_effect = [None, HttpError(httplib2.Response({"status": 404}), b"gone")]
    with _google_patched(service):
        payload = asyncio.run(
            google_mail_writes.mail_move(
                message_ids=["ok", "gone"], to="archive", config=_google_cfg(tmp_path)
            )
        )
    assert payload["moved"] == ["ok"]
    assert payload["skipped"] == [{"id": "gone", "reason": "message not found: gone"}]

    modify.execute.side_effect = HttpError(httplib2.Response({"status": 500}), b"boom")
    with _google_patched(service), pytest.raises(HttpError):
        asyncio.run(
            google_mail_writes.mail_move(
                message_ids=["a", "b"], to="archive", config=_google_cfg(tmp_path)
            )
        )


def test_google_delete_trashes(tmp_path: Path) -> None:
    service = MagicMock()
    with _google_patched(service):
        asyncio.run(GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_delete(message_ids=["m1"]))
    service.users.return_value.messages.return_value.trash.assert_called_once_with(
        userId="me", id="m1"
    )


def test_google_missing_scope_fails_closed(tmp_path: Path) -> None:
    from blumkin.auth import MissingScopeError

    with patch(
        "blumkin.providers.google.mail_writes.get_credentials",
        side_effect=MissingScopeError(
            "needs gmail.modify", missing=frozenset(), current=frozenset()
        ),
    ):
        with pytest.raises(MissingScopeError):
            asyncio.run(
                google_mail_writes.mail_move(
                    message_ids=["m1"], to="archive", config=_google_cfg(tmp_path)
                )
            )


def test_google_move_404_maps_not_found(tmp_path: Path) -> None:
    service = MagicMock()
    service.users.return_value.messages.return_value.modify.return_value.execute.side_effect = (
        HttpError(httplib2.Response({"status": 404}), b"gone")
    )
    with _google_patched(service), pytest.raises(MailMessageNotFoundError):
        asyncio.run(
            google_mail_writes.mail_move(
                message_ids=["gone"], to="archive", config=_google_cfg(tmp_path)
            )
        )


# --------------------------------------------------------------------------- formatter / CLI


def test_format_triage_human() -> None:
    assert (
        "Moved 1 message(s) -> archive:"
        in format_triage_human({"moved": ["m"], "count": 1, "to": "archive"})[0]
    )
    lines = format_triage_human({"deleted": ["a"], "skipped": [{"id": "b", "reason": "gone"}]})
    assert any("skipped b: gone" in line for line in lines)


def test_cli_triage_help_and_yes() -> None:
    for verb in ("move", "mark", "delete"):
        out = CliRunner().invoke(main, ["mail", verb, "--help"]).output
        assert "--id" in out and "--yes" in out
    no_yes = CliRunner().invoke(main, ["mail", "delete", "--id", "m", "--json"])
    assert no_yes.exit_code == EXIT_USAGE


def test_cli_triage_without_yes_never_reaches_the_provider(monkeypatch) -> None:
    """delete / mark / move are mutates:true, notifies_others:false - the consent gate
    still demands --yes (catalog `--yes required: True`), and must fail before dispatch
    ever calls the provider. A regression dropping `yes` from _run_mail_triage's args
    would slip past this without a CLI-level check."""
    spy = SimpleNamespace(
        mail_delete=AsyncMock(return_value={}),
        mail_mark=AsyncMock(return_value={}),
        mail_move=AsyncMock(return_value={}),
    )
    monkeypatch.setattr("blumkin.cli._workspace", lambda: spy)
    cases = [
        ["mail", "delete", "--id", "m", "--json"],
        ["mail", "mark", "--id", "m", "--read", "--json"],
        ["mail", "move", "--id", "m", "--to", "archive", "--json"],
    ]
    for argv in cases:
        result = CliRunner().invoke(main, argv)
        assert result.exit_code == EXIT_USAGE, argv
    spy.mail_delete.assert_not_awaited()
    spy.mail_mark.assert_not_awaited()
    spy.mail_move.assert_not_awaited()


def test_cli_triage_missing_scope_routes(monkeypatch) -> None:
    from blumkin.auth import MissingScopeError

    async def _raise(**_kwargs):
        raise MissingScopeError("needs gmail.modify", missing=frozenset(), current=frozenset())

    monkeypatch.setattr("blumkin.cli._workspace", lambda: SimpleNamespace(mail_move=_raise))
    result = CliRunner().invoke(
        main, ["mail", "move", "--id", "m", "--to", "archive", "--yes", "--json"]
    )
    assert result.exit_code == EXIT_MISSING_SCOPE


def test_cli_mark_forwards_options_and_returns_success(monkeypatch) -> None:
    seen = {}

    async def _mark(**kwargs):
        seen.update(kwargs)
        return {"marked": ["m"], "count": 1, "skipped": []}

    monkeypatch.setattr("blumkin.cli._workspace", lambda: SimpleNamespace(mail_mark=_mark))
    result = CliRunner().invoke(
        main,
        [
            "mail",
            "mark",
            "--id",
            "m",
            "--read",
            "--flag",
            "--importance",
            "high",
            "--yes",
            "--json",
        ],
    )
    assert result.exit_code == 0
    assert seen == {
        "message_ids": ["m"],
        "read": True,
        "flagged": True,
        "importance": "high",
    }
    assert json.loads(result.stdout)["marked"] == ["m"]


def test_cli_triage_not_found(monkeypatch) -> None:
    async def _raise(**_kwargs):
        raise MailMessageNotFoundError("message not found: m")

    monkeypatch.setattr("blumkin.cli._workspace", lambda: SimpleNamespace(mail_delete=_raise))
    result = CliRunner().invoke(main, ["mail", "delete", "--id", "m", "--yes", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert json.loads(result.stderr)["error"] == "not_found"
