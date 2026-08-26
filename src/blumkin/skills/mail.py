"""Mail read and draft skills."""

from __future__ import annotations

import base64
import binascii
import html as html_lib
import re
from pathlib import Path
from typing import Any, Literal

from kiota_abstractions.method import Method
from kiota_abstractions.request_information import RequestInformation
from kiota_abstractions.serialization.parsable_factory import ParsableFactory
from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.email_address import EmailAddress
from msgraph.generated.models.file_attachment import FileAttachment
from msgraph.generated.models.item_body import ItemBody
from msgraph.generated.models.message import Message
from msgraph.generated.models.o_data_errors.o_data_error import ODataError
from msgraph.generated.models.recipient import Recipient
from msgraph.generated.users.item.messages.item.attachments.attachments_request_builder import (
    AttachmentsRequestBuilder,
)
from msgraph.generated.users.item.messages.messages_request_builder import (
    MessagesRequestBuilder,
)

from blumkin.attachments import (
    existing_entry_names,
    out_is_directory_intent,
    prepare_download_directory,
    resolve_attachment_dest,
    sanitize_attachment_filename,
    unique_filename,
)
from blumkin.config import BlumkinConfig, load_config
from blumkin.graph import create_graph_client, request_config
from blumkin.output import sanitize_terminal

MailBodyType = Literal["html", "text"]


class MailAttachmentNotFoundError(Exception):
    """Attachment id missing on the message (not_found)."""


class MailAttachmentSkippedError(Exception):
    """Attachment type is not downloadable in v1 (usage)."""


class MailBodyFileError(Exception):
    """--body-file could not be read (usage, not auth)."""


class MailDraftNotFoundError(Exception):
    """Draft id missing or not a draft (not_found)."""


class MailMessageNotFoundError(Exception):
    """Message id missing (not_found)."""


def format_attachments_download_human(payload: dict[str, Any]) -> list[str]:
    lines = [
        f"Saved {len(payload.get('saved', []))} attachment(s) for {payload.get('message_id')!r}"
    ]
    for item in payload.get("saved") or []:
        name = sanitize_terminal(str(item.get("name") or ""))
        saved_path = sanitize_terminal(str(item.get("saved_path") or ""))
        lines.append(f"  • {name!r} → {saved_path}")
    for item in payload.get("skipped") or []:
        name = sanitize_terminal(str(item.get("name") or ""))
        reason = sanitize_terminal(str(item.get("reason") or ""))
        lines.append(f"  • skipped {name!r}: {reason}")
    return lines


def format_attachments_human(payload: dict[str, Any]) -> list[str]:
    attachments = payload.get("attachments") or []
    lines = [f"Attachments on {payload.get('message_id')!r}: {len(attachments)}"]
    if not attachments:
        lines.append("  (none)")
        return lines
    for item in attachments:
        if item.get("skipped"):
            label = sanitize_terminal(str(item.get("name") or item.get("id") or ""))
            attachment_type = sanitize_terminal(str(item.get("attachment_type") or ""))
            skip_reason = sanitize_terminal(str(item.get("skip_reason") or ""))
            lines.append(f"  • {label!r} [{attachment_type}] skipped: {skip_reason}")
        else:
            name = sanitize_terminal(str(item.get("name") or ""))
            content_type = sanitize_terminal(str(item.get("content_type") or ""))
            lines.append(
                f"  • {name!r} ({item.get('size')} bytes, {content_type}) id={item.get('id')}"
            )
    return lines


def format_delete_draft_human(payload: dict[str, Any]) -> list[str]:
    return [f"Draft deleted: {payload.get('deleted')!r}"]


def format_draft_human(payload: dict[str, Any]) -> list[str]:
    draft = payload.get("draft") or {}
    to_addr = sanitize_terminal(str(draft.get("to") or ""))
    body_type = draft.get("body_type") or "text"
    return [
        f"Draft saved: {draft.get('subject')!r} → {to_addr} ({body_type})",
        f"  id={draft.get('id')}",
    ]


def format_inbox_human(payload: dict[str, Any]) -> list[str]:
    lines = [f"Inbox (top {payload['top']}): {len(payload['items'])} message(s)"]
    if not payload["items"]:
        lines.append("  (none)")
        return lines
    for item in payload["items"]:
        unread = "" if item.get("is_read") else " [unread]"
        who = sanitize_terminal(str(item.get("from_name") or item.get("from_email") or "(unknown)"))
        subject = sanitize_terminal(str(item.get("subject") or "(no subject)"))
        lines.append(f"  • {item.get('received')}{unread} — {who}: {subject}")
    return lines


def format_send_draft_human(payload: dict[str, Any]) -> list[str]:
    return [f"Sent draft {payload.get('sent')!r}"]


async def mail_attachments_list(
    *,
    message_id: str,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if not message_id.strip():
        raise ValueError("--id is required")
    cfg = config or load_config()
    client = create_graph_client(cfg)
    mid = message_id.strip()
    await _require_message(client, mid)
    query = AttachmentsRequestBuilder.AttachmentsRequestBuilderGetQueryParameters(
        select=["id", "name", "size", "contentType", "isInline"],
    )
    page = await client.me.messages.by_message_id(mid).attachments.get(request_config(query))
    raw: list[Any] = []
    while page is not None:
        raw.extend(page.value or [])
        link = getattr(page, "odata_next_link", None)
        if not link:
            break
        page = await client.me.messages.by_message_id(mid).attachments.with_url(link).get()
    return {"attachments": [_attachment_to_dict(att) for att in raw], "message_id": mid}


async def mail_attachments_download(
    *,
    message_id: str,
    out: str,
    attachment_id: str | None = None,
    download_all: bool = False,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if not message_id.strip():
        raise ValueError("--message-id is required")
    if not out.strip():
        raise ValueError("--out is required")
    if download_all == bool(attachment_id and attachment_id.strip()):
        raise ValueError("provide exactly one of --attachment-id or --all")
    if not download_all and not (attachment_id and attachment_id.strip()):
        raise ValueError("provide exactly one of --attachment-id or --all")
    cfg = config or load_config()
    client = create_graph_client(cfg)
    mid = message_id.strip()
    await _require_message(client, mid)
    listed = await mail_attachments_list(message_id=mid, config=cfg)
    attachments = listed["attachments"]
    if download_all:
        out_path = prepare_download_directory(out)
        targets = [a for a in attachments if not a.get("skipped")]
    else:
        out_path = Path(out)
        aid = attachment_id.strip() if attachment_id else ""
        match = next((a for a in attachments if a.get("id") == aid), None)
        if match is None:
            raise MailAttachmentNotFoundError(f"attachment not found: {aid}")
        if match.get("skipped"):
            raise MailAttachmentSkippedError(
                match.get("skip_reason") or "unsupported attachment type"
            )
        targets = [match]
        filename = sanitize_attachment_filename(match.get("name") or aid)
        if out_is_directory_intent(out, out_path):
            if not out_path.exists():
                out_path.mkdir(parents=True, exist_ok=True)
            if not out_path.is_dir():
                raise ValueError("--out must be a directory")
            unique = unique_filename(filename, existing_entry_names(out_path))
            out_path = resolve_attachment_dest(out_path, unique)
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    used_names = existing_entry_names(out_path) if download_all else set()
    for meta in attachments if download_all else targets:
        if meta.get("skipped"):
            skipped.append(
                {
                    "id": meta.get("id"),
                    "name": meta.get("name"),
                    "reason": meta.get("skip_reason"),
                }
            )
            continue
        aid = str(meta["id"])
        attachment = (
            await client.me.messages.by_message_id(mid).attachments.by_attachment_id(aid).get()
        )
        if attachment is None:
            raise MailAttachmentNotFoundError(f"attachment not found: {aid}")
        if _attachment_is_skipped(attachment):
            reason = _skip_reason(attachment)
            skipped.append({"id": aid, "name": meta.get("name"), "reason": reason})
            if not download_all:
                raise MailAttachmentSkippedError(reason)
            continue
        content = await _fetch_attachment_bytes(client, mid, aid, attachment)
        if download_all:
            filename = unique_filename(
                sanitize_attachment_filename(meta.get("name") or aid),
                used_names,
            )
            dest = resolve_attachment_dest(out_path, filename)
        else:
            dest = out_path
        dest.write_bytes(content)
        saved.append(
            {
                "attachment_id": aid,
                "content_type": meta.get("content_type"),
                "name": meta.get("name"),
                "saved_path": str(dest.resolve()),
                "size": len(content),
            }
        )
    return {"message_id": mid, "saved": saved, "skipped": skipped}


async def mail_delete_draft(
    *,
    draft_id: str,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if not draft_id.strip():
        raise ValueError("--id is required")
    cfg = config or load_config()
    client = create_graph_client(cfg)
    mid = draft_id.strip()
    existing = await client.me.messages.by_message_id(mid).get()
    if existing is None or not existing.id:
        raise MailDraftNotFoundError(f"message not found: {mid}")
    if not existing.is_draft:
        raise MailDraftNotFoundError(f"message is not a draft: {mid}")
    await client.me.messages.by_message_id(mid).delete()
    return {"deleted": mid}


async def mail_draft(
    *,
    to: str,
    subject: str,
    body: str | None = None,
    body_file: str | None = None,
    body_type: str = "text",
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if not to.strip():
        raise ValueError("--to is required")
    if not subject.strip():
        raise ValueError("--subject is required")
    content, body_type_label, graph_body_type = resolve_mail_body(
        body=body, body_file=body_file, body_type=body_type
    )
    cfg = config or load_config()
    client = create_graph_client(cfg)
    message = Message(
        body=ItemBody(content_type=graph_body_type, content=content),
        subject=subject.strip(),
        to_recipients=[
            Recipient(email_address=EmailAddress(address=to.strip())),
        ],
    )
    created = await client.me.messages.post(message)
    if created is None or not created.id:
        raise RuntimeError("Graph returned no draft message")
    return {
        "draft": {
            "body_type": body_type_label,
            "id": created.id,
            "subject": created.subject,
            "to": to.strip(),
        }
    }


async def mail_inbox(
    *,
    top: int = 10,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if top < 1:
        raise ValueError("--top must be >= 1")
    cfg = config or load_config()
    client = create_graph_client(cfg)
    query = MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters(
        top=top,
        orderby=["receivedDateTime desc"],
        select=[
            "id",
            "subject",
            "from",
            "receivedDateTime",
            "isRead",
            "hasAttachments",
            "bodyPreview",
            "body",
        ],
    )
    page = await client.me.messages.get(request_config(query))
    items = [] if page is None else (page.value or [])
    return {"items": [_message_to_dict(msg) for msg in items], "top": top}


async def mail_send_draft(
    *,
    draft_id: str,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if not draft_id.strip():
        raise ValueError("--id is required")
    cfg = config or load_config()
    client = create_graph_client(cfg)
    await client.me.messages.by_message_id(draft_id.strip()).send.post()
    return {"sent": draft_id.strip()}


async def mail_update_draft(
    *,
    draft_id: str,
    subject: str | None = None,
    body: str | None = None,
    body_file: str | None = None,
    body_type: str = "text",
    to: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if not draft_id.strip():
        raise ValueError("--id is required")
    has_body = body is not None or body_file is not None
    if subject is None and not has_body and to is None:
        raise ValueError("provide at least one of --subject, --body/--body-file, or --to")
    content: str | None = None
    body_type_label: MailBodyType | None = None
    graph_body_type: BodyType | None = None
    if has_body:
        content, body_type_label, graph_body_type = resolve_mail_body(
            body=body, body_file=body_file, body_type=body_type
        )
        if not content.strip():
            raise ValueError("--body/--body-file must be non-empty when provided")
    cfg = config or load_config()
    client = create_graph_client(cfg)
    mid = draft_id.strip()
    existing = await client.me.messages.by_message_id(mid).get()
    if existing is None or not existing.id:
        raise MailDraftNotFoundError(f"message not found: {mid}")
    if not existing.is_draft:
        raise MailDraftNotFoundError(f"message is not a draft: {mid}")
    patch = Message()
    if subject is not None:
        if not subject.strip():
            raise ValueError("--subject must be non-empty when provided")
        patch.subject = subject.strip()
    if content is not None and graph_body_type is not None:
        patch.body = ItemBody(content_type=graph_body_type, content=content)
    if to is not None:
        if not to.strip():
            raise ValueError("--to must be non-empty when provided")
        existing_tos = list(existing.to_recipients or [])
        if len(existing_tos) > 1:
            raise ValueError("draft has multiple To recipients; --to would replace the entire list")
        patch.to_recipients = [
            Recipient(email_address=EmailAddress(address=to.strip())),
        ]
    updated = await client.me.messages.by_message_id(mid).patch(patch)
    if updated is None:
        # Empty 2xx body — re-fetch so JSON/human output reflects post-PATCH state.
        updated = await client.me.messages.by_message_id(mid).get()
    if updated is None:
        raise RuntimeError(f"Graph returned no message after update-draft: {mid}")
    to_out = to.strip() if to is not None and to.strip() else _primary_to_address(updated)
    body_out = body_type_label
    if body_out is None and updated.body and updated.body.content_type is not None:
        body_out = "html" if updated.body.content_type == BodyType.Html else "text"
    return {
        "draft": {
            "body_type": body_out or "text",
            "id": updated.id or mid,
            "subject": updated.subject if updated.subject is not None else existing.subject,
            "to": to_out,
        }
    }


def resolve_mail_body(
    *,
    body: str | None = None,
    body_file: str | None = None,
    body_type: str = "text",
) -> tuple[str, MailBodyType, BodyType]:
    """Resolve --body / --body-file and --body-type into content for Graph."""
    has_body = body is not None
    has_file = body_file is not None
    if has_body == has_file:
        raise ValueError("exactly one of --body or --body-file is required")
    label = _parse_body_type(body_type)
    graph_type = BodyType.Html if label == "html" else BodyType.Text
    if has_file:
        path = Path(str(body_file))
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise MailBodyFileError(f"cannot read --body-file {path}: {exc}") from exc
    else:
        content = str(body)
    return content, label, graph_type


_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _attachment_is_skipped(attachment: Any) -> bool:
    if isinstance(attachment, FileAttachment):
        return False
    odata_type = (getattr(attachment, "odata_type", None) or "").casefold()
    if odata_type in {"", "#microsoft.graph.fileattachment"}:
        return False
    return True


def _attachment_to_dict(attachment: Any) -> dict[str, Any]:
    skipped = _attachment_is_skipped(attachment)
    payload: dict[str, Any] = {
        "attachment_type": attachment.odata_type,
        "content_type": attachment.content_type,
        "id": attachment.id,
        "is_inline": bool(attachment.is_inline),
        "name": attachment.name,
        "size": attachment.size,
        "skipped": skipped,
    }
    if skipped:
        payload["skip_reason"] = _skip_reason(attachment)
    return payload


async def _fetch_attachment_bytes(
    client: Any, message_id: str, attachment_id: str, attachment: Any
) -> bytes:
    raw = getattr(attachment, "content_bytes", None)
    if raw:
        if isinstance(raw, bytes):
            return raw
        if isinstance(raw, str):
            try:
                return base64.b64decode(raw)
            except (ValueError, binascii.Error) as exc:
                raise ValueError(f"invalid attachment contentBytes encoding: {exc}") from exc
    request_info = RequestInformation(
        Method.GET,
        "https://graph.microsoft.com/v1.0/me/messages/{message%2Did}/attachments/{attachment%2Did}/$value",
        {
            "attachment%2Did": attachment_id,
            "message%2Did": message_id,
        },
    )
    error_mapping: dict[str, ParsableFactory] = {"4XX": ODataError, "5XX": ODataError}
    result = await client.request_adapter.send_primitive_async(request_info, "bytes", error_mapping)
    if result is None:
        raise RuntimeError(f"Graph returned empty attachment content: {attachment_id}")
    return bytes(result)


def _message_to_dict(msg: Any) -> dict[str, Any]:
    from_name = None
    from_email = None
    if msg.from_ and msg.from_.email_address:
        from_name = msg.from_.email_address.name
        from_email = msg.from_.email_address.address
    body_html = None
    body_text = None
    if msg.body and msg.body.content:
        body_html = msg.body.content
        body_text = _html_to_text(body_html)
    return {
        "body_html": body_html,
        "body_preview": msg.body_preview,
        "body_text": body_text,
        "from_email": from_email,
        "from_name": from_name,
        "has_attachments": bool(msg.has_attachments),
        "id": msg.id,
        "is_read": bool(msg.is_read),
        "received": str(msg.received_date_time) if msg.received_date_time else None,
        "subject": msg.subject,
    }


def _parse_body_type(raw: str) -> MailBodyType:
    label = raw.strip().lower()
    if label not in {"html", "text"}:
        raise ValueError("--body-type must be 'text' or 'html'")
    return label  # type: ignore[return-value]


def _primary_to_address(msg: Any) -> str | None:
    recipients = getattr(msg, "to_recipients", None) or []
    for recipient in recipients:
        email = getattr(recipient, "email_address", None)
        address = getattr(email, "address", None) if email is not None else None
        if address:
            return str(address)
    return None


async def _require_message(client: Any, message_id: str) -> None:
    existing = await client.me.messages.by_message_id(message_id).get()
    if existing is None or not existing.id:
        raise MailMessageNotFoundError(f"message not found: {message_id}")


def _skip_reason(attachment: Any) -> str:
    odata_type = getattr(attachment, "odata_type", None) or "attachment"
    return f"{odata_type} not supported in v1"
