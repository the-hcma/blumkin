"""Line breaks in a plain-text body must survive the write to Graph / Gmail.

A bare-LF body is accepted by both backends but rendered with its line breaks
collapsed (Gmail parses the `raw` message as RFC 5322; Graph's text -> HTML
conversion keys on CRLF). See the Gmail half in ``test_google_mail_writes_unit``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from msgraph.generated.models.body_type import BodyType

from blumkin.skills.mail import _compose_item_body, mail_draft, mail_update_draft


def test_compose_item_body_forces_crlf_on_text() -> None:
    body = _compose_item_body(BodyType.Text, "one\ntwo\r\nthree\rfour\n\nsix")
    assert body.content_type == BodyType.Text
    assert body.content == "one\r\ntwo\r\nthree\r\nfour\r\n\r\nsix"


def test_compose_item_body_leaves_html_untouched() -> None:
    body = _compose_item_body(BodyType.Html, "<p>one</p>\n<p>two</p>")
    assert body.content == "<p>one</p>\n<p>two</p>"


def _client(monkeypatch) -> MagicMock:
    client = MagicMock()
    client.me.messages.post = AsyncMock(return_value=SimpleNamespace(id="draft-1", subject="S"))
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(client_id="x", default_tz="UTC"),
    )
    return client


def test_mail_draft_text_body_goes_to_graph_with_crlf(monkeypatch) -> None:
    client = _client(monkeypatch)
    asyncio.run(
        mail_draft(
            to="a@b.com",
            subject="S",
            body="para one\n\npara two\n1. a\n2. b",
            body_type="text",
        )
    )
    posted = client.me.messages.post.await_args.args[0]
    assert posted.body.content_type == BodyType.Text
    assert "\n" not in posted.body.content.replace("\r\n", "")
    assert posted.body.content == "para one\r\n\r\npara two\r\n1. a\r\n2. b"


def test_mail_draft_html_body_is_not_reflowed(monkeypatch) -> None:
    client = _client(monkeypatch)
    asyncio.run(mail_draft(to="a@b.com", subject="S", body="<p>a</p>\n<p>b</p>", body_type="html"))
    posted = client.me.messages.post.await_args.args[0]
    assert posted.body.content_type == BodyType.Html
    assert posted.body.content == "<p>a</p>\n<p>b</p>"


def test_mail_update_draft_text_body_patched_with_crlf(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="S",
        body=SimpleNamespace(content_type=BodyType.Text, content="old"),
        to_recipients=[SimpleNamespace(email_address=SimpleNamespace(address="a@b.com"))],
        cc_recipients=None,
        bcc_recipients=None,
    )
    client = MagicMock()
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(return_value=existing)
    item.patch = AsyncMock(return_value=existing)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(client_id="x", default_tz="UTC"),
    )

    asyncio.run(
        mail_update_draft(draft_id="draft-1", body="new line one\nnew line two", body_type="text")
    )

    assert item.patch.await_args is not None
    patched = item.patch.await_args.args[0]
    assert patched.body.content == "new line one\r\nnew line two"
