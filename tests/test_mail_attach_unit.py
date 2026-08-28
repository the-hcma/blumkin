"""Unit tests for ``--attach`` on ``mail draft`` and ``mail update-draft``."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from msgraph.generated.models.body_type import BodyType

from blumkin.skills.mail import (
    _MAX_ATTACHMENT_BYTES,
    MailAttachError,
    format_draft_human,
    mail_draft,
    mail_update_draft,
)


def test_format_draft_human_lists_attachments() -> None:
    lines = format_draft_human(
        {
            "draft": {
                "attachments": [{"id": "att-1", "name": "report.pdf", "size": 12}],
                "body_type": "text",
                "id": "draft-1",
                "subject": "Hi",
                "to": "a@b.com",
            }
        }
    )
    assert lines[-1] == "  attached: 'report.pdf' (12 bytes) id=att-1"


def test_mail_draft_attaches_files_in_order(tmp_path, monkeypatch) -> None:
    first = tmp_path / "first.txt"
    first.write_bytes(b"one")
    second = tmp_path / "second.txt"
    second.write_bytes(b"two")
    client = _client(monkeypatch)
    payload = asyncio.run(
        mail_draft(
            to="a@b.com",
            subject="Hi",
            body="Hello",
            attach=[str(first), str(second)],
        )
    )
    posted = _attachments_posted(client)
    assert [(att.name, att.content_bytes) for att in posted] == [
        ("first.txt", b"one"),
        ("second.txt", b"two"),
    ]
    assert posted[0].odata_type == "#microsoft.graph.fileAttachment"
    assert [item["name"] for item in payload["draft"]["attachments"]] == [
        "first.txt",
        "second.txt",
    ]
    # The attachments hang off the draft that was just created, not some other message.
    client.me.messages.by_message_id.assert_called_with("draft-1")


def test_mail_draft_reports_no_attachments_when_none_were_asked_for(monkeypatch) -> None:
    client = _client(monkeypatch)
    payload = asyncio.run(mail_draft(to="a@b.com", subject="Hi", body="Hello"))
    assert payload["draft"]["attachments"] == []
    client.me.messages.by_message_id.return_value.attachments.post.assert_not_awaited()


def test_mail_draft_sanitizes_the_attachment_name(tmp_path, monkeypatch) -> None:
    source = tmp_path / "we;ird name.txt"
    source.write_bytes(b"x")
    client = _client(monkeypatch)
    asyncio.run(mail_draft(to="a@b.com", subject="Hi", body="Hello", attach=[str(source)]))
    assert _attachments_posted(client)[0].name == "we_ird name.txt"


def test_mail_draft_rejects_a_directory(tmp_path, monkeypatch) -> None:
    _client(monkeypatch)
    with pytest.raises(MailAttachError, match="not a directory"):
        asyncio.run(mail_draft(to="a@b.com", subject="Hi", body="Hello", attach=[str(tmp_path)]))


def test_mail_draft_rejects_a_missing_file(tmp_path, monkeypatch) -> None:
    client = _client(monkeypatch)
    with pytest.raises(MailAttachError, match="not found"):
        asyncio.run(
            mail_draft(
                to="a@b.com",
                subject="Hi",
                body="Hello",
                attach=[str(tmp_path / "nope.txt")],
            )
        )
    # A bad path must not leave a draft behind — the files are read before Graph is called.
    client.me.messages.post.assert_not_awaited()


def test_mail_draft_rejects_an_oversized_file(tmp_path, monkeypatch) -> None:
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * _MAX_ATTACHMENT_BYTES)
    client = _client(monkeypatch)
    with pytest.raises(MailAttachError, match="too large"):
        asyncio.run(mail_draft(to="a@b.com", subject="Hi", body="Hello", attach=[str(source)]))
    client.me.messages.post.assert_not_awaited()


def test_mail_draft_rejects_an_oversized_file_before_reading(tmp_path, monkeypatch) -> None:
    """A multi-GB file must not be buffered into RAM just to refuse it."""
    source = tmp_path / "huge.bin"
    source.write_bytes(b"tiny")  # real bytes are irrelevant; size comes from the stub.

    def _huge_stat(_self):  # noqa: ANN001
        return SimpleNamespace(st_size=_MAX_ATTACHMENT_BYTES)

    monkeypatch.setattr(type(source), "stat", _huge_stat)

    def _boom(_self):  # noqa: ANN001
        raise AssertionError("read_bytes must not run for an oversized file")

    monkeypatch.setattr(type(source), "read_bytes", _boom)
    client = _client(monkeypatch)
    with pytest.raises(MailAttachError, match="too large"):
        asyncio.run(mail_draft(to="a@b.com", subject="Hi", body="Hello", attach=[str(source)]))
    client.me.messages.post.assert_not_awaited()


def test_mail_draft_deletes_the_draft_when_an_upload_fails(tmp_path, monkeypatch) -> None:
    first = tmp_path / "first.txt"
    first.write_bytes(b"one")
    second = tmp_path / "second.txt"
    second.write_bytes(b"two")
    client = _client(monkeypatch)
    posts = client.me.messages.by_message_id.return_value.attachments.post
    posts.side_effect = [
        SimpleNamespace(id="att-first.txt", name="first.txt", size=3),
        RuntimeError("Graph said no"),
    ]
    client.me.messages.by_message_id.return_value.delete = AsyncMock(return_value=None)
    attachments = client.me.messages.by_message_id.return_value.attachments
    attachments.by_attachment_id.return_value.delete = AsyncMock(return_value=None)

    with pytest.raises(RuntimeError, match="Graph said no"):
        asyncio.run(
            mail_draft(
                to="a@b.com",
                subject="Hi",
                body="Hello",
                attach=[str(first), str(second)],
            )
        )

    # The first attachment was rolled back, then the half-built draft was deleted.
    attachments.by_attachment_id.assert_called_with("att-first.txt")
    client.me.messages.by_message_id.return_value.delete.assert_awaited_once()


def test_mail_update_draft_rolls_back_attachments_when_a_later_upload_fails(
    tmp_path, monkeypatch
) -> None:
    first = tmp_path / "first.txt"
    first.write_bytes(b"one")
    second = tmp_path / "second.txt"
    second.write_bytes(b"two")
    client = _draft_client(monkeypatch)
    posts = client.me.messages.by_message_id.return_value.attachments.post
    posts.side_effect = [
        SimpleNamespace(id="att-first.txt", name="first.txt", size=3),
        RuntimeError("Graph said no"),
    ]
    delete = AsyncMock(return_value=None)
    attachments = client.me.messages.by_message_id.return_value.attachments
    attachments.by_attachment_id.return_value.delete = delete

    with pytest.raises(RuntimeError, match="Graph said no"):
        asyncio.run(
            mail_update_draft(
                draft_id="draft-1",
                subject="New",
                attach=[str(first), str(second)],
            )
        )

    delete.assert_awaited_once()
    # Upload runs before PATCH, so a failed batch leaves subject/body/to untouched.
    client.me.messages.by_message_id.return_value.patch.assert_not_awaited()
    # The pre-existing draft stays — only the attachment uploaded in this call is undone.
    client.me.messages.by_message_id.return_value.delete.assert_not_called()


def test_mail_update_draft_accepts_attach_alone(tmp_path, monkeypatch) -> None:
    source = tmp_path / "note.txt"
    source.write_bytes(b"hi")
    client = _draft_client(monkeypatch)
    payload = asyncio.run(mail_update_draft(draft_id="draft-1", attach=[str(source)]))
    assert [item["name"] for item in payload["draft"]["attachments"]] == ["note.txt"]
    assert payload["draft"]["subject"] == "Old"
    # Attach-only keeps Graph recipients via `_recipient_field` (no PATCH).
    assert payload["draft"]["to"] == "a@b.com, c@d.com"
    assert payload["draft"]["cc"] == "sam@example.com"
    assert payload["draft"]["bcc"] == "secret@example.com"
    # Nothing else changed, so there is nothing to PATCH.
    client.me.messages.by_message_id.return_value.patch.assert_not_awaited()


def test_mail_update_draft_attaches_alongside_a_patch(tmp_path, monkeypatch) -> None:
    source = tmp_path / "note.txt"
    source.write_bytes(b"hi")
    client = _draft_client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    order: list[str] = []
    original_post = item.attachments.post
    original_patch = item.patch

    async def _post(att):
        order.append("upload")
        return await original_post(att)

    async def _patch(msg):
        order.append("patch")
        return await original_patch(msg)

    item.attachments.post = AsyncMock(side_effect=_post)
    item.patch = AsyncMock(side_effect=_patch)
    payload = asyncio.run(
        mail_update_draft(draft_id="draft-1", subject="New", attach=[str(source)])
    )
    assert payload["draft"]["subject"] == "New"
    assert [item["name"] for item in payload["draft"]["attachments"]] == ["note.txt"]
    # Attachments land before the field PATCH so a failed upload cannot leave a half-edit.
    assert order == ["upload", "patch"]


def test_mail_draft_rejects_a_non_regular_file(tmp_path, monkeypatch) -> None:
    """Devices and FIFOs report st_size 0 and would hang or OOM on read_bytes."""
    source = tmp_path / "pipe-or-device"
    source.write_bytes(b"x")
    monkeypatch.setattr(type(source), "is_file", lambda _self: False)
    monkeypatch.setattr(type(source), "is_dir", lambda _self: False)
    monkeypatch.setattr(type(source), "exists", lambda _self: True)
    _client(monkeypatch)
    with pytest.raises(MailAttachError, match="regular file"):
        asyncio.run(mail_draft(to="a@b.com", subject="Hi", body="Hello", attach=[str(source)]))


def test_mail_update_draft_validates_before_uploading(tmp_path, monkeypatch) -> None:
    """A usage error must not leave a newly uploaded attachment behind."""
    source = tmp_path / "note.txt"
    source.write_bytes(b"hi")
    client = _draft_client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(
        return_value=SimpleNamespace(
            id="draft-1",
            is_draft=True,
            subject="Old",
            body=SimpleNamespace(content_type=BodyType.Text, content="old"),
            to_recipients=[
                SimpleNamespace(email_address=SimpleNamespace(address="a@b.com")),
            ],
            cc_recipients=[],
            bcc_recipients=[],
        )
    )

    with pytest.raises(ValueError, match="--subject must be non-empty"):
        asyncio.run(mail_update_draft(draft_id="draft-1", subject="  ", attach=[str(source)]))

    item.attachments.post.assert_not_awaited()
    item.patch.assert_not_awaited()


def test_mail_update_draft_rolls_back_attachments_when_patch_fails(tmp_path, monkeypatch) -> None:
    source = tmp_path / "note.txt"
    source.write_bytes(b"hi")
    client = _draft_client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.patch = AsyncMock(side_effect=RuntimeError("patch failed"))
    delete = AsyncMock(return_value=None)
    item.attachments.by_attachment_id.return_value.delete = delete

    with pytest.raises(RuntimeError, match="patch failed"):
        asyncio.run(mail_update_draft(draft_id="draft-1", subject="New", attach=[str(source)]))

    delete.assert_awaited_once()


def test_mail_update_draft_still_requires_a_field() -> None:
    with pytest.raises(ValueError, match="at least one"):
        asyncio.run(mail_update_draft(draft_id="draft-1"))


def _attachments_posted(client: MagicMock) -> list:
    post = client.me.messages.by_message_id.return_value.attachments.post
    return [call.args[0] for call in post.await_args_list]


def _client(monkeypatch) -> MagicMock:
    client = MagicMock()
    client.me.messages.post = AsyncMock(return_value=SimpleNamespace(id="draft-1", subject="Hi"))
    client.me.messages.by_message_id.return_value.attachments.post = AsyncMock(
        side_effect=lambda att: SimpleNamespace(
            id=f"att-{att.name}", name=att.name, size=len(att.content_bytes)
        )
    )
    _patch_graph(monkeypatch, client)
    return client


def _draft_client(monkeypatch) -> MagicMock:
    existing = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="Old",
        body=SimpleNamespace(content_type=BodyType.Text, content="old"),
        bcc_recipients=[
            SimpleNamespace(email_address=SimpleNamespace(address="secret@example.com")),
        ],
        cc_recipients=[
            SimpleNamespace(email_address=SimpleNamespace(address="sam@example.com")),
        ],
        to_recipients=[
            SimpleNamespace(email_address=SimpleNamespace(address="a@b.com")),
            SimpleNamespace(email_address=SimpleNamespace(address="c@d.com")),
        ],
    )
    client = MagicMock()
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(return_value=existing)
    item.patch = AsyncMock(
        return_value=SimpleNamespace(
            id="draft-1",
            is_draft=True,
            subject="New",
            body=existing.body,
            bcc_recipients=existing.bcc_recipients,
            cc_recipients=existing.cc_recipients,
            to_recipients=existing.to_recipients,
        )
    )
    item.attachments.post = AsyncMock(
        side_effect=lambda att: SimpleNamespace(
            id=f"att-{att.name}", name=att.name, size=len(att.content_bytes)
        )
    )
    _patch_graph(monkeypatch, client)
    return client


def _patch_graph(monkeypatch, client: MagicMock) -> None:
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
