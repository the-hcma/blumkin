"""Hermetic unit tests for Google Gmail attachments list/download and mail folders."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from click.testing import CliRunner
from googleapiclient.errors import HttpError

from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.exit_codes import EXIT_NOT_FOUND
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.skills.mail import MailAttachmentNotFoundError, MailMessageNotFoundError

_GOOGLE_MAIL = "blumkin.providers.google.mail"


def test_mail_attachments_list_walks_nested_parts(tmp_path: Path) -> None:
    service = _service(
        message=_full_message(
            attachments=[("flat.pdf", "att-1", "application/pdf", 10)],
            nested=[("deep.png", "att-2", "image/png", 20)],
        )
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_attachments_list(message_id="m-1")
        )
    assert payload["message_id"] == "m-1"
    by_name = {a["name"]: a for a in payload["attachments"]}
    assert set(by_name) == {"flat.pdf", "deep.png"}
    assert by_name["flat.pdf"] == {
        "attachment_type": "application/pdf",
        "content_type": "application/pdf",
        "id": "att-1",
        "is_inline": False,
        "name": "flat.pdf",
        "size": 10,
        "skipped": False,
    }


def test_mail_attachments_list_missing_message_raises_not_found(tmp_path: Path) -> None:
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.side_effect = (
        HttpError(httplib2.Response({"status": 404}), b"{}", uri="x")
    )
    with _patched(service), pytest.raises(MailMessageNotFoundError):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_attachments_list(message_id="gone")
        )


def test_mail_attachments_download_single_writes_file(tmp_path: Path) -> None:
    service = _service(
        message=_full_message(attachments=[("report.pdf", "att-1", "application/pdf", 7)]),
        attachment_data=b"PDFDATA",
    )
    dest = tmp_path / "saved.pdf"
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_attachments_download(
                message_id="m-1", attachment_id="att-1", out=str(dest)
            )
        )
    assert dest.read_bytes() == b"PDFDATA"
    assert payload["saved"][0]["name"] == "report.pdf"
    assert payload["saved"][0]["size"] == len(b"PDFDATA")
    assert payload["skipped"] == []


def test_mail_attachments_list_is_inline_matches_the_directive_not_a_substring(
    tmp_path: Path,
) -> None:
    msg = _full_message()
    msg["payload"]["parts"].extend(
        [
            {
                "mimeType": "application/pdf",
                "filename": "inline-report.pdf",  # "inline" in the filename, not the directive
                "body": {"attachmentId": "att-file", "size": 1},
                "headers": [
                    {
                        "name": "Content-Disposition",
                        "value": 'attachment; filename="inline-report.pdf"',
                    }
                ],
            },
            {
                "mimeType": "image/png",
                "filename": "logo.png",
                "body": {"attachmentId": "att-inline", "size": 1},
                "headers": [{"name": "Content-Disposition", "value": "inline; filename=logo.png"}],
            },
        ]
    )
    with _patched(_service(message=msg)):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_attachments_list(message_id="m-1")
        )
    by_name = {a["name"]: a for a in payload["attachments"]}
    assert by_name["inline-report.pdf"]["is_inline"] is False
    assert by_name["logo.png"]["is_inline"] is True


def test_mail_attachments_download_repads_unpadded_base64(tmp_path: Path) -> None:
    payload_bytes = b"five!"  # encodes to a length that needs padding
    service = _service(
        message=_full_message(attachments=[("x.bin", "att-1", "application/octet-stream", 5)]),
        attachment_data=payload_bytes,
        unpadded=True,
    )
    dest = tmp_path / "x.bin"
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_attachments_download(
                message_id="m-1", attachment_id="att-1", out=str(dest)
            )
        )
    assert dest.read_bytes() == payload_bytes


def test_mail_attachments_list_uses_attachment_id_when_filename_missing(tmp_path: Path) -> None:
    msg = _full_message()
    msg["payload"]["parts"].append(
        {"mimeType": "application/pdf", "body": {"attachmentId": "att-nofn", "size": 4}}
    )
    with _patched(_service(message=msg)):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_attachments_list(message_id="m-1")
        )
    assert [a["name"] for a in payload["attachments"]] == ["att-nofn"]
    assert payload["attachments"][0]["id"] == "att-nofn"


def test_mail_attachments_download_all_writes_dir(tmp_path: Path) -> None:
    service = _service(
        message=_full_message(
            attachments=[
                ("a.txt", "att-1", "text/plain", 3),
                ("b.txt", "att-2", "text/plain", 3),
            ]
        ),
        attachment_data=b"xyz",
    )
    out_dir = tmp_path / "dump"
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_attachments_download(
                message_id="m-1", download_all=True, out=str(out_dir)
            )
        )
    assert {p.name for p in out_dir.iterdir()} == {"a.txt", "b.txt"}
    assert len(payload["saved"]) == 2


def test_mail_attachments_download_requires_exactly_one_selector(tmp_path: Path) -> None:
    with _patched(_service()), pytest.raises(ValueError, match="exactly one"):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_attachments_download(
                message_id="m-1", out=str(tmp_path / "x")
            )
        )


def test_mail_attachments_download_unknown_id_raises_not_found(tmp_path: Path) -> None:
    service = _service(message=_full_message(attachments=[("a.txt", "att-1", "text/plain", 3)]))
    with _patched(service), pytest.raises(MailAttachmentNotFoundError):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_attachments_download(
                message_id="m-1", attachment_id="nope", out=str(tmp_path / "x")
            )
        )


def test_mail_attachments_download_rejects_both_selectors(tmp_path: Path) -> None:
    with _patched(_service()), pytest.raises(ValueError, match="exactly one"):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_attachments_download(
                message_id="m-1", attachment_id="att-1", download_all=True, out=str(tmp_path / "x")
            )
        )


def test_mail_attachments_download_missing_data_raises_not_found(tmp_path: Path) -> None:
    service = _service(message=_full_message(attachments=[("a.txt", "att-1", "text/plain", 3)]))
    _attachments_get(service).execute.return_value = {}
    with _patched(service), pytest.raises(MailAttachmentNotFoundError):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_attachments_download(
                message_id="m-1", attachment_id="att-1", out=str(tmp_path / "x")
            )
        )


def test_mail_attachments_download_get_404_maps_to_not_found_via_cli(tmp_path: Path) -> None:
    service = _service(message=_full_message(attachments=[("a.txt", "att-1", "text/plain", 3)]))
    _attachments_get(service).execute.side_effect = HttpError(
        httplib2.Response({"status": 404}), b"{}", uri="x"
    )
    provider = GoogleWorkspaceProvider(_cfg(tmp_path))
    with _patched(service), patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(
            main,
            [
                "mail",
                "attachments",
                "download",
                "--message-id",
                "m-1",
                "--attachment-id",
                "att-1",
                "--out",
                str(tmp_path / "x"),
                "--json",
            ],
        )
    assert result.exit_code == EXIT_NOT_FOUND
    assert json.loads(result.stderr)["error"] == "not_found"


def test_mail_folders_maps_system_labels_and_user_labels(tmp_path: Path) -> None:
    service = MagicMock()
    labels = service.users.return_value.labels.return_value
    labels.list.return_value.execute.return_value = {
        "labels": [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "STARRED", "name": "STARRED", "type": "system"},
            {"id": "Label_1", "name": "Clients/Acme", "type": "user"},
        ]
    }
    labels.get.return_value.execute.return_value = {"messagesTotal": 5, "messagesUnread": 2}
    with _patched(service):
        payload = asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).mail_folders())
    paths = [f["path"] for f in payload["folders"]]
    assert paths == ["Clients/Acme", "Inbox"]  # STARRED dropped, sorted by path
    assert payload["folders"][0] == {
        "id": "Label_1",
        "path": "Clients/Acme",
        "total": 5,
        "unread": 2,
    }
    assert payload["counts_may_lag"] is True
    assert payload["truncated"] is False


def test_mail_folders_tolerates_a_failing_label_get(tmp_path: Path) -> None:
    service = MagicMock()
    labels = service.users.return_value.labels.return_value
    labels.list.return_value.execute.return_value = {
        "labels": [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "Label_1", "name": "Work", "type": "user"},
        ]
    }

    def _get(userId: str, id: str):  # noqa: A002 - matches the API kwarg
        result = MagicMock()
        if id == "INBOX":
            result.execute.side_effect = HttpError(
                httplib2.Response({"status": 404}), b"{}", uri="x"
            )
        else:
            result.execute.return_value = {"messagesTotal": 3, "messagesUnread": 1}
        return result

    labels.get.side_effect = _get
    with _patched(service):
        payload = asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).mail_folders())
    by_path = {f["path"]: f for f in payload["folders"]}
    assert by_path["Inbox"]["total"] is None  # count unknown, still listed
    assert by_path["Work"]["total"] == 3


def test_mail_folders_truncates_past_the_cap(tmp_path: Path) -> None:
    service = MagicMock()
    labels = service.users.return_value.labels.return_value
    labels.list.return_value.execute.return_value = {
        "labels": [{"id": f"L{i}", "name": f"label-{i:04d}", "type": "user"} for i in range(301)]
    }
    labels.get.return_value.execute.return_value = {"messagesTotal": 1, "messagesUnread": 0}
    with _patched(service):
        payload = asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).mail_folders())
    assert len(payload["folders"]) == 300
    assert payload["truncated"] is True


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


def _full_message(
    *,
    attachments: list[tuple[str, str, str, int]] | None = None,
    nested: list[tuple[str, str, str, int]] | None = None,
) -> dict:
    def part(spec: tuple[str, str, str, int]) -> dict:
        name, att_id, mime, size = spec
        return {
            "mimeType": mime,
            "filename": name,
            "body": {"attachmentId": att_id, "size": size},
        }

    parts: list[dict] = [{"mimeType": "text/plain", "body": {"data": ""}}]
    parts.extend(part(spec) for spec in attachments or [])
    if nested:
        parts.append({"mimeType": "multipart/mixed", "parts": [part(s) for s in nested]})
    return {"id": "m-1", "payload": {"mimeType": "multipart/mixed", "parts": parts}}


def _attachments_get(service: MagicMock) -> MagicMock:
    return (
        service.users.return_value.messages.return_value.attachments.return_value.get.return_value
    )


def _patched(service: MagicMock):
    return patch.multiple(
        _GOOGLE_MAIL,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    )


def _service(
    *,
    message: dict | None = None,
    attachment_data: bytes = b"data",
    unpadded: bool = False,
) -> MagicMock:
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.get.return_value.execute.return_value = message or _full_message()
    encoded = base64.urlsafe_b64encode(attachment_data).decode()
    if unpadded:
        # Real Gmail returns unpadded base64url; _attachment_bytes must re-pad.
        encoded = encoded.rstrip("=")
    messages.attachments.return_value.get.return_value.execute.return_value = {
        "data": encoded,
        "size": len(attachment_data),
    }
    return service
