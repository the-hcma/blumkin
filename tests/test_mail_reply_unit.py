"""Reply and forward drafts (issue #55, item 3)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.o_data_errors.main_error import MainError
from msgraph.generated.models.o_data_errors.o_data_error import ODataError

from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.skills.mail import (
    MailBodyFileError,
    MailMessageNotFoundError,
    format_reply_human,
    mail_forward,
    mail_reply,
)


def test_format_reply_human_names_the_original() -> None:
    lines = format_reply_human(
        {
            "draft": {
                "body_type": "html",
                "id": "draft-1",
                "kind": "reply",
                "source_message_id": "msg-1",
                "subject": "RE: Quarterly sync",
                "to": "rebecca@example.com",
            }
        }
    )

    assert lines[0] == "reply draft saved: 'RE: Quarterly sync' → rebecca@example.com (html)"
    assert lines[1] == "  id=draft-1"
    assert lines[2] == "  in reply to msg-1"


def test_format_reply_human_says_forwarding_for_a_forward() -> None:
    lines = format_reply_human(
        {"draft": {"kind": "forward", "source_message_id": "msg-1", "to": ""}}
    )

    assert lines[0].startswith("forward draft saved:")
    assert "(no recipient)" in lines[0]
    assert lines[2] == "  forwarding msg-1"


def test_mail_reply_joins_multiple_to_addresses(monkeypatch) -> None:
    """draft.to is a string like mail.draft — reply-all must not switch it to a list."""
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _draft("RE: Quarterly sync")
    draft.to_recipients = [
        SimpleNamespace(
            email_address=SimpleNamespace(address="rebecca@example.com", name="Rebecca")
        ),
        SimpleNamespace(email_address=SimpleNamespace(address="sam@example.com", name="Sam")),
    ]
    item.create_reply_all.post = AsyncMock(return_value=draft)

    payload = asyncio.run(mail_reply(message_id="msg-1", reply_all=True, body="ack"))

    assert payload["draft"]["to"] == "rebecca@example.com, sam@example.com"


def test_mail_forward_requires_a_recipient() -> None:
    with pytest.raises(ValueError, match="--to"):
        asyncio.run(mail_forward(message_id="msg-1", to="  "))


def test_mail_forward_sends_the_comment_and_recipient(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_forward.post = AsyncMock(return_value=_draft("FW: Quarterly sync"))

    payload = asyncio.run(mail_forward(message_id="msg-1", to="sam@example.com", body="FYI"))

    request = _posted(item.create_forward.post)
    assert request.comment == "FYI"
    assert request.to_recipients[0].email_address.address == "sam@example.com"
    assert payload["draft"]["kind"] == "forward"
    assert payload["draft"]["body_type"] == "html"
    assert payload["draft"]["source_message_id"] == "msg-1"


def test_mail_reply_all_uses_the_reply_all_action(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_reply_all.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))

    payload = asyncio.run(mail_reply(message_id="msg-1", body="Thanks", reply_all=True))

    assert payload["draft"]["kind"] == "reply-all"
    item.create_reply.post.assert_not_called()


def test_mail_reply_allows_an_empty_draft(monkeypatch) -> None:
    """A reply with no text yet is a normal thing to want; update-draft fills it in."""
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))

    asyncio.run(mail_reply(message_id="msg-1"))

    assert _posted(item.create_reply.post).comment == ""


def test_mail_reply_creates_a_draft_through_graph(monkeypatch) -> None:
    """createReply is what puts the draft in the original conversation."""
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))

    payload = asyncio.run(mail_reply(message_id="msg-1", body="Thanks"))

    assert _posted(item.create_reply.post).comment == "Thanks"
    draft = payload["draft"]
    assert draft["conversation_id"] == "conv-1"
    assert draft["id"] == "draft-1"
    assert draft["kind"] == "reply"
    assert draft["body_type"] == "html"
    assert draft["subject"] == "RE: Quarterly sync"
    assert draft["to"] == "rebecca@example.com"


def test_mail_reply_reports_text_when_graph_returns_no_html_body(monkeypatch) -> None:
    """createReply usually quotes as HTML; pin the fallback when Graph sends something else."""
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _draft("RE: Quarterly sync")
    draft.body = SimpleNamespace(content="plain", content_type=BodyType.Text)
    item.create_reply.post = AsyncMock(return_value=draft)

    payload = asyncio.run(mail_reply(message_id="msg-1", body="Thanks"))

    assert payload["draft"]["body_type"] == "text"


def test_mail_reply_escapes_text_comments_for_the_html_draft(monkeypatch) -> None:
    """createReply embeds the comment in HTML; unescaped <…> would be parsed as markup."""
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))

    asyncio.run(mail_reply(message_id="msg-1", body="see <5 and 6> for details"))

    assert _posted(item.create_reply.post).comment == "see &lt;5 and 6&gt; for details"


def test_mail_reply_leaves_html_comments_unescaped(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))

    asyncio.run(mail_reply(message_id="msg-1", body="<p>Thanks</p>", body_type="html"))

    assert _posted(item.create_reply.post).comment == "<p>Thanks</p>"


def test_mail_reply_fails_loudly_when_graph_returns_no_draft(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=None)

    with pytest.raises(RuntimeError, match="no draft"):
        asyncio.run(mail_reply(message_id="msg-1", body="Thanks"))


def test_mail_reply_lets_other_graph_errors_through(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(side_effect=_odata_error(400, "invalidRequest"))

    with pytest.raises(ODataError):
        asyncio.run(mail_reply(message_id="msg-1", body="Thanks"))


def test_mail_reply_rejects_a_bad_body_type() -> None:
    with pytest.raises(ValueError, match="--body-type"):
        asyncio.run(mail_reply(message_id="msg-1", body_type="markdown"))


def test_mail_reply_rejects_an_empty_id() -> None:
    with pytest.raises(ValueError, match="--id"):
        asyncio.run(mail_reply(message_id="   ", body="Thanks"))


def test_mail_reply_rejects_both_body_sources() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(mail_reply(message_id="msg-1", body="Thanks", body_file="notes.txt"))


def test_mail_reply_reports_a_missing_message_as_not_found(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(side_effect=_odata_error(404, "ErrorItemNotFound"))

    with pytest.raises(MailMessageNotFoundError, match="msg-gone"):
        asyncio.run(mail_reply(message_id="msg-gone", body="Thanks"))


def test_mail_reply_reports_an_unreadable_body_file() -> None:
    with pytest.raises(MailBodyFileError):
        asyncio.run(mail_reply(message_id="msg-1", body_file="/nonexistent/notes.txt"))


def test_mail_reply_merges_cc_onto_inherited_recipients(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _draft("RE: Quarterly sync")
    draft.cc_recipients = [
        SimpleNamespace(
            email_address=SimpleNamespace(address="already@example.com", name="Already")
        )
    ]
    item.create_reply.post = AsyncMock(return_value=draft)
    patched = _draft("RE: Quarterly sync")
    patched.cc_recipients = [
        SimpleNamespace(
            email_address=SimpleNamespace(address="already@example.com", name="Already")
        ),
        SimpleNamespace(email_address=SimpleNamespace(address="new@example.com", name="New")),
    ]
    item.patch = AsyncMock(return_value=patched)

    payload = asyncio.run(
        mail_reply(message_id="msg-1", body="Thanks", cc="new@example.com, ALREADY@example.com")
    )

    patch_msg = _posted(item.patch)
    addresses = [r.email_address.address for r in patch_msg.cc_recipients]
    assert addresses == ["already@example.com", "new@example.com"]
    assert payload["draft"]["cc"] == "already@example.com, new@example.com"
    item.patch.assert_awaited_once()


def test_mail_reply_skips_recipient_patch_when_cc_omitted(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))
    item.patch = AsyncMock()

    asyncio.run(mail_reply(message_id="msg-1", body="Thanks"))

    item.patch.assert_not_called()


def test_mail_forward_patches_bcc(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_forward.post = AsyncMock(return_value=_draft("FW: Quarterly sync"))
    patched = _draft("FW: Quarterly sync")
    patched.bcc_recipients = [
        SimpleNamespace(email_address=SimpleNamespace(address="hidden@example.com", name=None))
    ]
    item.patch = AsyncMock(return_value=patched)

    payload = asyncio.run(
        mail_forward(message_id="msg-1", to="sam@example.com", body="FYI", bcc="hidden@example.com")
    )

    assert [r.email_address.address for r in _posted(item.patch).bcc_recipients] == [
        "hidden@example.com"
    ]
    assert payload["draft"]["bcc"] == "hidden@example.com"


def test_mail_reply_appends_signature_and_converts_newlines(monkeypatch) -> None:
    client = _client(monkeypatch, signature=True)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))

    asyncio.run(mail_reply(message_id="msg-1", body="line1\nline2"))

    assert _posted(item.create_reply.post).comment == "line1<br>line2<br><br>Ada"


def test_mail_reply_signature_only_empty_body(monkeypatch) -> None:
    client = _client(monkeypatch, signature=True)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))

    asyncio.run(mail_reply(message_id="msg-1"))

    assert _posted(item.create_reply.post).comment == "Ada"


def test_mail_reply_no_signature_skips_config(monkeypatch) -> None:
    client = _client(monkeypatch, signature=True)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))

    asyncio.run(mail_reply(message_id="msg-1", body="Thanks", no_signature=True))

    assert _posted(item.create_reply.post).comment == "Thanks"


def test_mail_forward_appends_html_signature(monkeypatch) -> None:
    client = _client(monkeypatch, signature=True)
    item = client.me.messages.by_message_id.return_value
    item.create_forward.post = AsyncMock(return_value=_draft("FW: Quarterly sync"))

    asyncio.run(
        mail_forward(
            message_id="msg-1",
            to="sam@example.com",
            body="<p>FYI</p>",
            body_type="html",
        )
    )

    comment = _posted(item.create_forward.post).comment
    assert comment.startswith("<p>FYI</p><br><br>")
    assert "Ada" in comment
    assert "font-weight:bold" in comment


def _client(monkeypatch, *, signature: bool = False) -> MagicMock:
    client = MagicMock()
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    if signature:
        cfg = BlumkinConfig(
            client_id="x",
            config_dir=Path("/tmp"),
            default_tz="UTC",
            files_scopes=False,
            mail_signature=MailSignatureConfig(enabled=True, name="Ada"),
            tenant_id="t",
            wo1162425_scopes=False,
        )
        monkeypatch.setattr("blumkin.skills.mail.load_config", lambda: cfg)
    else:
        monkeypatch.setattr(
            "blumkin.skills.mail.load_config",
            lambda: SimpleNamespace(client_id="x", default_tz="UTC"),
        )
    return client


def _draft(subject: str) -> Any:
    return SimpleNamespace(
        body=SimpleNamespace(content="<p>…</p>", content_type=BodyType.Html),
        conversation_id="conv-1",
        id="draft-1",
        subject=subject,
        to_recipients=[
            SimpleNamespace(
                email_address=SimpleNamespace(address="rebecca@example.com", name="Rebecca")
            )
        ],
    )


def _odata_error(status: int, code: str) -> ODataError:
    error = ODataError()
    error.response_status_code = status
    error.error = MainError(code=code, message=code)
    return error


def _posted(post_mock: AsyncMock) -> Any:
    await_args = post_mock.await_args
    assert await_args is not None
    return await_args.args[0]
