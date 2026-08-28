"""Multi-recipient draft / update-draft (issue #60)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from msgraph.generated.models.body_type import BodyType

from blumkin.skills.mail import format_draft_human, mail_draft, mail_update_draft


def test_format_draft_human_lists_cc_and_bcc() -> None:
    lines = format_draft_human(
        {
            "draft": {
                "bcc": "secret@example.com",
                "body_type": "text",
                "cc": "sam@example.com",
                "id": "draft-1",
                "subject": "Hi",
                "to": "a@b.com, c@d.com",
            }
        }
    )

    assert lines[0] == "Draft saved: 'Hi' → a@b.com, c@d.com (text)"
    assert lines[2] == "  cc: sam@example.com"
    assert lines[3] == "  bcc: secret@example.com"


def test_mail_draft_accepts_repeatable_and_comma_separated_recipients(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.messages.post = AsyncMock(return_value=SimpleNamespace(id="draft-1", subject="Hi"))

    payload = asyncio.run(
        mail_draft(
            to=("a@b.com", "c@d.com, e@f.com"),
            cc="sam@example.com",
            bcc=("secret@example.com",),
            subject="Hi",
            body="Hello",
        )
    )

    post_await = client.me.messages.post.await_args
    assert post_await is not None
    posted = post_await.args[0]
    assert [r.email_address.address for r in posted.to_recipients] == [
        "a@b.com",
        "c@d.com",
        "e@f.com",
    ]
    assert [r.email_address.address for r in posted.cc_recipients] == ["sam@example.com"]
    assert [r.email_address.address for r in posted.bcc_recipients] == ["secret@example.com"]
    assert payload["draft"]["to"] == "a@b.com, c@d.com, e@f.com"
    assert payload["draft"]["cc"] == "sam@example.com"
    assert payload["draft"]["bcc"] == "secret@example.com"


def test_mail_draft_requires_to() -> None:
    with pytest.raises(ValueError, match="--to"):
        asyncio.run(mail_draft(to="  ", subject="Hi", body="Hello"))


def test_mail_update_draft_omitted_recipients_leave_lists_unchanged(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="Old",
        body=SimpleNamespace(content_type=BodyType.Text, content="old"),
        to_recipients=[SimpleNamespace(email_address=SimpleNamespace(address="a@b.com"))],
        cc_recipients=[SimpleNamespace(email_address=SimpleNamespace(address="sam@example.com"))],
        bcc_recipients=[],
    )
    patched = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="New",
        body=existing.body,
        to_recipients=existing.to_recipients,
        cc_recipients=existing.cc_recipients,
        bcc_recipients=[],
    )
    client = _client(monkeypatch)
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=existing)
    client.me.messages.by_message_id.return_value.patch = AsyncMock(return_value=patched)

    payload = asyncio.run(mail_update_draft(draft_id="draft-1", subject="New"))

    patch_await = client.me.messages.by_message_id.return_value.patch.await_args
    assert patch_await is not None
    posted = patch_await.args[0]
    assert posted.to_recipients is None
    assert posted.cc_recipients is None
    assert posted.bcc_recipients is None
    assert payload["draft"]["to"] == "a@b.com"
    assert payload["draft"]["cc"] == "sam@example.com"


def test_mail_update_draft_rejects_empty_recipient_flags() -> None:
    with pytest.raises(ValueError, match="--to"):
        asyncio.run(mail_update_draft(draft_id="draft-1", to=""))
    with pytest.raises(ValueError, match="--cc"):
        asyncio.run(mail_update_draft(draft_id="draft-1", cc="  ,  "))
    with pytest.raises(ValueError, match="--bcc"):
        asyncio.run(mail_update_draft(draft_id="draft-1", bcc=("",)))


def test_mail_update_draft_replaces_multi_to_and_sets_cc(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="Old",
        body=SimpleNamespace(content_type=BodyType.Text, content="old"),
        to_recipients=[
            SimpleNamespace(email_address=SimpleNamespace(address="a@b.com")),
            SimpleNamespace(email_address=SimpleNamespace(address="c@d.com")),
        ],
        cc_recipients=[],
        bcc_recipients=[],
    )
    patched = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="Old",
        body=existing.body,
        to_recipients=[
            SimpleNamespace(email_address=SimpleNamespace(address="e@f.com")),
            SimpleNamespace(email_address=SimpleNamespace(address="g@h.com")),
        ],
        cc_recipients=[SimpleNamespace(email_address=SimpleNamespace(address="sam@example.com"))],
        bcc_recipients=[],
    )
    client = _client(monkeypatch)
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=existing)
    client.me.messages.by_message_id.return_value.patch = AsyncMock(return_value=patched)

    payload = asyncio.run(
        mail_update_draft(
            draft_id="draft-1",
            to=("e@f.com", "g@h.com"),
            cc="sam@example.com",
        )
    )

    patch_await = client.me.messages.by_message_id.return_value.patch.await_args
    assert patch_await is not None
    posted = patch_await.args[0]
    assert [r.email_address.address for r in posted.to_recipients] == ["e@f.com", "g@h.com"]
    assert [r.email_address.address for r in posted.cc_recipients] == ["sam@example.com"]
    assert posted.bcc_recipients is None
    assert payload["draft"]["to"] == "e@f.com, g@h.com"
    assert payload["draft"]["cc"] == "sam@example.com"


def _client(monkeypatch) -> MagicMock:
    client = MagicMock()
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(client_id="x", default_tz="UTC"),
    )
    return client
