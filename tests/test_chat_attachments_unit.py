"""Mocked tests for chat attachment list/download skills."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from blumkin.skills.chat import (
    ChatAttachmentNotFoundError,
    ChatAttachmentScopeError,
    ChatAttachmentSkippedError,
    ChatMessageNotFoundError,
    chat_attachments_download,
    chat_attachments_list,
    format_attachments_download_human,
    format_attachments_human,
)


def test_chat_attachments_download_all_writes_files(monkeypatch, tmp_path) -> None:
    message = _message_stub(
        [
            _reference_attachment(attachment_id="att-1", name="report.docx"),
            _reference_attachment(attachment_id="att-2", name="report.docx"),
            _card_attachment(),
        ]
    )
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value.get = (
        AsyncMock(return_value=message)
    )
    _configure(monkeypatch, client, scopes=["Chat.Read", "Files.Read"])
    _stub_download(monkeypatch)
    out_dir = tmp_path / "downloads"
    payload = asyncio.run(
        chat_attachments_download(
            chat_id="chat-1",
            download_all=True,
            message_id="msg-1",
            out=str(out_dir),
        )
    )
    saved_names = sorted(item["name"] for item in payload["saved"])
    assert saved_names == ["report.docx", "report.docx"]
    written = sorted(path.name for path in out_dir.iterdir())
    assert written == ["report.docx", "report_2.docx"]
    assert len(payload["skipped"]) == 1
    assert any("skipped" in line for line in format_attachments_download_human(payload))


def test_chat_attachments_download_attachment_not_found(monkeypatch, tmp_path) -> None:
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value.get = (
        AsyncMock(return_value=_message_stub([_reference_attachment()]))
    )
    _configure(monkeypatch, client, scopes=["Chat.Read", "Files.Read"])
    with pytest.raises(ChatAttachmentNotFoundError):
        asyncio.run(
            chat_attachments_download(
                attachment_id="nope",
                chat_id="chat-1",
                message_id="msg-1",
                out=str(tmp_path / "out.docx"),
            )
        )


def test_chat_attachments_download_card_is_usage_error(monkeypatch, tmp_path) -> None:
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value.get = (
        AsyncMock(return_value=_message_stub([_card_attachment()]))
    )
    _configure(monkeypatch, client, scopes=["Chat.Read", "Files.Read"])
    with pytest.raises(ChatAttachmentSkippedError):
        asyncio.run(
            chat_attachments_download(
                attachment_id="att-card",
                chat_id="chat-1",
                message_id="msg-1",
                out=str(tmp_path / "out.json"),
            )
        )


def test_chat_attachments_download_graph_403_keeps_share_url(monkeypatch, tmp_path) -> None:
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value.get = (
        AsyncMock(return_value=_message_stub([_reference_attachment()]))
    )
    _configure(monkeypatch, client, scopes=["Chat.Read", "Files.Read"])

    class _Denied(Exception):
        response_status_code = 403

    async def _boom(_client, _url):
        raise _Denied("access denied")

    monkeypatch.setattr("blumkin.skills.chat._fetch_shared_item_bytes", _boom)
    with pytest.raises(ChatAttachmentScopeError) as excinfo:
        asyncio.run(
            chat_attachments_download(
                attachment_id="att-1",
                chat_id="chat-1",
                message_id="msg-1",
                out=str(tmp_path / "out.docx"),
            )
        )
    assert _CONTENT_URL in str(excinfo.value)


def test_chat_attachments_download_missing_files_scope(monkeypatch, tmp_path) -> None:
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value.get = (
        AsyncMock(return_value=_message_stub([_reference_attachment()]))
    )
    _configure(monkeypatch, client, scopes=["Chat.Read"])
    with pytest.raises(ChatAttachmentScopeError) as excinfo:
        asyncio.run(
            chat_attachments_download(
                attachment_id="att-1",
                chat_id="chat-1",
                message_id="msg-1",
                out=str(tmp_path / "out.docx"),
            )
        )
    # The share URL must be in the error so the operator can fall back to a browser.
    assert _CONTENT_URL in str(excinfo.value)
    assert "files_scopes" in str(excinfo.value)


def test_chat_attachments_download_sanitizes_control_chars_in_share_url(
    monkeypatch, tmp_path
) -> None:
    hostile = "https://contoso.sharepoint.com/x\x1b[2Kspoofed"
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value.get = (
        AsyncMock(return_value=_message_stub([_reference_attachment(content_url=hostile)]))
    )
    _configure(monkeypatch, client, scopes=["Chat.Read"])
    with pytest.raises(ChatAttachmentScopeError) as excinfo:
        asyncio.run(
            chat_attachments_download(
                attachment_id="att-1",
                chat_id="chat-1",
                message_id="msg-1",
                out=str(tmp_path / "out.docx"),
            )
        )
    assert "\x1b" not in str(excinfo.value)


def test_chat_attachments_download_directory_intent_over_file_is_usage_error(
    monkeypatch, tmp_path
) -> None:
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value.get = (
        AsyncMock(return_value=_message_stub([_reference_attachment()]))
    )
    _configure(monkeypatch, client, scopes=["Chat.Read", "Files.Read"])
    _stub_download(monkeypatch)
    existing_file = tmp_path / "report.docx"
    existing_file.write_bytes(b"existing")
    # Trailing slash asks for a directory, but the path is a regular file.
    with pytest.raises(ValueError, match="--out must be a directory"):
        asyncio.run(
            chat_attachments_download(
                attachment_id="att-1",
                chat_id="chat-1",
                message_id="msg-1",
                out=f"{existing_file}/",
            )
        )
    assert existing_file.read_bytes() == b"existing"


def test_chat_attachments_download_refuses_existing_out_file(monkeypatch, tmp_path) -> None:
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value.get = (
        AsyncMock(return_value=_message_stub([_reference_attachment()]))
    )
    _configure(monkeypatch, client, scopes=["Chat.Read", "Files.Read"])
    _stub_download(monkeypatch)
    existing = tmp_path / "out.docx"
    existing.write_bytes(b"existing")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        asyncio.run(
            chat_attachments_download(
                attachment_id="att-1",
                chat_id="chat-1",
                message_id="msg-1",
                out=str(existing),
            )
        )
    assert existing.read_bytes() == b"existing"


def test_chat_attachments_download_rejects_both_selectors(tmp_path) -> None:
    with pytest.raises(ValueError, match="exactly one of --attachment-id or --all"):
        asyncio.run(
            chat_attachments_download(
                attachment_id="att-1",
                chat_id="chat-1",
                download_all=True,
                message_id="msg-1",
                out=str(tmp_path / "out"),
            )
        )


def test_chat_attachments_download_single_into_directory_avoids_overwrite(
    monkeypatch, tmp_path
) -> None:
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value.get = (
        AsyncMock(return_value=_message_stub([_reference_attachment()]))
    )
    _configure(monkeypatch, client, scopes=["Chat.Read", "Files.ReadWrite"])
    _stub_download(monkeypatch, b"new-bytes")
    out_dir = tmp_path / "downloads"
    out_dir.mkdir()
    (out_dir / "report.docx").write_bytes(b"existing")
    payload = asyncio.run(
        chat_attachments_download(
            attachment_id="att-1",
            chat_id="chat-1",
            message_id="msg-1",
            out=f"{out_dir}/",
        )
    )
    assert (out_dir / "report.docx").read_bytes() == b"existing"
    assert (out_dir / "report_2.docx").read_bytes() == b"new-bytes"
    assert payload["saved"][0]["saved_path"].endswith("report_2.docx")


def test_chat_attachments_download_uses_shares_url(monkeypatch, tmp_path) -> None:
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value.get = (
        AsyncMock(return_value=_message_stub([_reference_attachment()]))
    )
    _configure(monkeypatch, client, scopes=["Chat.Read", "Files.Read"])
    requested = _stub_download(monkeypatch)
    out_file = tmp_path / "nested" / "out.docx"
    payload = asyncio.run(
        chat_attachments_download(
            attachment_id="att-1",
            chat_id="chat-1",
            message_id="msg-1",
            out=str(out_file),
        )
    )
    assert requested == [_CONTENT_URL]
    assert out_file.read_bytes() == b"chat-file-bytes"
    assert payload["saved"][0]["size"] == len(b"chat-file-bytes")


def test_chat_attachments_latest_scans_for_attachments(monkeypatch) -> None:
    plain = _message_stub([], message_id="msg-plain")
    with_files = _message_stub([_reference_attachment()], message_id="msg-2")
    first_page = SimpleNamespace(
        odata_next_link="https://graph.microsoft.com/v1.0/next",
        value=[plain],
    )
    second_page = SimpleNamespace(odata_next_link=None, value=[with_files])
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.get = AsyncMock(return_value=first_page)
    client.me.chats.by_chat_id.return_value.messages.with_url.return_value.get = AsyncMock(
        return_value=second_page
    )
    _configure(monkeypatch, client)
    payload = asyncio.run(chat_attachments_list(chat_id="chat-1", latest=True))
    assert payload["message_id"] == "msg-2"
    assert payload["attachments"][0]["id"] == "att-1"


def test_chat_attachments_latest_without_attachments_is_not_found(monkeypatch) -> None:
    page = SimpleNamespace(odata_next_link=None, value=[_message_stub([])])
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.get = AsyncMock(return_value=page)
    _configure(monkeypatch, client)
    with pytest.raises(ChatMessageNotFoundError):
        asyncio.run(chat_attachments_list(chat_id="chat-1", latest=True))


def test_chat_attachments_list_classifies_types(monkeypatch) -> None:
    onedrive = _reference_attachment(
        attachment_id="att-od",
        content_url="https://contoso-my.sharepoint.com/personal/me/Documents/notes.txt",
        name="notes.txt",
    )
    reference_message = SimpleNamespace(
        content_type="messageReference",
        content_url=None,
        id="att-ref",
        name=None,
    )
    message = _message_stub(
        [_reference_attachment(), onedrive, _card_attachment(), reference_message]
    )
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value.get = (
        AsyncMock(return_value=message)
    )
    _configure(monkeypatch, client)
    payload = asyncio.run(chat_attachments_list(chat_id="chat-1", message_id="msg-1"))
    by_id = {item["id"]: item for item in payload["attachments"]}
    assert by_id["att-1"]["downloadable"] is True
    assert by_id["att-1"]["source"] == "sharepoint"
    assert by_id["att-od"]["source"] == "onedrive"
    assert by_id["att-card"]["downloadable"] is False
    assert "card" in by_id["att-card"]["skip_reason"]
    assert by_id["att-ref"]["downloadable"] is False
    lines = format_attachments_human(payload)
    assert any("report.docx" in line for line in lines)
    assert any("not downloadable" in line for line in lines)


def test_chat_attachments_list_message_not_found(monkeypatch) -> None:
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value.get = (
        AsyncMock(return_value=None)
    )
    _configure(monkeypatch, client)
    with pytest.raises(ChatMessageNotFoundError):
        asyncio.run(chat_attachments_list(chat_id="chat-1", message_id="missing"))


def test_chat_attachments_list_requires_one_message_selector() -> None:
    with pytest.raises(ValueError, match="exactly one of --message-id or --latest"):
        asyncio.run(chat_attachments_list(chat_id="chat-1"))
    with pytest.raises(ValueError, match="exactly one of --message-id or --latest"):
        asyncio.run(chat_attachments_list(chat_id="chat-1", latest=True, message_id="msg-1"))


def test_chat_attachments_resolves_chat_by_display_name(monkeypatch) -> None:
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value.get = (
        AsyncMock(return_value=_message_stub([_reference_attachment()]))
    )
    _configure(monkeypatch, client)

    async def _fake_find(*, with_name: str, config=None):
        return {
            "items": [
                {"chat_type": "oneOnOne", "id": "chat-99", "members": ["Ada"], "topic": None}
            ],
            "partial": False,
            "query": with_name,
            "skipped": 0,
        }

    monkeypatch.setattr("blumkin.skills.chat.chat_find", _fake_find)
    payload = asyncio.run(chat_attachments_list(message_id="msg-1", with_name="Ada"))
    assert payload["chat_id"] == "chat-99"


def test_sharing_token_matches_graph_encoding() -> None:
    from blumkin.skills.chat import _sharing_token

    token = _sharing_token(_CONTENT_URL)
    assert token.startswith("u!")
    assert "=" not in token
    padded = token[2:] + "=" * (-len(token[2:]) % 4)
    assert base64.urlsafe_b64decode(padded).decode() == _CONTENT_URL


_CONTENT_URL = "https://contoso.sharepoint.com/sites/team/Shared%20Documents/report.docx"


def _card_attachment(*, attachment_id: str = "att-card") -> SimpleNamespace:
    return SimpleNamespace(
        content_type="application/vnd.microsoft.card.adaptive",
        content_url=None,
        id=attachment_id,
        name=None,
    )


def _configure(monkeypatch, client, *, scopes: list[str] | None = None) -> None:
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(client_id="x", default_tz="UTC"),
    )
    monkeypatch.setattr(
        "blumkin.skills.chat.effective_scopes",
        lambda _cfg: scopes if scopes is not None else ["Chat.Read"],
    )


def _message_stub(attachments: list[SimpleNamespace], *, message_id: str = "msg-1"):
    return SimpleNamespace(
        attachments=attachments,
        body=SimpleNamespace(content="see attached", content_type=None),
        created_date_time="2026-08-26T12:00:00Z",
        from_=None,
        id=message_id,
        message_type="message",
    )


def _reference_attachment(
    *,
    attachment_id: str = "att-1",
    content_url: str = _CONTENT_URL,
    name: str | None = "report.docx",
) -> SimpleNamespace:
    return SimpleNamespace(
        content_type="reference",
        content_url=content_url,
        id=attachment_id,
        name=name,
    )


def _stub_download(monkeypatch, content: bytes = b"chat-file-bytes") -> list[str]:
    """Record the sharing URLs the shares-API fetch was asked for."""
    requested: list[str] = []

    async def _fake_fetch(_client, content_url: str) -> bytes:
        requested.append(content_url)
        return content

    monkeypatch.setattr("blumkin.skills.chat._fetch_shared_item_bytes", _fake_fetch)
    return requested
