"""Hermetic tests for Google Chat send / edit / delete and attachments."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.providers.google_auth import GOOGLE_SCOPES
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.skills.chat import (
    ChatAttachmentNotFoundError,
    ChatAttachmentSkippedError,
    ChatMessageNotFoundError,
)

_GOOGLE_CHAT = "blumkin.providers.google.chat"


def _message(name: str, text: str = "hi", attachments: list[dict] | None = None) -> dict:
    msg: dict = {
        "name": name,
        "text": text,
        "createTime": "2026-09-01T12:00:00Z",
        "sender": {"displayName": "Vivek", "name": "users/1"},
    }
    if attachments is not None:
        msg["attachments"] = attachments
    return msg


def _file_attachment(resource: str, content_name: str) -> dict:
    return {
        "name": f"att/{content_name}",
        "contentName": content_name,
        "contentType": "application/pdf",
        "attachmentDataRef": {"resourceName": resource},
        "downloadUri": "https://chat.google.com/dl",
    }


def _drive_attachment(content_name: str) -> dict:
    return {
        "name": f"att/{content_name}",
        "contentName": content_name,
        "contentType": "application/vnd.google-apps.document",
        "driveDataRef": {"driveFileId": "drive-1"},
    }


def test_chat_send_posts_to_the_resolved_space(tmp_path: Path) -> None:
    service = _service(created=_message("spaces/AAA/messages/9", "hello there"))
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_send(
                text="  hello there  ", chat_id="spaces/AAA"
            )
        )
    assert payload["message"]["body_text"] == "hello there"
    service.spaces.return_value.messages.return_value.create.assert_called_once_with(
        parent="spaces/AAA", body={"text": "hello there"}
    )


def test_chat_send_requires_text_and_exactly_one_target(tmp_path: Path) -> None:
    provider = GoogleWorkspaceProvider(_cfg(tmp_path))
    with _patched(_service()):
        with pytest.raises(ValueError, match="--text must be non-empty"):
            asyncio.run(provider.chat_send(text="   ", chat_id="spaces/AAA"))
        with pytest.raises(ValueError, match="exactly one of --with or --chat-id"):
            asyncio.run(provider.chat_send(text="hi"))


def test_chat_edit_patches_only_the_text_field(tmp_path: Path) -> None:
    service = _service(patched=_message("spaces/AAA/messages/9", "corrected"))
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_edit(
                chat_id="spaces/AAA", message_id="spaces/AAA/messages/9", text="corrected"
            )
        )
    assert payload["message"]["body_text"] == "corrected"
    kwargs = service.spaces.return_value.messages.return_value.patch.call_args.kwargs
    assert kwargs["updateMask"] == "text"
    assert kwargs["body"] == {"text": "corrected"}


def test_chat_delete_removes_the_message(tmp_path: Path) -> None:
    service = _service()
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_delete(
                chat_id="spaces/AAA", message_id="spaces/AAA/messages/9"
            )
        )
    assert payload == {"chat_id": "spaces/AAA", "deleted": "spaces/AAA/messages/9"}
    service.spaces.return_value.messages.return_value.delete.assert_called_once_with(
        name="spaces/AAA/messages/9"
    )


def test_chat_attachments_list_marks_drive_files_as_not_downloadable(tmp_path: Path) -> None:
    """A Drive-backed attachment has no bytes on the Chat media endpoint."""
    service = _service(
        message=_message(
            "spaces/AAA/messages/9",
            attachments=[_file_attachment("res-1", "brief.pdf"), _drive_attachment("notes.gdoc")],
        )
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_attachments_list(
                chat_id="spaces/AAA", message_id="spaces/AAA/messages/9"
            )
        )
    by_name = {a["name"]: a for a in payload["attachments"]}
    assert by_name["brief.pdf"]["downloadable"] is True
    assert by_name["brief.pdf"]["id"] == "res-1"
    assert by_name["notes.gdoc"]["downloadable"] is False
    assert "Drive" in by_name["notes.gdoc"]["skip_reason"]
    assert by_name["notes.gdoc"]["source"] == "drive"


def test_chat_attachments_list_requires_exactly_one_message_selector(tmp_path: Path) -> None:
    with _patched(_service()), pytest.raises(ValueError, match="exactly one of --message-id"):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_attachments_list(chat_id="spaces/AAA")
        )


def test_chat_attachments_list_latest_scans_back_for_one_with_files(tmp_path: Path) -> None:
    service = _service()
    service.spaces.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [
            _message("spaces/AAA/messages/9", attachments=[]),
            _message("spaces/AAA/messages/8", attachments=[_file_attachment("res-1", "a.pdf")]),
        ]
    }
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_attachments_list(
                chat_id="spaces/AAA", latest=True
            )
        )
    assert payload["message_id"] == "spaces/AAA/messages/8"
    # The Chat discovery document lists ASC/DESC in caps; a lowercase "desc" is a
    # 400 INVALID_ARGUMENT that no mock-based test would otherwise catch.
    kwargs = service.spaces.return_value.messages.return_value.list.call_args.kwargs
    assert kwargs["orderBy"] == "createTime DESC"


def test_chat_attachments_list_latest_reports_when_nothing_has_files(tmp_path: Path) -> None:
    service = _service()
    service.spaces.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [_message("spaces/AAA/messages/9", attachments=[])]
    }
    with _patched(service), pytest.raises(ChatMessageNotFoundError, match="no message with attach"):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_attachments_list(
                chat_id="spaces/AAA", latest=True
            )
        )


def test_chat_attachments_download_writes_one_file(tmp_path: Path) -> None:
    service = _service(
        message=_message(
            "spaces/AAA/messages/9", attachments=[_file_attachment("res-1", "brief.pdf")]
        ),
        media=b"PDFBYTES",
    )
    dest = tmp_path / "saved.pdf"
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_attachments_download(
                chat_id="spaces/AAA",
                message_id="spaces/AAA/messages/9",
                attachment_id="res-1",
                out=str(dest),
            )
        )
    assert dest.read_bytes() == b"PDFBYTES"
    assert payload["saved"][0]["name"] == "brief.pdf"
    assert payload["saved"][0]["size"] == len(b"PDFBYTES")


def test_chat_attachments_download_refuses_a_drive_file(tmp_path: Path) -> None:
    service = _service(
        message=_message("spaces/AAA/messages/9", attachments=[_drive_attachment("notes.gdoc")])
    )
    with _patched(service), pytest.raises(ChatAttachmentSkippedError, match="Drive"):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_attachments_download(
                chat_id="spaces/AAA",
                message_id="spaces/AAA/messages/9",
                attachment_id="att/notes.gdoc",
                out=str(tmp_path / "x"),
            )
        )


def test_chat_attachments_download_all_refuses_an_empty_result(tmp_path: Path) -> None:
    """Exit 0 with an empty directory would read as a successful download."""
    service = _service(
        message=_message("spaces/AAA/messages/9", attachments=[_drive_attachment("notes.gdoc")])
    )
    with _patched(service), pytest.raises(ChatAttachmentNotFoundError, match="no downloadable"):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_attachments_download(
                chat_id="spaces/AAA",
                message_id="spaces/AAA/messages/9",
                download_all=True,
                out=str(tmp_path / "dump"),
            )
        )


def test_chat_attachments_download_unknown_id(tmp_path: Path) -> None:
    service = _service(
        message=_message(
            "spaces/AAA/messages/9", attachments=[_file_attachment("res-1", "brief.pdf")]
        )
    )
    with (
        _patched(service),
        pytest.raises(ChatAttachmentNotFoundError, match="attachment not found"),
    ):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_attachments_download(
                chat_id="spaces/AAA",
                message_id="spaces/AAA/messages/9",
                attachment_id="nope",
                out=str(tmp_path / "x"),
            )
        )


def test_require_message_maps_404_to_not_found(tmp_path: Path) -> None:
    service = _service()
    service.spaces.return_value.messages.return_value.get.return_value.execute.side_effect = (
        HttpError(httplib2.Response({"status": 404}), b"{}", uri="x")
    )
    with _patched(service), pytest.raises(ChatMessageNotFoundError):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_attachments_list(
                chat_id="spaces/AAA", message_id="spaces/AAA/messages/missing"
            )
        )


def test_google_scopes_include_chat_write() -> None:
    assert "https://www.googleapis.com/auth/chat.messages" in GOOGLE_SCOPES


def _cfg(config_dir: Path) -> BlumkinConfig:
    oauth = config_dir / "desktop-client.json"
    if not oauth.is_file():
        oauth.write_text(
            '{"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "s"}}'
        )
    return BlumkinConfig(
        client_id="id.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=oauth,
        graph_timeout_seconds=60.0,
        legacy_flat=True,
        mail_signature=MailSignatureConfig(),
        profile="default",
        provider=ProviderKind.GOOGLE,
        tags=(),
        tenant_id="",
        wo1162425_scopes=False,
    )


def _patched(service: MagicMock):
    return patch.multiple(
        _GOOGLE_CHAT,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    )


def _service(
    *,
    message: dict | None = None,
    created: dict | None = None,
    patched: dict | None = None,
    media: bytes = b"data",
) -> MagicMock:
    service = MagicMock()
    messages = service.spaces.return_value.messages.return_value
    messages.get.return_value.execute.return_value = message or _message("spaces/AAA/messages/9")
    messages.create.return_value.execute.return_value = created or _message("spaces/AAA/messages/9")
    messages.patch.return_value.execute.return_value = patched or _message("spaces/AAA/messages/9")
    messages.delete.return_value.execute.return_value = {}
    messages.list.return_value.execute.return_value = {"messages": []}
    service.media.return_value.download_media.return_value.execute.return_value = media
    return service


def test_chat_send_resolves_a_name_to_one_space(tmp_path: Path) -> None:
    service = _service(created=_message("spaces/AAA/messages/9", "hi"))
    service.spaces.return_value.list.return_value.execute.return_value = {
        "spaces": [{"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"}]
    }
    service.spaces.return_value.members.return_value.list.return_value.execute.return_value = {
        "memberships": [{"member": {"displayName": "Vivek Kumar"}}]
    }
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_send(text="hi", with_name="vivek")
        )
    assert payload["chat"]["id"] == "spaces/AAA"
    assert payload["query"] == "vivek"
    assert (
        service.spaces.return_value.messages.return_value.create.call_args.kwargs["parent"]
        == "spaces/AAA"
    )


def test_chat_send_fails_closed_on_ambiguous_partial_and_missing_names(tmp_path: Path) -> None:
    """The #121-parity guarantee: never guess which conversation was meant."""
    provider = GoogleWorkspaceProvider(_cfg(tmp_path))

    def _with_spaces(spaces, members, errors=None):
        service = _service()
        service.spaces.return_value.list.return_value.execute.return_value = {"spaces": spaces}

        def _members(*, parent, pageSize, pageToken):  # noqa: N803
            request = MagicMock()
            err = (errors or {}).get(parent)
            if err is not None:
                request.execute.side_effect = err
            else:
                request.execute.return_value = {
                    "memberships": [{"member": {"displayName": n}} for n in members.get(parent, [])]
                }
            return request

        service.spaces.return_value.members.return_value.list.side_effect = _members
        return service

    two = [
        {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
        {"name": "spaces/BBB", "spaceType": "DIRECT_MESSAGE"},
    ]
    ambiguous = _with_spaces(two, {"spaces/AAA": ["Vivek Kumar"], "spaces/BBB": ["Vivek Rao"]})
    with _patched(ambiguous), pytest.raises(ValueError, match="ambiguous chat match"):
        asyncio.run(provider.chat_send(text="hi", with_name="vivek"))

    partial = _with_spaces(
        two,
        {"spaces/BBB": ["Vivek Kumar"]},
        {"spaces/AAA": HttpError(httplib2.Response({"status": 403}), b"{}", uri="x")},
    )
    with _patched(partial), pytest.raises(ValueError, match="is partial"):
        asyncio.run(provider.chat_send(text="hi", with_name="vivek"))

    none = _with_spaces(
        [{"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"}], {"spaces/AAA": ["Ada"]}
    )
    with _patched(none), pytest.raises(LookupError, match="no chat matched"):
        asyncio.run(provider.chat_send(text="hi", with_name="vivek"))


def test_download_attachment_falls_back_to_the_plain_media_method(tmp_path: Path) -> None:
    """Whether discovery generates download_media or download is not ours to guess."""
    service = _service(
        message=_message(
            "spaces/AAA/messages/9", attachments=[_file_attachment("res-1", "brief.pdf")]
        )
    )
    media = MagicMock(spec=["download"])
    media.download.return_value.execute.return_value = b"BYTES"
    service.media.return_value = media
    dest = tmp_path / "x.pdf"
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_attachments_download(
                chat_id="spaces/AAA",
                message_id="spaces/AAA/messages/9",
                attachment_id="res-1",
                out=str(dest),
            )
        )
    assert dest.read_bytes() == b"BYTES"
    media.download.assert_called_once_with(resourceName="res-1")


def test_download_prefers_the_media_variant_that_returns_bytes(tmp_path: Path) -> None:
    """`media.download` returns a JSON Media object; only `download_media` yields bytes.

    Chat's discovery document sets supportsMediaDownload on media.download, so
    google-api-python-client generates both, and only the `_media` variant appends
    `?alt=media`. Preferring it is what makes a real download return a file.
    """
    service = _service(
        message=_message("spaces/AAA/messages/9", attachments=[_file_attachment("res-1", "b.pdf")])
    )
    media = service.media.return_value
    media.download.return_value.execute.return_value = {"resourceName": "res-1"}
    media.download_media.return_value.execute.return_value = b"PDF-BYTES"
    dest = tmp_path / "b.pdf"
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_attachments_download(
                chat_id="spaces/AAA",
                message_id="spaces/AAA/messages/9",
                attachment_id="res-1",
                out=str(dest),
            )
        )
    assert media.download_media.called
    assert not media.download.called
    assert dest.read_bytes() == b"PDF-BYTES"
    assert payload["saved"][0]["name"] == "b.pdf"
