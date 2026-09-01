"""Hermetic unit tests for Google Gmail draft writes (create / update / delete / send)."""

from __future__ import annotations

import asyncio
import base64
import json
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as _email_policy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from click.testing import CliRunner
from googleapiclient.errors import HttpError

from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.exit_codes import EXIT_MISSING_SCOPE, EXIT_NOT_FOUND
from blumkin.providers.google_auth import GOOGLE_SCOPES
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.skills.mail import MailDraftNotFoundError

_MAIL_WRITES = "blumkin.providers.google.mail_writes"


def test_google_scopes_include_gmail_compose() -> None:
    assert "https://www.googleapis.com/auth/gmail.compose" in GOOGLE_SCOPES


def test_mail_draft_builds_rfc822_and_skill_payload(tmp_path: Path) -> None:
    service = _service(create_result={"id": "draft-1"})
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_draft(
                to=["a@example.com", "b@example.com"],
                cc="c@example.com",
                subject="Renewal",
                body="Please review - thanks.",
            )
        )
    assert payload["draft"] == {
        "attachments": [],
        "bcc": None,
        "body_type": "text",
        "cc": "c@example.com",
        "id": "draft-1",
        "subject": "Renewal",
        "to": "a@example.com, b@example.com",
    }
    sent = _sent_message(service, "create")
    assert sent["Subject"] == "Renewal"
    assert sent["To"] == "a@example.com, b@example.com"
    assert sent["Cc"] == "c@example.com"
    assert _content(sent, "plain").strip() == "Please review - thanks."


def test_mail_draft_html_adds_alternative(tmp_path: Path) -> None:
    service = _service(create_result={"id": "d"})
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_draft(
                to="a@example.com",
                subject="Hi",
                body="<p>Hello there</p>",
                body_type="html",
            )
        )
    assert payload["draft"]["body_type"] == "html"
    sent = _sent_message(service, "create")
    assert _content(sent, "html").strip() == "<p>Hello there</p>"
    assert "Hello there" in _content(sent, "plain")


def test_mail_draft_attaches_file(tmp_path: Path) -> None:
    attachment = tmp_path / "note.txt"
    attachment.write_text("payload bytes")
    service = _service(create_result={"id": "d"})
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_draft(
                to="a@example.com",
                subject="With file",
                body="see attached",
                attach=[str(attachment)],
            )
        )
    assert payload["draft"]["attachments"] == [
        {"id": None, "name": "note.txt", "size": len(b"payload bytes")}
    ]
    names = [part.get_filename() for part in _sent_message(service, "create").iter_attachments()]
    assert names == ["note.txt"]


def test_mail_draft_appends_configured_signature(tmp_path: Path) -> None:
    service = _service(create_result={"id": "d"})
    cfg = _cfg(tmp_path, signature=MailSignatureConfig(enabled=True, name="Ada Lovelace"))
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(cfg).mail_draft(
                to="a@example.com", subject="S", body="Body line"
            )
        )
    body = _content(_sent_message(service, "create"), "plain")
    assert "Body line" in body
    assert "Ada Lovelace" in body


def test_mail_send_draft_calls_drafts_send(tmp_path: Path) -> None:
    service = _service(send_result={"id": "m", "labelIds": ["SENT"]})
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_send_draft(draft_id=" draft-9 ")
        )
    assert payload == {"sent": "draft-9"}
    service.users.return_value.drafts.return_value.send.assert_called_once_with(
        userId="me", body={"id": "draft-9"}
    )


def test_mail_send_draft_404_maps_to_not_found_via_cli(tmp_path: Path) -> None:
    service = MagicMock()
    service.users.return_value.drafts.return_value.send.return_value.execute.side_effect = (
        HttpError(httplib2.Response({"status": 404}), b'{"error":{"message":"not found"}}', uri="x")
    )
    provider = GoogleWorkspaceProvider(_cfg(tmp_path))
    with _patched(service), patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(
            main, ["mail", "send-draft", "--id", "missing", "--yes", "--json"]
        )
    assert result.exit_code == EXIT_NOT_FOUND
    assert json.loads(result.stderr)["error"] == "not_found"


def test_mail_delete_draft_ok(tmp_path: Path) -> None:
    service = _service(get_result=_raw_draft(subject="Draft"))
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_delete_draft(draft_id="d-1")
        )
    assert payload == {"deleted": "d-1"}
    service.users.return_value.drafts.return_value.delete.assert_called_once_with(
        userId="me", id="d-1"
    )


def test_mail_delete_draft_missing_raises_not_found(tmp_path: Path) -> None:
    service = MagicMock()
    service.users.return_value.drafts.return_value.get.return_value.execute.side_effect = HttpError(
        httplib2.Response({"status": 404}), b"{}", uri="x"
    )
    with _patched(service), pytest.raises(MailDraftNotFoundError):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).mail_delete_draft(draft_id="gone"))


def test_mail_update_draft_replaces_subject_keeps_body_and_recipients(tmp_path: Path) -> None:
    service = _service(
        get_result=_raw_draft(subject="Old", to="keep@example.com", body="original body text"),
        update_result={"id": "d-2"},
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_update_draft(
                draft_id="d-2", subject="New subject"
            )
        )
    assert payload["draft"]["subject"] == "New subject"
    assert payload["draft"]["to"] == "keep@example.com"
    sent = _sent_message(service, "update")
    assert sent["Subject"] == "New subject"
    assert sent["To"] == "keep@example.com"
    assert "original body text" in _content(sent, "plain")


def test_mail_update_draft_requires_at_least_one_field(tmp_path: Path) -> None:
    with _patched(_service()), pytest.raises(ValueError, match="provide at least one"):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).mail_update_draft(draft_id="d"))


def test_mail_update_draft_appends_attachment_to_existing(tmp_path: Path) -> None:
    existing = _raw_draft(
        subject="Doc", to="a@example.com", body="hi", attachments=[("first.txt", b"one")]
    )
    new_file = tmp_path / "second.txt"
    new_file.write_text("two")
    service = _service(get_result=existing, update_result={"id": "d-3"})
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_update_draft(
                draft_id="d-3", attach=[str(new_file)]
            )
        )
    assert [a["name"] for a in payload["draft"]["attachments"]] == ["first.txt", "second.txt"]
    names = [p.get_filename() for p in _sent_message(service, "update").iter_attachments()]
    assert names == ["first.txt", "second.txt"]


def test_mail_draft_403_maps_to_missing_scope_via_cli(tmp_path: Path) -> None:
    service = MagicMock()
    service.users.return_value.drafts.return_value.create.return_value.execute.side_effect = (
        HttpError(
            httplib2.Response({"status": 403}),
            b'{"error":{"message":"insufficient authentication scopes"}}',
            uri="x",
        )
    )
    provider = GoogleWorkspaceProvider(_cfg(tmp_path))
    with _patched(service), patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(
            main,
            ["mail", "draft", "--to", "a@example.com", "--subject", "S", "--body", "B", "--json"],
        )
    assert result.exit_code == EXIT_MISSING_SCOPE
    assert json.loads(result.stderr)["error"] == "missing_scope"


def _cfg(config_dir: Path, *, signature: MailSignatureConfig | None = None) -> BlumkinConfig:
    oauth = config_dir / "desktop-client.json"
    if not oauth.is_file():
        oauth.write_text(
            '{"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "s"}}'
        )
    return BlumkinConfig(
        client_id="id.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="UTC",
        files_scopes=False,
        google_oauth_client_file=oauth,
        graph_timeout_seconds=60.0,
        legacy_flat=True,
        mail_signature=signature or MailSignatureConfig(),
        profile="default",
        provider=ProviderKind.GOOGLE,
        tags=(),
        tenant_id="",
        wo1162425_scopes=False,
    )


def _content(message: Any, subtype: str) -> str:
    part = message.get_body(preferencelist=(subtype,))
    assert part is not None, f"no {subtype} body part"
    return part.get_content()


def _patched(service: MagicMock):
    return patch.multiple(
        _MAIL_WRITES,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    )


def _raw_draft(
    *,
    subject: str,
    to: str = "",
    cc: str = "",
    body: str = "",
    attachments: list[tuple[str, bytes]] | None = None,
) -> dict:
    message = EmailMessage()
    message["Subject"] = subject
    if to:
        message["To"] = to
    if cc:
        message["Cc"] = cc
    message.set_content(body)
    for name, data in attachments or []:
        message.add_attachment(data, maintype="text", subtype="plain", filename=name)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return {"id": "d", "message": {"raw": raw}}


def _sent_message(service: MagicMock, verb: str):
    call = getattr(service.users.return_value.drafts.return_value, verb).call_args
    raw = call.kwargs["body"]["message"]["raw"]
    return message_from_bytes(
        base64.urlsafe_b64decode(raw.encode() + b"=" * (-len(raw) % 4)),
        policy=_email_policy,
    )


def _service(
    *,
    create_result: dict | None = None,
    update_result: dict | None = None,
    get_result: dict | None = None,
    send_result: dict | None = None,
) -> MagicMock:
    service = MagicMock()
    drafts = service.users.return_value.drafts.return_value
    drafts.create.return_value.execute.return_value = create_result or {"id": "d"}
    drafts.update.return_value.execute.return_value = update_result or {"id": "d"}
    drafts.get.return_value.execute.return_value = get_result or _raw_draft(subject="d")
    drafts.delete.return_value.execute.return_value = ""
    drafts.send.return_value.execute.return_value = send_result or {"id": "m"}
    return service
