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
from msgraph.generated.users.item.mail_folders.mail_folders_request_builder import (
    MailFoldersRequestBuilder,
)
from msgraph.generated.users.item.messages.item.attachments.attachments_request_builder import (
    AttachmentsRequestBuilder,
)
from msgraph.generated.users.item.messages.messages_request_builder import (
    MessagesRequestBuilder,
)

from blumkin.attachments import (
    existing_entry_names,
    prepare_download_directory,
    resolve_attachment_dest,
    resolve_single_download_dest,
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


class MailFolderNotFoundError(Exception):
    """--folder did not resolve to a mail folder (not_found)."""


class MailDraftNotFoundError(Exception):
    """Draft id missing or not a draft (not_found)."""


class MailMessageNotFoundError(Exception):
    """Message id missing (not_found)."""


WELL_KNOWN_MAIL_FOLDERS = (
    "archive",
    "deleteditems",
    "drafts",
    "inbox",
    "junkemail",
    "outbox",
    "sentitems",
)


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


def format_folders_human(payload: dict[str, Any]) -> list[str]:
    folders = payload.get("folders") or []
    lines = [f"Mail folders: {len(folders)}"]
    if not folders:
        lines.append("  (none)")
        return lines
    for item in folders:
        path = sanitize_terminal(str(item.get("path") or ""))
        lines.append(f"  • {path} — {item.get('total')} message(s), {item.get('unread')} unread")
        lines.append(f"      id={item.get('id')}")
    if payload.get("truncated"):
        lines.append(f"  (truncated: {_folder_limits_note(payload.get('limits'))})")
    return lines


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


def format_list_human(payload: dict[str, Any]) -> list[str]:
    items = payload["items"]
    folder = sanitize_terminal(str(payload.get("folder") or "all mail"))
    orderby = payload.get("orderby") or "received"
    lines = [f"{folder} (top {payload['top']}, by {orderby}): {len(items)} message(s)"]
    if not items:
        lines.append("  (none)")
        return lines
    outbound = bool(payload.get("outbound"))
    for item in items:
        stamp = item.get(orderby) or item.get("received") or item.get("created") or "(no date)"
        unread = "" if item.get("is_read") else " [unread]"
        if outbound:
            who = "to " + sanitize_terminal(str(item.get("to_email") or "(no recipient)"))
        else:
            who = sanitize_terminal(
                str(item.get("from_name") or item.get("from_email") or "(unknown)")
            )
        subject = sanitize_terminal(str(item.get("subject") or "(no subject)"))
        lines.append(f"  • {stamp}{unread} — {who}: {subject}")
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
        aid = attachment_id.strip() if attachment_id else ""
        match = next((a for a in attachments if a.get("id") == aid), None)
        if match is None:
            raise MailAttachmentNotFoundError(f"attachment not found: {aid}")
        if match.get("skipped"):
            raise MailAttachmentSkippedError(
                match.get("skip_reason") or "unsupported attachment type"
            )
        targets = [match]
        out_path = resolve_single_download_dest(out, match.get("name") or aid)
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


async def mail_folders(*, config: BlumkinConfig | None = None) -> dict[str, Any]:
    """List mail folders (including nested ones) so custom folders are addressable."""
    cfg = config or load_config()
    client = create_graph_client(cfg)
    folders: list[dict[str, Any]] = []
    truncated = await _collect_mail_folders(
        client, client.me.mail_folders, folders=folders, prefix="", depth=0
    )
    return {
        "folders": folders,
        "limits": {"max_depth": _MAX_FOLDER_DEPTH, "max_folders": _MAX_FOLDERS},
        "truncated": truncated,
    }


async def mail_inbox(
    *,
    top: int = 10,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    payload = await mail_list(top=top, config=config)
    return {"items": payload["items"], "top": payload["top"]}


async def mail_list(
    *,
    top: int = 10,
    folder: str | None = None,
    orderby: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """List messages from a well-known folder, a folder id, or the whole mailbox."""
    if top < 1:
        raise ValueError("--top must be >= 1")
    label = None if folder is None else folder.strip()
    if folder is not None and not label:
        raise ValueError("--folder cannot be empty")
    requested_sort = None if orderby is None else _validate_orderby(orderby)
    cfg = config or load_config()
    client = create_graph_client(cfg)
    well_known = None if label is None else _well_known_folder(label)
    target = well_known or label
    sort = requested_sort or _default_orderby(well_known)
    try:
        page = await _get_messages(client, target, top=top, sort=sort)
    except ODataError as exc:
        # An exact well-known name cannot be a stale id, so only fall back for
        # free-form labels: a real folder of that display name wins over the alias.
        if label is None or well_known is not None or not _is_folder_lookup_failure(exc):
            raise
        target, well_known, truncated = await _resolve_folder_fallback(client, label)
        if target is None:
            raise MailFolderNotFoundError(
                _folder_not_found_message(label, truncated=truncated)
            ) from exc
        sort = requested_sort or _default_orderby(well_known)
        page = await _get_messages(client, target, top=top, sort=sort)
    items = [] if page is None else (page.value or [])
    return {
        "folder": target,
        "items": [_message_to_dict(msg) for msg in items],
        "orderby": sort,
        "outbound": well_known in _OUTBOUND_FOLDERS,
        "top": top,
    }


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


_FOLDER_LOOKUP_ERROR_CODES = frozenset(
    {
        "errorfoldernotfound",
        "errorinvalididmalformed",
        "erroritemnotfound",
        "resourcenotfound",
    }
)
_FOLDER_PAGE_SIZE = 100
# Guardrails for the recursive folder walk: mailboxes with deep or huge folder trees
# should return a useful listing rather than fan out into hundreds of Graph calls.
_MAX_FOLDERS = 500
_MAX_FOLDER_DEPTH = 6
_ORDERBY_FIELDS = {
    "created": "createdDateTime",
    "received": "receivedDateTime",
    "sent": "sentDateTime",
}
# Outbound folders have a null receivedDateTime, which collapses the default ordering.
# Drafts and Outbox have never been sent either, so they order by creation instead.
_FOLDER_DEFAULT_ORDERBY = {
    "drafts": "created",
    "outbox": "created",
    "sentitems": "sent",
}
_OUTBOUND_FOLDERS = frozenset(_FOLDER_DEFAULT_ORDERBY)
# Everyday spellings, applied only after a real folder of that name fails to match.
_MAIL_FOLDER_ALIASES = {
    "deleted": "deleteditems",
    "draft": "drafts",
    "junk": "junkemail",
    "sent": "sentitems",
    "spam": "junkemail",
    "trash": "deleteditems",
}
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


async def _collect_mail_folders(
    client: Any,
    builder: Any,
    *,
    folders: list[dict[str, Any]],
    prefix: str,
    depth: int,
) -> bool:
    """Append folders under ``builder``; returns True when a cap cut the walk short."""
    if depth > _MAX_FOLDER_DEPTH or len(folders) >= _MAX_FOLDERS:
        return True
    truncated = False
    query = MailFoldersRequestBuilder.MailFoldersRequestBuilderGetQueryParameters(
        top=_FOLDER_PAGE_SIZE,
        select=["childFolderCount", "displayName", "id", "totalItemCount", "unreadItemCount"],
    )
    page = await builder.get(request_config(query))
    while page is not None:
        for folder in page.value or []:
            if len(folders) >= _MAX_FOLDERS:
                return True
            name = str(getattr(folder, "display_name", None) or "")
            path = f"{prefix}/{name}" if prefix else name
            folder_id = getattr(folder, "id", None)
            child_count = getattr(folder, "child_folder_count", None) or 0
            folders.append(
                {
                    "child_count": int(child_count),
                    "id": folder_id,
                    "name": name,
                    "path": path,
                    "total": getattr(folder, "total_item_count", None),
                    "unread": getattr(folder, "unread_item_count", None),
                }
            )
            if child_count and folder_id:
                truncated |= await _collect_mail_folders(
                    client,
                    client.me.mail_folders.by_mail_folder_id(str(folder_id)).child_folders,
                    folders=folders,
                    prefix=path,
                    depth=depth + 1,
                )
        link = getattr(page, "odata_next_link", None)
        if not link:
            return truncated
        page = await builder.with_url(str(link)).get()
    return truncated


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


def _ambiguous_folder_message(label: str, matches: list[dict[str, Any]]) -> str:
    listed = "; ".join(
        f"{sanitize_terminal(str(item.get('path') or ''))} (id={item.get('id')})"
        for item in matches
    )
    return (
        f"mail folder name is ambiguous: {sanitize_terminal(label)!r} matches {listed}. "
        "Pass the folder id instead."
    )


def _default_orderby(well_known: str | None) -> str:
    return _FOLDER_DEFAULT_ORDERBY.get(well_known or "", "received")


def _folder_match_key(label: str) -> str:
    return re.sub(r"[\s_-]+", "", label.casefold())


def _folder_limits_note(limits: Any) -> str:
    values = limits if isinstance(limits, dict) else {}
    max_folders = values.get("max_folders", _MAX_FOLDERS)
    max_depth = values.get("max_depth", _MAX_FOLDER_DEPTH)
    return f"listing stops after {max_folders} folders or depth {max_depth}"


def _folder_not_found_message(folder: str, *, truncated: bool = False) -> str:
    known = ", ".join(WELL_KNOWN_MAIL_FOLDERS)
    message = (
        f"mail folder not found: {sanitize_terminal(folder)!r} "
        f"(well-known names: {known}; run 'blumkin mail folders' to list folder ids)"
    )
    if truncated:
        # Without this the guidance is a dead end: the same caps hide the folder there too.
        message += (
            f". The folder listing was truncated ({_folder_limits_note(None)}), "
            "so this name may sit beyond it"
        )
    return message


async def _get_messages(client: Any, folder: str | None, *, top: int, sort: str) -> Any:
    query = MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters(
        top=top,
        orderby=[f"{_ORDERBY_FIELDS[sort]} desc"],
        select=[
            "id",
            "subject",
            "from",
            "toRecipients",
            "createdDateTime",
            "receivedDateTime",
            "sentDateTime",
            "isRead",
            "hasAttachments",
            "bodyPreview",
            "body",
        ],
    )
    builder = (
        client.me.messages
        if folder is None
        else client.me.mail_folders.by_mail_folder_id(folder).messages
    )
    return await builder.get(request_config(query))


def _is_folder_lookup_failure(exc: ODataError) -> bool:
    """True when Graph rejected the folder segment rather than the query itself.

    A bare 400 is not enough: Graph also returns it for query-level problems such as
    a --top above its cap, and reporting those as a missing folder sends the operator
    after the wrong thing. Only id-shaped complaints count.
    """
    status = getattr(exc, "response_status_code", None)
    code = str(getattr(getattr(exc, "error", None), "code", "") or "").casefold()
    if code in _FOLDER_LOOKUP_ERROR_CODES:
        return True
    return status == 404


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
    created = getattr(msg, "created_date_time", None)
    sent = getattr(msg, "sent_date_time", None)
    return {
        "body_html": body_html,
        "created": str(created) if created else None,
        "body_preview": msg.body_preview,
        "body_text": body_text,
        "from_email": from_email,
        "from_name": from_name,
        "has_attachments": bool(msg.has_attachments),
        "id": msg.id,
        "is_read": bool(msg.is_read),
        "received": str(msg.received_date_time) if msg.received_date_time else None,
        "sent": str(sent) if sent else None,
        "subject": msg.subject,
        "to_email": _primary_to_address(msg),
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


async def _resolve_folder_fallback(client: Any, label: str) -> tuple[str | None, str | None, bool]:
    """Resolve a --folder label Graph rejected as an id, returning (target, well-known).

    A real folder with that display name wins over the alias table, so a mailbox that
    kept both "Sent Items" and a custom "Sent" after an IMAP migration can still reach
    its own folder by name instead of being silently redirected.
    """
    key = _folder_match_key(label)
    folders: list[dict[str, Any]] = []
    truncated = await _collect_mail_folders(
        client, client.me.mail_folders, folders=folders, prefix="", depth=0
    )
    matches = [
        item
        for item in folders
        if item.get("id")
        and key
        in {
            _folder_match_key(str(item.get("name") or "")),
            _folder_match_key(str(item.get("path") or "")),
        }
    ]
    if len(matches) > 1:
        raise MailFolderNotFoundError(_ambiguous_folder_message(label, matches))
    if matches:
        return str(matches[0]["id"]), None, truncated
    alias = _MAIL_FOLDER_ALIASES.get(key)
    if alias is not None:
        return alias, alias, truncated
    return None, None, truncated


def _validate_orderby(raw: str) -> str:
    label = raw.strip().casefold()
    if label not in _ORDERBY_FIELDS:
        allowed = ", ".join(f"'{name}'" for name in sorted(_ORDERBY_FIELDS))
        raise ValueError(f"--orderby must be one of {allowed}")
    return label


def _well_known_folder(label: str) -> str | None:
    key = _folder_match_key(label)
    return key if key in WELL_KNOWN_MAIL_FOLDERS else None


async def _require_message(client: Any, message_id: str) -> None:
    existing = await client.me.messages.by_message_id(message_id).get()
    if existing is None or not existing.id:
        raise MailMessageNotFoundError(f"message not found: {message_id}")


def _skip_reason(attachment: Any) -> str:
    odata_type = getattr(attachment, "odata_type", None) or "attachment"
    return f"{odata_type} not supported in v1"
