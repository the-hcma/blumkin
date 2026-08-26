"""Mocked tests for mail attachment list/download skills."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from blumkin.skills.mail import (
    MailAttachmentNotFoundError,
    MailAttachmentSkippedError,
    MailMessageNotFoundError,
    format_attachments_human,
    mail_attachments_download,
    mail_attachments_list,
)


def _message_stub(*, message_id: str = "msg-1") -> SimpleNamespace:
    return SimpleNamespace(id=message_id)


def _file_attachment(
    *,
    attachment_id: str = "att-1",
    name: str = "report.docx",
    content: bytes = b"doc-bytes",
    include_bytes: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=attachment_id,
        name=name,
        size=len(content),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        is_inline=False,
        odata_type="#microsoft.graph.fileAttachment",
        content_bytes=content if include_bytes else None,
    )


def _item_attachment(*, attachment_id: str = "att-inline") -> SimpleNamespace:
    return SimpleNamespace(
        id=attachment_id,
        name="Agenda",
        size=0,
        content_type=None,
        is_inline=True,
        odata_type="#microsoft.graph.itemAttachment",
        content_bytes=None,
    )


def test_mail_attachments_list_mocked(monkeypatch) -> None:
    file_att = _file_attachment()
    item_att = _item_attachment()
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=_message_stub())
    client.me.messages.by_message_id.return_value.attachments.get = AsyncMock(
        return_value=SimpleNamespace(value=[file_att, item_att])
    )
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    payload = asyncio.run(mail_attachments_list(message_id="msg-1"))
    assert payload["message_id"] == "msg-1"
    assert payload["attachments"][0]["skipped"] is False
    assert payload["attachments"][1]["skipped"] is True
    assert "itemAttachment" in (payload["attachments"][1]["skip_reason"] or "")
    assert any("report.docx" in line for line in format_attachments_human(payload))


def test_mail_attachments_list_message_not_found(monkeypatch) -> None:
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=None)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    with pytest.raises(MailMessageNotFoundError):
        asyncio.run(mail_attachments_list(message_id="missing"))


def test_mail_attachments_download_content_bytes(tmp_path, monkeypatch) -> None:
    file_att = _file_attachment(content=b"hello")
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=_message_stub())
    client.me.messages.by_message_id.return_value.attachments.get = AsyncMock(
        return_value=SimpleNamespace(value=[file_att])
    )
    client.me.messages.by_message_id.return_value.attachments.by_attachment_id.return_value.get = (
        AsyncMock(return_value=file_att)
    )
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    out = tmp_path / "saved.docx"
    payload = asyncio.run(
        mail_attachments_download(
            message_id="msg-1",
            attachment_id="att-1",
            out=str(out),
        )
    )
    assert out.read_bytes() == b"hello"
    assert payload["saved"][0]["saved_path"] == str(out.resolve())


def test_mail_attachments_download_value_fallback(tmp_path, monkeypatch) -> None:
    file_att = _file_attachment(content=b"fallback", include_bytes=False)
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=_message_stub())
    client.me.messages.by_message_id.return_value.attachments.get = AsyncMock(
        return_value=SimpleNamespace(value=[file_att])
    )
    client.me.messages.by_message_id.return_value.attachments.by_attachment_id.return_value.get = (
        AsyncMock(return_value=file_att)
    )
    client.request_adapter.send_primitive_async = AsyncMock(return_value=b"from-value")
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    out = tmp_path / "saved.docx"
    payload = asyncio.run(
        mail_attachments_download(
            message_id="msg-1",
            attachment_id="att-1",
            out=str(out),
        )
    )
    assert out.read_bytes() == b"from-value"
    assert payload["saved"][0]["size"] == len(b"from-value")
    client.request_adapter.send_primitive_async.assert_awaited_once()


def test_mail_attachments_download_all_skips_item_attachment(tmp_path, monkeypatch) -> None:
    file_att = _file_attachment(attachment_id="att-file", name="a.docx", content=b"A")
    item_att = _item_attachment()
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=_message_stub())
    client.me.messages.by_message_id.return_value.attachments.get = AsyncMock(
        return_value=SimpleNamespace(value=[file_att, item_att])
    )
    client.me.messages.by_message_id.return_value.attachments.by_attachment_id.return_value.get = (
        AsyncMock(return_value=file_att)
    )
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    out_dir = tmp_path / "downloads"
    payload = asyncio.run(
        mail_attachments_download(
            message_id="msg-1",
            download_all=True,
            out=str(out_dir),
        )
    )
    assert len(payload["saved"]) == 1
    assert len(payload["skipped"]) == 1
    assert (out_dir / "a.docx").read_bytes() == b"A"


def test_mail_attachments_download_rejects_missing_attachment(monkeypatch) -> None:
    file_att = _file_attachment()
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=_message_stub())
    client.me.messages.by_message_id.return_value.attachments.get = AsyncMock(
        return_value=SimpleNamespace(value=[file_att])
    )
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    with pytest.raises(MailAttachmentNotFoundError):
        asyncio.run(
            mail_attachments_download(
                message_id="msg-1",
                attachment_id="missing",
                out="out.bin",
            )
        )


def test_mail_attachments_download_rejects_skipped_type(monkeypatch) -> None:
    item_att = _item_attachment()
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=_message_stub())
    client.me.messages.by_message_id.return_value.attachments.get = AsyncMock(
        return_value=SimpleNamespace(value=[item_att])
    )
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    with pytest.raises(MailAttachmentSkippedError):
        asyncio.run(
            mail_attachments_download(
                message_id="msg-1",
                attachment_id="att-inline",
                out="out.bin",
            )
        )
