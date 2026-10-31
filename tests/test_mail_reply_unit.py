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

from blumkin.config import (
    BlumkinConfig,
    MailSignatureConfig,
    MessagePolicyConfig,
    PreferencesConfig,
)
from blumkin.providers.kind import ProviderKind
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


def test_format_reply_human_lists_cc_and_bcc() -> None:
    lines = format_reply_human(
        {
            "draft": {
                "bcc": "secret@example.com",
                "body_type": "html",
                "cc": "sam@example.com",
                "id": "draft-1",
                "kind": "reply",
                "source_message_id": "msg-1",
                "subject": "RE: Hi",
                "to": "a@b.com",
            }
        }
    )
    assert "  cc: sam@example.com" in lines
    assert "  bcc: secret@example.com" in lines


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
    item.get = AsyncMock(return_value=draft)

    payload = asyncio.run(mail_reply(message_id="msg-1", reply_all=True, body="ack"))

    assert payload["draft"]["to"] == "rebecca@example.com, sam@example.com"


def test_mail_forward_requires_a_recipient() -> None:
    with pytest.raises(ValueError, match="--to"):
        asyncio.run(mail_forward(message_id="msg-1", to="  "))


def test_mail_forward_sends_the_comment_and_recipient(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_forward.post = AsyncMock(return_value=_draft("FW: Quarterly sync"))

    payload = asyncio.run(
        mail_forward(message_id="msg-1", to="sam@example.com", body="FYI", body_type="text")
    )

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
    draft = _draft("RE: Quarterly sync")
    item.create_reply.post = AsyncMock(return_value=draft)
    item.get = AsyncMock(return_value=draft)

    payload = asyncio.run(mail_reply(message_id="msg-1", body="Thanks", body_type="text"))

    assert _posted(item.create_reply.post).comment == "Thanks"
    draft_out = payload["draft"]
    assert draft_out["conversation_id"] == "conv-1"
    assert draft_out["id"] == "draft-1"
    assert draft_out["kind"] == "reply"
    assert draft_out["body_type"] == "html"
    assert draft_out["subject"] == "RE: Quarterly sync"
    assert draft_out["to"] == "rebecca@example.com"


def test_mail_reply_reports_text_when_graph_returns_no_html_body(monkeypatch) -> None:
    """createReply usually quotes as HTML; pin the fallback when Graph sends something else."""
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _draft("RE: Quarterly sync")
    draft.body = SimpleNamespace(content="plain", content_type=BodyType.Text)
    item.create_reply.post = AsyncMock(return_value=draft)
    item.get = AsyncMock(return_value=draft)

    payload = asyncio.run(mail_reply(message_id="msg-1", body="Thanks"))

    assert payload["draft"]["body_type"] == "text"


def test_mail_reply_escapes_text_comments_for_the_html_draft(monkeypatch) -> None:
    """createReply embeds the comment in HTML; unescaped <…> would be parsed as markup."""
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))

    asyncio.run(mail_reply(message_id="msg-1", body="see <5 and 6> for details", body_type="text"))

    assert _posted(item.create_reply.post).comment == "see &lt;5 and 6&gt; for details"


def _patched_body(item: Any) -> Any:
    """The body of the one PATCH sent to the draft."""
    assert item.patch.await_count == 1
    return item.patch.await_args.args[0].body


def _with_quote(subject: str) -> Any:
    draft = _draft(subject)
    draft.body = SimpleNamespace(
        content='<p></p><hr><div id="divRplyFwdMsg"><b>From:</b> Rebecca</div>',
        content_type=BodyType.Html,
    )
    return draft


def test_mail_reply_with_an_html_body_is_patched_in_above_the_quote(monkeypatch) -> None:
    """Graph reads a createReply comment as plain text, so HTML is put into the body (#612)."""
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _with_quote("RE: Quarterly sync")
    item.create_reply.post = AsyncMock(return_value=draft)
    item.get = AsyncMock(return_value=draft)
    item.patch = AsyncMock(return_value=draft)

    asyncio.run(
        mail_reply(message_id="msg-1", body="<p><strong>Thanks</strong></p>", body_type="html")
    )

    assert _posted(item.create_reply.post).comment == ""
    body = _patched_body(item)
    assert body.content_type == BodyType.Html
    assert body.content == (
        '<p><strong>Thanks</strong></p><hr><div id="divRplyFwdMsg"><b>From:</b> Rebecca</div>'
    )


def test_mail_reply_all_with_an_html_body_file_is_patched_too(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _with_quote("RE: Quarterly sync")
    item.create_reply_all.post = AsyncMock(return_value=draft)
    item.get = AsyncMock(return_value=draft)
    item.patch = AsyncMock(return_value=draft)
    html = tmp_path / "body.html"
    html.write_text("<p>Hello <code>x</code></p>")

    asyncio.run(
        mail_reply(message_id="msg-1", body_file=str(html), body_type="html", reply_all=True)
    )

    assert _posted(item.create_reply_all.post).comment == ""
    assert "<code>x</code>" in _patched_body(item).content


def test_mail_reply_keeps_the_whole_draft_when_no_quote_is_recognised(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _draft("RE: Quarterly sync")  # body "<p>…</p>": no quote marker
    item.create_reply.post = AsyncMock(return_value=draft)
    item.get = AsyncMock(return_value=draft)
    item.patch = AsyncMock(return_value=draft)

    asyncio.run(mail_reply(message_id="msg-1", body="<p>Thanks</p>", body_type="html"))

    assert _patched_body(item).content == "<p>Thanks</p><p>…</p>"


def test_mail_reply_markdown_is_rendered_to_html_and_patched(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _with_quote("RE: Quarterly sync")
    item.create_reply.post = AsyncMock(return_value=draft)
    item.get = AsyncMock(return_value=draft)
    item.patch = AsyncMock(return_value=draft)

    asyncio.run(mail_reply(message_id="msg-1", body="**bold** text", body_type="markdown"))

    assert _posted(item.create_reply.post).comment == ""
    assert "<strong>bold</strong>" in _patched_body(item).content


def test_mail_reply_with_a_text_body_still_goes_through_the_comment(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))
    item.patch = AsyncMock()

    asyncio.run(mail_reply(message_id="msg-1", body="Thanks", body_type="text", no_signature=True))

    assert _posted(item.create_reply.post).comment == "Thanks"
    item.patch.assert_not_called()


def test_mail_forward_with_an_html_body_is_patched_in_above_the_quote(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _with_quote("FW: Quarterly sync")
    item.create_forward.post = AsyncMock(return_value=draft)
    item.get = AsyncMock(return_value=draft)
    item.patch = AsyncMock(return_value=draft)

    asyncio.run(
        mail_forward(message_id="msg-1", to="sam@example.com", body="<p>FYI</p>", body_type="html")
    )

    assert _posted(item.create_forward.post).comment == ""
    assert _patched_body(item).content.startswith("<p>FYI</p><hr>")


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
        asyncio.run(mail_reply(message_id="msg-1", body_type="richtext"))


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
    # createReply body often omits recipient collections; live GET supplies the merge base.
    created = _draft("RE: Quarterly sync")
    created.cc_recipients = None
    live = _draft("RE: Quarterly sync")
    live.cc_recipients = [
        SimpleNamespace(
            email_address=SimpleNamespace(address="already@example.com", name="Already")
        )
    ]
    item.create_reply.post = AsyncMock(return_value=created)
    item.get = AsyncMock(return_value=live)
    patched = _draft("RE: Quarterly sync")
    patched.cc_recipients = [
        SimpleNamespace(
            email_address=SimpleNamespace(address="already@example.com", name="Already")
        ),
        SimpleNamespace(email_address=SimpleNamespace(address="new@example.com", name="New")),
    ]
    item.patch = AsyncMock(return_value=patched)

    payload = asyncio.run(
        mail_reply(
            message_id="msg-1",
            body="Thanks",
            body_type="text",
            cc="new@example.com, ALREADY@example.com",
        )
    )

    patch_msg = _posted(item.patch)
    addresses = [r.email_address.address for r in patch_msg.cc_recipients]
    assert addresses == ["already@example.com", "new@example.com"]
    # Preserved recipients keep display names; newly added ones have none.
    assert [r.email_address.name for r in patch_msg.cc_recipients] == ["Already", None]
    assert payload["draft"]["cc"] == "already@example.com, new@example.com"
    item.get.assert_awaited()
    item.patch.assert_awaited_once()


def test_mail_reply_skips_recipient_patch_when_cc_omitted(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    created = _draft("RE: Quarterly sync")
    created.cc_recipients = None
    live = _draft("RE: Quarterly sync")
    live.cc_recipients = [
        SimpleNamespace(
            email_address=SimpleNamespace(address="already@example.com", name="Already")
        )
    ]
    item.create_reply.post = AsyncMock(return_value=created)
    item.get = AsyncMock(return_value=live)
    item.patch = AsyncMock()

    payload = asyncio.run(mail_reply(message_id="msg-1", body="Thanks", body_type="text"))

    item.get.assert_awaited()
    item.patch.assert_not_called()
    assert payload["draft"]["cc"] == "already@example.com"


def test_mail_reply_deletes_draft_when_recipient_patch_fails(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))
    item.get = AsyncMock(return_value=_draft("RE: Quarterly sync"))
    item.patch = AsyncMock(side_effect=_odata_error(400, "ErrorInvalidRecipients"))
    item.delete = AsyncMock(return_value=None)

    with pytest.raises(ODataError):
        asyncio.run(mail_reply(message_id="msg-1", body="Thanks", cc="bad@example.com"))

    item.delete.assert_awaited_once()


def test_mail_reply_keeps_draft_when_post_patch_refetch_fails(monkeypatch) -> None:
    """PATCH succeeded (empty body); a failing re-fetch must not delete the built draft."""
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))
    item.get = AsyncMock(
        side_effect=[
            _draft("RE: Quarterly sync"),
            _odata_error(503, "ErrorServiceUnavailable"),
        ]
    )
    item.patch = AsyncMock(return_value=None)
    item.delete = AsyncMock(return_value=None)

    with pytest.raises(ODataError):
        asyncio.run(mail_reply(message_id="msg-1", body="Thanks", cc="new@example.com"))

    item.patch.assert_awaited_once()
    item.delete.assert_not_called()


def test_mail_forward_merges_bcc_onto_inherited_recipients(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    created = _draft("FW: Quarterly sync")
    created.bcc_recipients = None
    live = _draft("FW: Quarterly sync")
    live.bcc_recipients = [
        SimpleNamespace(email_address=SimpleNamespace(address="hidden@example.com", name="Hidden"))
    ]
    item.create_forward.post = AsyncMock(return_value=created)
    item.get = AsyncMock(return_value=live)
    patched = _draft("FW: Quarterly sync")
    patched.bcc_recipients = [
        SimpleNamespace(email_address=SimpleNamespace(address="hidden@example.com", name="Hidden")),
        SimpleNamespace(email_address=SimpleNamespace(address="new@example.com", name=None)),
    ]
    item.patch = AsyncMock(return_value=patched)

    payload = asyncio.run(
        mail_forward(
            message_id="msg-1",
            to="sam@example.com",
            body="FYI",
            body_type="text",
            bcc="new@example.com, HIDDEN@example.com",
        )
    )

    patch_msg = _posted(item.patch)
    assert [r.email_address.address for r in patch_msg.bcc_recipients] == [
        "hidden@example.com",
        "new@example.com",
    ]
    assert [r.email_address.name for r in patch_msg.bcc_recipients] == ["Hidden", None]
    assert payload["draft"]["bcc"] == "hidden@example.com, new@example.com"
    item.get.assert_awaited()
    item.patch.assert_awaited_once()


def test_mail_reply_appends_signature_and_converts_newlines(monkeypatch) -> None:
    client = _client(monkeypatch, signature=True)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))

    asyncio.run(mail_reply(message_id="msg-1", body="line1\nline2", body_type="text"))

    assert _posted(item.create_reply.post).comment == "line1<br>line2<br><br>Ada"


def test_mail_reply_signature_only_empty_body(monkeypatch) -> None:
    client = _client(monkeypatch, signature=True)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))

    draft = _draft("RE: Quarterly sync")
    item.get = AsyncMock(return_value=draft)
    item.patch = AsyncMock(return_value=draft)

    asyncio.run(mail_reply(message_id="msg-1"))

    # Empty-body reply on the markdown default: the signature is HTML, so it is put into the
    # body above the quoted original instead of travelling as a plain-text comment.
    assert _posted(item.create_reply.post).comment == ""
    assert _patched_body(item).content.startswith(
        '<span style="color:#003366;font-weight:bold">Ada</span>'
    )


def test_mail_reply_no_signature_skips_config(monkeypatch) -> None:
    client = _client(monkeypatch, signature=True)
    item = client.me.messages.by_message_id.return_value
    item.create_reply.post = AsyncMock(return_value=_draft("RE: Quarterly sync"))

    asyncio.run(mail_reply(message_id="msg-1", body="Thanks", body_type="text", no_signature=True))

    assert _posted(item.create_reply.post).comment == "Thanks"


def test_mail_forward_appends_html_signature(monkeypatch) -> None:
    client = _client(monkeypatch, signature=True)
    item = client.me.messages.by_message_id.return_value
    draft = _draft("FW: Quarterly sync")
    item.create_forward.post = AsyncMock(return_value=draft)
    item.get = AsyncMock(return_value=draft)
    item.patch = AsyncMock(return_value=draft)

    asyncio.run(
        mail_forward(
            message_id="msg-1",
            to="sam@example.com",
            body="<p>FYI</p>",
            body_type="html",
        )
    )

    comment = _patched_body(item).content
    assert comment.startswith("<p>FYI</p><br><br>")
    assert "Ada" in comment
    assert "font-weight:bold" in comment


def _client(monkeypatch, *, signature: bool = False) -> MagicMock:
    client = MagicMock()
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    # Reply/forward always re-fetch the draft for accurate recipient summaries.
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=_draft("RE: live"))
    # A markup body is patched into the draft after it is created (#612).
    client.me.messages.by_message_id.return_value.patch = AsyncMock(return_value=_draft("RE: live"))
    if signature:
        cfg = BlumkinConfig(
            client_id="x",
            config_dir=Path("/tmp"),
            default_tz="UTC",
            email="",
            files_scopes=False,
            google_oauth_client_file=None,
            graph_timeout_seconds=60.0,
            mail_signature=MailSignatureConfig(enabled=True, name="Ada"),
            preferences=PreferencesConfig(),
            profile="default",
            provider=ProviderKind.MICROSOFT,
            tags=(),
            tenant_id="t",
            wo1162425_scopes=False,
        )
        monkeypatch.setattr("blumkin.skills.mail.load_config", lambda: cfg)
    else:
        monkeypatch.setattr(
            "blumkin.skills.mail.load_config",
            lambda: SimpleNamespace(
                client_id="x",
                default_tz="UTC",
                mail_signature=MailSignatureConfig(),
                message_policy=MessagePolicyConfig(),
            ),
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


def test_mail_reply_deletes_the_draft_when_the_body_patch_fails(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _with_quote("RE: Quarterly sync")
    item.create_reply.post = AsyncMock(return_value=draft)
    item.get = AsyncMock(return_value=draft)
    item.patch = AsyncMock(side_effect=_odata_error(400, "ErrorInvalidRequest"))
    item.delete = AsyncMock()

    with pytest.raises(ODataError):
        asyncio.run(mail_reply(message_id="msg-1", body="<p>Thanks</p>", body_type="html"))

    item.delete.assert_awaited_once()


def test_a_failed_follow_up_read_does_not_delete_a_draft_whose_body_landed(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _with_quote("RE: Quarterly sync")
    item.create_reply.post = AsyncMock(return_value=draft)
    item.get = AsyncMock(side_effect=[draft, None])
    item.patch = AsyncMock(return_value=draft)
    item.delete = AsyncMock()

    with pytest.raises(RuntimeError, match="no draft after the body update"):
        asyncio.run(mail_reply(message_id="msg-1", body="<p>Thanks</p>", body_type="html"))

    item.delete.assert_not_called()


def test_an_unfamiliar_quote_shape_is_kept_whole_below_the_new_text(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _draft("RE: Quarterly sync")
    draft.body = SimpleNamespace(
        content="<p>From: Rebecca</p><blockquote>earlier</blockquote><p>tail</p>",
        content_type=BodyType.Html,
    )
    item.create_reply.post = AsyncMock(return_value=draft)
    item.get = AsyncMock(return_value=draft)
    item.patch = AsyncMock(return_value=draft)

    asyncio.run(mail_reply(message_id="msg-1", body="<p>Thanks</p>", body_type="html"))

    assert _patched_body(item).content == (
        "<p>Thanks</p><p>From: Rebecca</p><blockquote>earlier</blockquote><p>tail</p>"
    )


def test_the_markup_goes_inside_the_body_tag_of_a_full_document(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _draft("RE: Quarterly sync")
    draft.body = SimpleNamespace(
        content=(
            "<html><head><style>p{}</style></head><body dir='ltr'><p></p><hr>"
            '<div id="divRplyFwdMsg" dir="ltr"><b>From:</b> R</div></body></html>'
        ),
        content_type=BodyType.Html,
    )
    item.create_reply.post = AsyncMock(return_value=draft)
    item.get = AsyncMock(return_value=draft)
    item.patch = AsyncMock(return_value=draft)

    asyncio.run(mail_reply(message_id="msg-1", body="<p>Thanks</p>", body_type="html"))

    assert _patched_body(item).content == (
        "<html><head><style>p{}</style></head><body dir='ltr'><p>Thanks</p><hr>"
        '<div id="divRplyFwdMsg" dir="ltr"><b>From:</b> R</div></body></html>'
    )


def test_a_quote_wrapper_with_other_attribute_order_is_still_found(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _draft("RE: Quarterly sync")
    draft.body = SimpleNamespace(
        content="<p></p><div class='x' id='divRplyFwdMsg'><blockquote>q</blockquote></div>",
        content_type=BodyType.Html,
    )
    item.create_reply.post = AsyncMock(return_value=draft)
    item.get = AsyncMock(return_value=draft)
    item.patch = AsyncMock(return_value=draft)

    asyncio.run(mail_reply(message_id="msg-1", body="<p>Thanks</p>", body_type="html"))

    assert _patched_body(item).content == (
        "<p>Thanks</p><div class='x' id='divRplyFwdMsg'><blockquote>q</blockquote></div>"
    )


def test_cc_and_an_html_body_work_together_and_report_the_stored_body(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    created = _with_quote("RE: Quarterly sync")
    stored = _with_quote("RE: Quarterly sync")
    stored.body = SimpleNamespace(content="<p>x</p>", content_type=BodyType.Text)
    item.create_reply.post = AsyncMock(return_value=created)
    item.get = AsyncMock(side_effect=[created, stored])
    item.patch = AsyncMock(return_value=created)

    payload = asyncio.run(
        mail_reply(message_id="msg-1", body="<p>Hi</p>", body_type="html", cc="new@example.com")
    )

    assert item.patch.await_count == 2  # recipients, then the body
    assert payload["draft"]["body_type"] == "text"  # reported from the draft as stored


@pytest.mark.parametrize(
    "lookalike",
    [
        '<div data-id="divRplyFwdMsg">Not the wrapper</div>',
        '<div id="divRplyFwdMsg-extra">Not the wrapper</div>',
        '<div class="divRplyFwdMsg">Not the wrapper</div>',
    ],
)
def test_a_lookalike_of_the_quote_wrapper_cuts_nothing(monkeypatch, lookalike: str) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _draft("RE: Quarterly sync")
    draft.body = SimpleNamespace(
        content=f"<p>before</p>{lookalike}<p>after</p>", content_type=BodyType.Html
    )
    item.create_reply.post = AsyncMock(return_value=draft)
    item.get = AsyncMock(return_value=draft)
    item.patch = AsyncMock(return_value=draft)

    asyncio.run(mail_reply(message_id="msg-1", body="<p>Thanks</p>", body_type="html"))

    assert _patched_body(item).content == f"<p>Thanks</p><p>before</p>{lookalike}<p>after</p>"


def test_a_plain_text_draft_body_is_escaped_before_it_goes_under_the_markup(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    draft = _draft("RE: Quarterly sync")
    draft.body = SimpleNamespace(
        content="From: Rebecca\n<pending> & more\n", content_type=BodyType.Text
    )
    item.create_reply.post = AsyncMock(return_value=draft)
    item.get = AsyncMock(return_value=draft)
    item.patch = AsyncMock(return_value=draft)

    asyncio.run(mail_reply(message_id="msg-1", body="<p>Thanks</p>", body_type="html"))

    body = _patched_body(item)
    assert body.content_type == BodyType.Html
    assert body.content == "<p>Thanks</p>From: Rebecca<br>&lt;pending&gt; &amp; more<br>"
