"""Hermetic unit tests for Google Gmail writes (draft / update / delete / send / reply / fwd)."""

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
from blumkin.skills.mail import MailDraftNotFoundError, MailMessageNotFoundError

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


def test_mail_reply_threads_with_original_and_sets_headers(tmp_path: Path) -> None:
    service = _service(
        message_result=_full_message(
            subject="Renewal", sender="Ada <ada@example.com>", message_id="<orig@mail>"
        ),
        create_result={"id": "d-r", "threadId": "thread-1"},
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_reply(message_id="m-1", body="On it.")
        )
    draft = payload["draft"]
    assert draft["kind"] == "reply"
    assert draft["to"] == "ada@example.com"
    assert draft["subject"] == "Re: Renewal"
    assert draft["conversation_id"] == "thread-1"
    create_body = service.users.return_value.drafts.return_value.create.call_args.kwargs["body"]
    assert create_body["message"]["threadId"] == "thread-1"
    sent = _sent_message(service, "create")
    assert sent["In-Reply-To"] == "<orig@mail>"
    assert "<orig@mail>" in sent["References"]
    assert sent["Subject"] == "Re: Renewal"
    assert "On it." in _content(sent, "plain")
    assert "> original body" in _content(sent, "plain")


def test_mail_reply_all_ccs_others_excluding_self(tmp_path: Path) -> None:
    service = _service(
        message_result=_full_message(
            subject="Sync",
            sender="Ada <ada@example.com>",
            to="me@example.com, Bob <bob@example.com>",
            cc="carol@example.com",
        ),
        profile_email="me@example.com",
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_reply(
                message_id="m-1", body="thanks", reply_all=True
            )
        )
    draft = payload["draft"]
    assert draft["kind"] == "reply-all"
    assert draft["to"] == "ada@example.com"
    assert set(draft["cc"].split(", ")) == {"bob@example.com", "carol@example.com"}


def test_mail_reply_missing_original_raises_not_found(tmp_path: Path) -> None:
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.side_effect = (
        HttpError(httplib2.Response({"status": 404}), b"{}", uri="x")
    )
    with _patched(service), pytest.raises(MailMessageNotFoundError):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).mail_reply(message_id="gone"))


def test_mail_forward_prefixes_subject_and_carries_attachment(tmp_path: Path) -> None:
    service = _service(
        message_result=_full_message(
            subject="Contract",
            sender="Ada <ada@example.com>",
            attachments=[("contract.pdf", "att-1")],
        ),
        attachment_result={"data": base64.urlsafe_b64encode(b"PDFDATA").decode()},
        # drafts.create returns threadId on the nested message, not the draft top level.
        create_result={"id": "d-f", "message": {"threadId": "thread-9"}},
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_forward(
                message_id="m-1", to="dana@example.com", body="fyi"
            )
        )
    draft = payload["draft"]
    assert draft["kind"] == "forward"
    assert draft["subject"] == "Fwd: Contract"
    assert draft["to"] == "dana@example.com"
    assert draft["conversation_id"] == "thread-9"
    sent = _sent_message(service, "create")
    assert "In-Reply-To" not in sent
    names = [part.get_filename() for part in sent.iter_attachments()]
    assert names == ["contract.pdf"]
    assert "Forwarded message" in _content(sent, "plain")


def test_mail_forward_requires_to(tmp_path: Path) -> None:
    with _patched(_service()), pytest.raises(ValueError, match="--to is required"):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).mail_forward(message_id="m-1", to="  "))


def test_mail_reply_to_own_sent_message_addresses_original_recipients(tmp_path: Path) -> None:
    service = _service(
        message_result=_full_message(
            subject="Update",
            sender="me@example.com",
            to="client@example.com, cc@example.com",
        ),
        profile_email="me@example.com",
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_reply(message_id="m-1", body="follow-up")
        )
    # Following up on your own mail goes to whoever it was sent to, not yourself.
    assert set(payload["draft"]["to"].split(", ")) == {"client@example.com", "cc@example.com"}


def test_mail_reply_all_excludes_reply_to_with_display_name(tmp_path: Path) -> None:
    service = _service(
        message_result=_full_message(
            subject="Sync",
            sender="Ada <ada@example.com>",
            to="Ada <ada@example.com>, me@example.com, Bob <bob@example.com>",
            reply_to="Ada Lovelace <ada@example.com>",
        ),
        profile_email="me@example.com",
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_reply(
                message_id="m-1", body="thanks", reply_all=True
            )
        )
    assert payload["draft"]["to"] == "ada@example.com"
    # ada@example.com must not also appear in Cc.
    assert payload["draft"]["cc"] == "bob@example.com"


def test_mail_reply_spoofed_reply_to_self_still_replies_to_sender(tmp_path: Path) -> None:
    # From is a third party; a sender-controlled Reply-To: me must NOT redirect the
    # reply to the other recipients.
    service = _service(
        message_result=_full_message(
            subject="Invoice",
            sender="attacker@evil.com",
            to="me@example.com",
            cc="victim@example.com",
            reply_to="me@example.com",
        ),
        profile_email="me@example.com",
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_reply(message_id="m-1", body="no thanks")
        )
    assert payload["draft"]["to"] == "me@example.com"  # honors Reply-To as given
    assert "victim@example.com" not in (payload["draft"].get("cc") or "")


def test_mail_reply_all_merges_cc_flag_with_original(tmp_path: Path) -> None:
    service = _service(
        message_result=_full_message(
            subject="Sync",
            sender="Ada <ada@example.com>",
            to="me@example.com, Bob <bob@example.com>",
        ),
        profile_email="me@example.com",
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_reply(
                message_id="m-1",
                body="ok",
                reply_all=True,
                cc=["bob@example.com", "extra@example.com"],
            )
        )
    # bob (from the original) + extra (from --cc), deduped.
    assert set(payload["draft"]["cc"].split(", ")) == {"bob@example.com", "extra@example.com"}


def test_mail_reply_html_quotes_original_in_blockquote(tmp_path: Path) -> None:
    service = _service(
        message_result=_full_message(subject="Q", sender="Ada <ada@example.com>", body="a < b"),
    )
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_reply(
                message_id="m-1", body="<p>reply</p>", body_type="html"
            )
        )
    html = _content(_sent_message(service, "create"), "html")
    assert "<blockquote>" in html
    assert "a &lt; b" in html  # original escaped
    plain = _content(_sent_message(service, "create"), "plain")
    assert "reply" in plain


def test_mail_forward_html_quotes_forwarded_block(tmp_path: Path) -> None:
    service = _service(
        message_result=_full_message(subject="Doc", sender="Ada <ada@example.com>", body="x & y"),
        create_result={"id": "d", "message": {"threadId": "t"}},
    )
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_forward(
                message_id="m-1", to="dana@example.com", body="<p>fyi</p>", body_type="html"
            )
        )
    html = _content(_sent_message(service, "create"), "html")
    assert "Forwarded message" in html
    assert "x &amp; y" in html


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


def _full_message(
    *,
    subject: str,
    sender: str,
    to: str = "",
    cc: str = "",
    body: str = "original body",
    message_id: str = "<orig@mail>",
    references: str = "",
    thread_id: str = "thread-1",
    reply_to: str = "",
    attachments: list[tuple[str, str]] | None = None,
) -> dict:
    headers = [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": subject},
        {"name": "Message-ID", "value": message_id},
        {"name": "Date", "value": "Mon, 01 Sep 2026 09:00:00 +0000"},
    ]
    if to:
        headers.append({"name": "To", "value": to})
    if cc:
        headers.append({"name": "Cc", "value": cc})
    if references:
        headers.append({"name": "References", "value": references})
    if reply_to:
        headers.append({"name": "Reply-To", "value": reply_to})
    parts: list[dict] = [
        {
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
        }
    ]
    for name, att_id in attachments or []:
        parts.append(
            {
                "mimeType": "application/pdf",
                "filename": name,
                "body": {"attachmentId": att_id, "size": 3},
            }
        )
    return {
        "id": "m-1",
        "threadId": thread_id,
        "internalDate": "1756717200000",
        "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts},
    }


def _service(
    *,
    create_result: dict | None = None,
    update_result: dict | None = None,
    get_result: dict | None = None,
    send_result: dict | None = None,
    message_result: dict | None = None,
    attachment_result: dict | None = None,
    profile_email: str = "me@example.com",
) -> MagicMock:
    service = MagicMock()
    users = service.users.return_value
    drafts = users.drafts.return_value
    drafts.create.return_value.execute.return_value = create_result or {"id": "d", "threadId": "t"}
    drafts.update.return_value.execute.return_value = update_result or {"id": "d"}
    drafts.get.return_value.execute.return_value = get_result or _raw_draft(subject="d")
    drafts.delete.return_value.execute.return_value = ""
    drafts.send.return_value.execute.return_value = send_result or {"id": "m"}
    messages = users.messages.return_value
    messages.get.return_value.execute.return_value = message_result or _full_message(
        subject="Hi", sender="Ada <ada@example.com>"
    )
    messages.attachments.return_value.get.return_value.execute.return_value = attachment_result or {
        "data": base64.urlsafe_b64encode(b"pdf").decode()
    }
    users.getProfile.return_value.execute.return_value = {"emailAddress": profile_email}
    return service
