"""Mail read and draft skills."""

from __future__ import annotations

import base64
import binascii
import html as html_lib
import re
from collections.abc import Sequence
from datetime import UTC, datetime
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
from msgraph.generated.users.item.mail_folders.item.child_folders.child_folders_request_builder import (  # noqa: E501
    ChildFoldersRequestBuilder,
)
from msgraph.generated.users.item.mail_folders.mail_folders_request_builder import (
    MailFoldersRequestBuilder,
)
from msgraph.generated.users.item.messages.item.attachments.attachments_request_builder import (
    AttachmentsRequestBuilder,
)
from msgraph.generated.users.item.messages.item.create_forward.create_forward_post_request_body import (  # noqa: E501
    CreateForwardPostRequestBody,
)
from msgraph.generated.users.item.messages.item.create_reply.create_reply_post_request_body import (
    CreateReplyPostRequestBody,
)
from msgraph.generated.users.item.messages.item.create_reply_all.create_reply_all_post_request_body import (  # noqa: E501
    CreateReplyAllPostRequestBody,
)
from msgraph.generated.users.item.messages.item.message_item_request_builder import (
    MessageItemRequestBuilder,
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


class MailAttachError(Exception):
    """--attach path could not be used (usage, not auth)."""


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
    lines = [
        f"Draft saved: {draft.get('subject')!r} → {to_addr} ({body_type})",
        f"  id={draft.get('id')}",
    ]
    for label in ("cc", "bcc"):
        value = draft.get(label)
        if value:
            lines.append(f"  {label}: {sanitize_terminal(str(value))}")
    for item in draft.get("attachments") or []:
        name = sanitize_terminal(str(item.get("name") or ""))
        lines.append(f"  attached: {name!r} ({item.get('size')} bytes) id={item.get('id')}")
    return lines


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


def format_get_human(payload: dict[str, Any]) -> list[str]:
    msg = payload.get("message") or {}
    sender = (
        _format_participant({"email": msg.get("from_email"), "name": msg.get("from_name")})
        or "(unknown sender)"
    )
    lines = [
        sanitize_terminal(str(msg.get("subject") or "(no subject)")),
        f"  from: {sender}",
    ]
    for label, key in (("to", "to"), ("cc", "cc")):
        people = [_format_participant(person) for person in msg.get(key) or []]
        shown = [person for person in people if person]
        if shown:
            lines.append(f"  {label}: {', '.join(shown)}")
    stamp = msg.get("received") or msg.get("sent") or msg.get("created")
    lines.append(f"  date: {stamp or '(no date)'}")
    # `is False` rather than `not`: an absent read state is unknown, and rendering that
    # as "unread" would state something about the message that was never reported.
    flags = [
        name
        for name, on in (("unread", msg.get("is_read") is False), ("draft", msg.get("is_draft")))
        if on
    ]
    if flags:
        lines.append(f"  flags: {', '.join(flags)}")
    for item in msg.get("attachments") or []:
        name = sanitize_terminal(str(item.get("name") or item.get("id") or ""))
        lines.append(f"  attachment: {name!r} ({item.get('size')} bytes) id={item.get('id')}")
    lines.append("")
    body = str(msg.get("body") or "").splitlines() or ["(no body)"]
    lines.extend(sanitize_terminal(line) for line in body)
    return lines


def format_inbox_human(payload: dict[str, Any]) -> list[str]:
    # Mirror format_list_human: a search has no sort, so say so rather than looking newest-first.
    orderby = payload.get("orderby")
    order_note = f", by {orderby}" if orderby else ", by relevance"
    lines = [f"Inbox (top {payload['top']}{order_note}): {len(payload['items'])} message(s)"]
    lines.extend(_filter_notes(payload))
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
    # A search has no sort order to report: Graph ranks those matches by relevance.
    orderby = payload.get("orderby")
    order_note = f"by {orderby}" if orderby else "by relevance"
    lines = [f"{folder} (top {payload['top']}, {order_note}): {len(items)} message(s)"]
    lines.extend(_filter_notes(payload))
    if not items:
        lines.append("  (none)")
        return lines
    outbound = bool(payload.get("outbound"))
    for item in items:
        stamp = (
            item.get(orderby or "received")
            or item.get("received")
            or item.get("created")
            or "(no date)"
        )
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


def format_reply_human(payload: dict[str, Any]) -> list[str]:
    draft = payload.get("draft") or {}
    recipients = sanitize_terminal(str(draft.get("to") or ""))
    return [
        f"{draft.get('kind')} draft saved: {draft.get('subject')!r} "
        f"→ {recipients or '(no recipient)'} ({draft.get('body_type')})",
        f"  id={draft.get('id')}",
        f"  in reply to {draft.get('source_message_id')}"
        if draft.get("kind") != "forward"
        else f"  forwarding {draft.get('source_message_id')}",
    ]


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
    return {"attachments": await _collect_attachments(client, mid), "message_id": mid}


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
    to: str | Sequence[str],
    subject: str,
    attach: Sequence[str] = (),
    bcc: str | Sequence[str] = (),
    body: str | None = None,
    body_file: str | None = None,
    body_type: str = "text",
    cc: str | Sequence[str] = (),
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    to_addrs = _parse_addresses(to, flag="--to", required=True)
    if to_addrs is None:
        raise ValueError("--to is required")
    cc_addrs = _parse_addresses(cc, flag="--cc", required=False) or []
    bcc_addrs = _parse_addresses(bcc, flag="--bcc", required=False) or []
    if not subject.strip():
        raise ValueError("--subject is required")
    # Read the files before touching Graph: a bad path should not leave a half-built draft.
    pending = [_read_attachment(path) for path in attach]
    content, body_type_label, graph_body_type = resolve_mail_body(
        body=body, body_file=body_file, body_type=body_type
    )
    cfg = config or load_config()
    client = create_graph_client(cfg)
    message = Message(
        bcc_recipients=_recipient_models(bcc_addrs) or None,
        body=ItemBody(content_type=graph_body_type, content=content),
        cc_recipients=_recipient_models(cc_addrs) or None,
        subject=subject.strip(),
        to_recipients=_recipient_models(to_addrs),
    )
    created = await client.me.messages.post(message)
    if created is None or not created.id:
        raise RuntimeError("Graph returned no draft message")
    try:
        attachments = await _upload_attachments(client, created.id, pending)
    except Exception:
        # A half-attached draft is worse than no draft: delete it so a retry is a no-op.
        try:
            await client.me.messages.by_message_id(created.id).delete()
        except Exception:
            pass
        raise
    return {
        "draft": {
            "attachments": attachments,
            "bcc": _join_addresses(bcc_addrs) or None,
            "body_type": body_type_label,
            "cc": _join_addresses(cc_addrs) or None,
            "id": created.id,
            "subject": created.subject,
            "to": _join_addresses(to_addrs),
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


async def mail_forward(
    *,
    message_id: str,
    to: str,
    body: str | None = None,
    body_file: str | None = None,
    body_type: str = "text",
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Create a forward draft, letting Graph carry over the original and its attachments."""
    mid = message_id.strip()
    if not mid:
        raise ValueError("--id is required")
    if not to.strip():
        raise ValueError("--to is required")
    comment = _resolve_comment(body=body, body_file=body_file, body_type=body_type)
    cfg = config or load_config()
    client = create_graph_client(cfg)
    request = CreateForwardPostRequestBody(
        comment=comment,
        to_recipients=[Recipient(email_address=EmailAddress(address=to.strip()))],
    )
    created = await _create_draft_from(
        client.me.messages.by_message_id(mid).create_forward, request, message_id=mid
    )
    return {"draft": _draft_summary(created, source=mid, kind="forward")}


async def mail_get(
    *,
    message_id: str,
    body_type: str = "text",
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Read one message in full — participants, timestamps, attachments, and body."""
    if not message_id.strip():
        raise ValueError("--id is required")
    wanted = _parse_body_type(body_type)
    cfg = config or load_config()
    client = create_graph_client(cfg)
    mid = message_id.strip()
    query = MessageItemRequestBuilder.MessageItemRequestBuilderGetQueryParameters(
        select=[
            "body",
            "bodyPreview",
            "ccRecipients",
            "conversationId",
            "createdDateTime",
            "from",
            "hasAttachments",
            "id",
            "internetMessageId",
            "isDraft",
            "isRead",
            "receivedDateTime",
            "sentDateTime",
            "subject",
            "toRecipients",
            "webLink",
        ],
    )
    # Graph converts the body for us when asked, which beats stripping tags locally.
    headers = {"Prefer": f'outlook.body-content-type="{wanted}"'}
    try:
        msg = await client.me.messages.by_message_id(mid).get(
            request_config(query, headers=headers)
        )
    except ODataError as exc:
        if not _is_id_lookup_failure(exc):
            raise
        raise MailMessageNotFoundError(f"message not found: {mid}") from exc
    if msg is None or not msg.id:
        raise MailMessageNotFoundError(f"message not found: {mid}")
    detail = _message_detail(msg, wanted=wanted)
    if detail["has_attachments"]:
        detail["attachments"] = await _collect_attachments(client, mid)
    return {"message": detail}


async def mail_inbox(
    *,
    top: int = 10,
    search: str | None = None,
    sender: str | None = None,
    since: datetime | None = None,
    subject: str | None = None,
    unread: bool = False,
    until: datetime | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    payload = await mail_list(
        top=top,
        search=search,
        sender=sender,
        since=since,
        subject=subject,
        unread=unread,
        until=until,
        config=config,
    )
    return {
        "filters": payload["filters"],
        "items": payload["items"],
        "orderby": payload["orderby"],
        "top": payload["top"],
    }


async def mail_list(
    *,
    top: int = 10,
    folder: str | None = None,
    orderby: str | None = None,
    search: str | None = None,
    sender: str | None = None,
    since: datetime | None = None,
    subject: str | None = None,
    unread: bool = False,
    until: datetime | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """List messages from a well-known folder, a folder id, or the whole mailbox."""
    if top < 1:
        raise ValueError("--top must be >= 1")
    label = None if folder is None else folder.strip()
    if folder is not None and not label:
        raise ValueError("--folder cannot be empty")
    requested_sort = None if orderby is None else _validate_orderby(orderby)
    term = _validate_search(
        search,
        orderby=requested_sort,
        sender=sender,
        since=since,
        subject=subject,
        unread=unread,
        until=until,
    )
    if since is not None and until is not None and until <= since:
        raise ValueError("--until must be after --since")
    cfg = config or load_config()
    client = create_graph_client(cfg)
    well_known = None if label is None else _well_known_folder(label)
    target = well_known or label
    sort = requested_sort or _default_orderby(well_known)

    async def _fetch(
        folder_target: str | None, sort_label: str
    ) -> tuple[list[Any], int | None, bool | None]:
        # Graph rejects $search alongside $filter or $orderBy, so a search is a
        # relevance-ranked query on its own rather than one more clause.
        if term is not None:
            page = await _get_messages(
                client, folder_target, top=top, sort=None, criteria=None, search=term
            )
            found = [] if page is None else (page.value or [])
            # We asked for `top` and got one page. That is not a scan of the match set,
            # so leave scanned/complete null — a consumer must not read
            # `complete: true, scanned: 3` as "an exhaustive search found three".
            return list(found), None, None
        criteria = _build_filter(
            field=_ORDERBY_FIELDS[sort_label],
            since=since,
            unread=unread,
            until=until,
        )
        return await _scan_messages(
            client,
            folder_target,
            top=top,
            sort=sort_label,
            criteria=criteria,
            sender=sender,
            subject=subject,
        )

    try:
        items, scanned, complete = await _fetch(target, sort)
    except ODataError as exc:
        # An exact well-known name cannot be a stale id, so only fall back for
        # free-form labels: a real folder of that display name wins over the alias.
        if label is None or well_known is not None or not _is_id_lookup_failure(exc):
            raise
        target, well_known, truncated = await _resolve_folder_fallback(client, label)
        if target is None:
            raise MailFolderNotFoundError(
                _folder_not_found_message(label, truncated=truncated)
            ) from exc
        sort = requested_sort or _default_orderby(well_known)
        items, scanned, complete = await _fetch(target, sort)
    matched_locally = term is None and bool(sender or subject)
    return {
        "filters": {
            "complete": complete,
            "from": sender,
            "matched_locally": matched_locally,
            "scanned": scanned,
            "search": term,
            "since": _odata_datetime(since),
            "subject": subject,
            "unread": unread,
            "until": _odata_datetime(until),
        },
        "folder": target,
        "items": [_message_to_dict(msg) for msg in items],
        "orderby": None if term else sort,
        "outbound": well_known in _OUTBOUND_FOLDERS,
        "top": top,
    }


async def mail_reply(
    *,
    message_id: str,
    body: str | None = None,
    body_file: str | None = None,
    body_type: str = "text",
    reply_all: bool = False,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Create a reply draft through Graph so it threads with the original.

    Composing a fresh draft with "RE:" prepended looks the same in the sender's outbox
    but is not a reply: it carries no conversation, and Exchange has nothing from which
    to write In-Reply-To and References on send, so it opens a new thread in the
    recipient's client. Graph's createReply puts the draft in the original conversation
    and inherits the recipients and subject.
    """
    mid = message_id.strip()
    if not mid:
        raise ValueError("--id is required")
    comment = _resolve_comment(body=body, body_file=body_file, body_type=body_type)
    cfg = config or load_config()
    client = create_graph_client(cfg)
    item = client.me.messages.by_message_id(mid)
    if reply_all:
        created = await _create_draft_from(
            item.create_reply_all, CreateReplyAllPostRequestBody(comment=comment), message_id=mid
        )
    else:
        created = await _create_draft_from(
            item.create_reply, CreateReplyPostRequestBody(comment=comment), message_id=mid
        )
    kind = "reply-all" if reply_all else "reply"
    return {"draft": _draft_summary(created, source=mid, kind=kind)}


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
    attach: Sequence[str] = (),
    bcc: str | Sequence[str] | None = None,
    subject: str | None = None,
    body: str | None = None,
    body_file: str | None = None,
    body_type: str = "text",
    cc: str | Sequence[str] | None = None,
    to: str | Sequence[str] | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if not draft_id.strip():
        raise ValueError("--id is required")
    has_body = body is not None or body_file is not None
    to_addrs = _parse_addresses(to, flag="--to", required=False)
    cc_addrs = _parse_addresses(cc, flag="--cc", required=False)
    bcc_addrs = _parse_addresses(bcc, flag="--bcc", required=False)
    if (
        subject is None
        and not has_body
        and to_addrs is None
        and cc_addrs is None
        and bcc_addrs is None
        and not attach
    ):
        raise ValueError(
            "provide at least one of --subject, --body/--body-file, --to, --cc, --bcc, or --attach"
        )
    pending = [_read_attachment(path) for path in attach]
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
    # Validate field changes before any upload so a usage error cannot leave new
    # attachments behind (a retry would then silently duplicate them).
    patch = Message()
    if subject is not None:
        if not subject.strip():
            raise ValueError("--subject must be non-empty when provided")
        patch.subject = subject.strip()
    if content is not None and graph_body_type is not None:
        patch.body = ItemBody(content_type=graph_body_type, content=content)
    if to_addrs is not None:
        patch.to_recipients = _recipient_models(to_addrs)
    if cc_addrs is not None:
        patch.cc_recipients = _recipient_models(cc_addrs)
    if bcc_addrs is not None:
        patch.bcc_recipients = _recipient_models(bcc_addrs)
    # Upload before PATCH so a failed --attach batch cannot leave subject/body/to changed
    # while the CLI exits with an error (mail draft deletes the whole draft instead).
    uploaded = await _upload_attachments(client, mid, pending)
    recipients_patched = to_addrs is not None or cc_addrs is not None or bcc_addrs is not None
    if subject is None and content is None and not recipients_patched:
        # --attach on its own: an empty PATCH would be a pointless round trip.
        updated = existing
    else:
        try:
            updated = await client.me.messages.by_message_id(mid).patch(patch)
            if updated is None:
                # Empty 2xx body — re-fetch so JSON/human output reflects post-PATCH state.
                updated = await client.me.messages.by_message_id(mid).get()
        except Exception:
            await _delete_uploaded_attachments(client, mid, uploaded)
            raise
    if updated is None:
        await _delete_uploaded_attachments(client, mid, uploaded)
        raise RuntimeError(f"Graph returned no message after update-draft: {mid}")
    body_out = body_type_label
    if body_out is None and updated.body and updated.body.content_type is not None:
        body_out = "html" if updated.body.content_type == BodyType.Html else "text"
    return {
        "draft": {
            "attachments": uploaded,
            "bcc": _recipient_field(bcc_addrs, getattr(updated, "bcc_recipients", None)),
            "body_type": body_out or "text",
            "cc": _recipient_field(cc_addrs, getattr(updated, "cc_recipients", None)),
            "id": updated.id or mid,
            "subject": updated.subject if updated.subject is not None else existing.subject,
            "to": _recipient_field(to_addrs, getattr(updated, "to_recipients", None)) or "",
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


_FOLDER_PAGE_SIZE = 100
# Graph's id-shaped complaints, shared by the folder and message lookups: both mean
# "that id does not name anything" rather than "your query was wrong".
_ID_LOOKUP_ERROR_CODES = frozenset(
    {
        "errorfoldernotfound",
        "errorinvalididmalformed",
        "erroritemnotfound",
        "resourcenotfound",
    }
)
# Graph accepts an attachment inline on the attachments collection only up to 3 MB of
# request body; past that it wants an upload session, which this CLI does not implement.
# Base64 inflates by 4/3, and the JSON wrapper around contentBytes eats more, so keep
# the file itself well under the limit rather than sitting on the exact boundary.
_MAX_ATTACHMENT_BYTES = 2_000_000
# Guardrails for the recursive folder walk: mailboxes with deep or huge folder trees
# should return a useful listing rather than fan out into hundreds of Graph calls.
_MAX_FOLDERS = 500
_MAX_FOLDER_DEPTH = 6
# Cap on the local text scan, applied at page boundaries. Graph returns roughly 100
# messages per request, so this bounds a miss to a handful of round-trips: deep enough
# for the recent mail people ask about, shallow enough that a wrong guess does not hang
# for a minute. --search reaches the whole mailbox server-side when this is not enough.
_MAX_SCANNED = 500
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
_SCAN_PAGE_SIZE = 100
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


def _build_filter(
    *,
    field: str,
    since: datetime | None,
    unread: bool,
    until: datetime | None,
) -> str | None:
    """Assemble the ``$filter`` clauses, or None when nothing was asked for.

    Only comparisons live here. Graph rejects a string function such as ``contains``
    alongside ``$orderby`` with ``InefficientFilter``, whether or not the query is
    scoped to a folder, so text matching happens in ``_scan_messages`` instead.

    Date bounds apply to ``field`` — the same property the listing is sorted by — so
    "mail from last week" means the same thing in Drafts, where receivedDateTime is
    null, as it does in the Inbox.
    """
    clauses: list[str] = []
    if unread:
        clauses.append("isRead eq false")
    if since is not None:
        clauses.append(f"{field} ge {_odata_datetime(since)}")
    if until is not None:
        # Half-open [since, until), matching `calendar view`'s date range.
        clauses.append(f"{field} lt {_odata_datetime(until)}")
    return " and ".join(clauses) or None


async def _collect_attachments(client: Any, message_id: str) -> list[dict[str, Any]]:
    query = AttachmentsRequestBuilder.AttachmentsRequestBuilderGetQueryParameters(
        select=["id", "name", "size", "contentType", "isInline"],
    )
    builder = client.me.messages.by_message_id(message_id).attachments
    page = await builder.get(request_config(query))
    raw: list[Any] = []
    while page is not None:
        raw.extend(page.value or [])
        link = getattr(page, "odata_next_link", None)
        if not link:
            break
        page = await builder.with_url(link).get()
    return [_attachment_to_dict(att) for att in raw]


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
    # Child folders are fetched through their own builder, whose query parameters are a
    # distinct (identically shaped) class; reusing the parent's would be a type mismatch.
    query_type = (
        MailFoldersRequestBuilder.MailFoldersRequestBuilderGetQueryParameters
        if depth == 0
        else ChildFoldersRequestBuilder.ChildFoldersRequestBuilderGetQueryParameters
    )
    query = query_type(
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


async def _create_draft_from(builder: Any, request: Any, *, message_id: str) -> Any:
    """POST a create-reply/forward action, mapping a rejected id to not-found."""
    try:
        created = await builder.post(request)
    except ODataError as exc:
        if not _is_id_lookup_failure(exc):
            raise
        raise MailMessageNotFoundError(f"message not found: {message_id}") from exc
    if created is None or not getattr(created, "id", None):
        raise RuntimeError(f"Graph returned no draft for message: {message_id}")
    return created


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


def _draft_summary(created: Any, *, source: str, kind: str) -> dict[str, Any]:
    body_type = "text"
    if created.body is not None and created.body.content_type == BodyType.Html:
        body_type = "html"
    # Same shape as mail.draft / mail.update-draft: a single string, not a list. Reply-all
    # may inherit several recipients, so join them rather than dropping all but the first.
    to_addrs = [
        person["email"]
        for person in _participants(getattr(created, "to_recipients", None))
        if person.get("email")
    ]
    return {
        "body_type": body_type,
        "conversation_id": getattr(created, "conversation_id", None),
        "id": created.id,
        "kind": kind,
        "source_message_id": source,
        "subject": created.subject,
        "to": ", ".join(to_addrs),
    }


def _filter_notes(payload: dict[str, Any]) -> list[str]:
    """One line naming the active filters, so a short result set explains itself."""
    filters = payload.get("filters") or {}
    parts = [
        f"{label}={sanitize_terminal(str(filters.get(key)))!r}"
        for label, key in (
            ("search", "search"),
            ("from", "from"),
            ("subject", "subject"),
            ("since", "since"),
            ("until", "until"),
        )
        if filters.get(key)
    ]
    if filters.get("unread"):
        parts.append("unread only")
    if not parts:
        return []
    lines = [f"  filters: {', '.join(parts)}"]
    # Only `complete is False` (hit the scan cap). `None` means we filled `--top`
    # without walking the rest — that is not the same claim, so stay quiet.
    if filters.get("matched_locally") and filters.get("complete") is False:
        # Silence here would read as "no more mail from them", which is a different claim.
        lines.append(
            f"  (stopped after scanning {filters.get('scanned')} messages; "
            "narrow with --since, or use --search to reach the whole mailbox)"
        )
    return lines


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


def _format_participant(person: dict[str, Any]) -> str:
    name = sanitize_terminal(str(person.get("name") or ""))
    email = sanitize_terminal(str(person.get("email") or ""))
    if name and email:
        return f"{name} <{email}>"
    return name or email


async def _get_messages(
    client: Any,
    folder: str | None,
    *,
    top: int,
    sort: str | None,
    criteria: str | None = None,
    search: str | None = None,
) -> Any:
    query = MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters(
        top=top,
        filter=criteria,
        search=None if search is None else f'"{search}"',
        orderby=None if sort is None else [f"{_ORDERBY_FIELDS[sort]} desc"],
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


def _is_id_lookup_failure(exc: ODataError) -> bool:
    """True when Graph rejected an id in the path rather than the query itself.

    A bare 400 is not enough: Graph also returns it for query-level problems such as
    a --top above its cap, and reporting those as a missing folder or message sends
    the operator after the wrong thing. Only id-shaped complaints count.
    """
    status = getattr(exc, "response_status_code", None)
    code = str(getattr(getattr(exc, "error", None), "code", "") or "").casefold()
    if code in _ID_LOOKUP_ERROR_CODES:
        return True
    return status == 404


def _matches_text(msg: Any, *, sender: str | None, subject: str | None) -> bool:
    """Case-insensitive substring match on sender (name or address) and subject."""
    if subject:
        if subject.casefold() not in str(getattr(msg, "subject", "") or "").casefold():
            return False
    if sender:
        email = getattr(getattr(msg, "from_", None), "email_address", None)
        needle = sender.casefold()
        # Checked per field: joining them would let a query straddle the boundary and
        # match text that appears in neither the address nor the name.
        fields = (str(getattr(email, attr, "") or "").casefold() for attr in ("address", "name"))
        if not any(needle in field for field in fields):
            return False
    return True


def _message_detail(msg: Any, *, wanted: MailBodyType) -> dict[str, Any]:
    """Shape one message for ``mail get``, honouring the requested body type.

    Graph normally converts the body via the ``Prefer`` header, but the response says
    what it actually sent, so trust that and convert locally only when the two differ.
    """
    body = None
    body_type: MailBodyType = wanted
    if msg.body is not None and msg.body.content:
        body = str(msg.body.content)
        returned: MailBodyType = "html" if msg.body.content_type == BodyType.Html else "text"
        if returned == "html" and wanted == "text":
            body = _html_to_text(body)
        else:
            body_type = returned
    from_email = None
    from_name = None
    if msg.from_ and msg.from_.email_address:
        from_email = msg.from_.email_address.address
        from_name = msg.from_.email_address.name
    created = getattr(msg, "created_date_time", None)
    sent = getattr(msg, "sent_date_time", None)
    received = getattr(msg, "received_date_time", None)
    return {
        "attachments": [],
        "body": body,
        "body_preview": msg.body_preview,
        "body_type": body_type,
        "cc": _participants(getattr(msg, "cc_recipients", None)),
        "conversation_id": getattr(msg, "conversation_id", None),
        "created": str(created) if created else None,
        "from_email": from_email,
        "from_name": from_name,
        "has_attachments": bool(msg.has_attachments),
        "id": msg.id,
        "internet_message_id": getattr(msg, "internet_message_id", None),
        "is_draft": bool(getattr(msg, "is_draft", False)),
        "is_read": bool(msg.is_read),
        "received": str(received) if received else None,
        "sent": str(sent) if sent else None,
        "subject": msg.subject,
        "to": _participants(getattr(msg, "to_recipients", None)),
        "web_link": getattr(msg, "web_link", None),
    }


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


def _odata_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _participants(recipients: Any) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    for recipient in recipients or []:
        email = getattr(recipient, "email_address", None)
        if email is None:
            continue
        address = getattr(email, "address", None)
        name = getattr(email, "name", None)
        if address or name:
            people.append({"email": address, "name": name})
    return people


def _parse_body_type(raw: str) -> MailBodyType:
    label = raw.strip().lower()
    if label not in {"html", "text"}:
        raise ValueError("--body-type must be 'text' or 'html'")
    return label  # type: ignore[return-value]


def _join_addresses(addresses: Sequence[str]) -> str:
    return ", ".join(addresses)


def _parse_addresses(
    value: str | Sequence[str] | None,
    *,
    flag: str,
    required: bool,
) -> list[str] | None:
    """Split repeatable and/or comma-separated address flags into a clean list.

    ``None`` means the flag was omitted (update-draft: leave unchanged). An empty
    provided value is a usage error — clearing a list is not supported yet.
    """
    if value is None:
        if required:
            raise ValueError(f"{flag} is required")
        return None
    parts: Sequence[str] = (value,) if isinstance(value, str) else value
    addresses: list[str] = []
    for raw in parts:
        for piece in str(raw).split(","):
            addr = piece.strip()
            if addr:
                addresses.append(addr)
    if not addresses:
        if required:
            raise ValueError(f"{flag} is required")
        # Empty sequence (draft defaults) means "no recipients", not "omit".
        return []
    return addresses


def _primary_to_address(msg: Any) -> str | None:
    recipients = getattr(msg, "to_recipients", None) or []
    for recipient in recipients:
        email = getattr(recipient, "email_address", None)
        address = getattr(email, "address", None) if email is not None else None
        if address:
            return str(address)
    return None


def _recipient_field(provided: list[str] | None, graph_recipients: Any) -> str | None:
    """Prefer addresses just patched; otherwise join what Graph returned."""
    if provided is not None:
        return _join_addresses(provided) or None
    addresses = [
        str(person["email"]) for person in _participants(graph_recipients) if person.get("email")
    ]
    return _join_addresses(addresses) or None


def _recipient_models(addresses: Sequence[str]) -> list[Recipient]:
    return [Recipient(email_address=EmailAddress(address=addr)) for addr in addresses]


def _read_attachment(path: str) -> tuple[str, bytes]:
    """Read one ``--attach`` file into (name, bytes), rejecting what Graph would reject."""
    source = Path(path)
    # is_file() is False for directories, devices, and FIFOs — those would hang or OOM on
    # read_bytes (/dev/zero, an unwritten pipe) even when st_size looks small.
    if not source.is_file():
        if source.is_dir():
            raise MailAttachError(f"--attach must name a file, not a directory: {path}")
        if not source.exists():
            raise MailAttachError(f"--attach file not found: {path}")
        raise MailAttachError(f"--attach must name a regular file: {path}")
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise MailAttachError(f"--attach file could not be read: {path}: {exc}") from exc
    if size >= _MAX_ATTACHMENT_BYTES:
        raise MailAttachError(
            f"--attach file is too large for a single request "
            f"({size} bytes >= {_MAX_ATTACHMENT_BYTES}): {path}"
        )
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise MailAttachError(f"--attach file could not be read: {path}: {exc}") from exc
    # The file may have grown between the size check and the read.
    if len(raw) >= _MAX_ATTACHMENT_BYTES:
        raise MailAttachError(
            f"--attach file is too large for a single request "
            f"({len(raw)} bytes >= {_MAX_ATTACHMENT_BYTES}): {path}"
        )
    return sanitize_attachment_filename(source.name), raw


def _resolve_comment(*, body: str | None, body_file: str | None, body_type: str) -> str:
    """Resolve the reply/forward text, which is optional: an empty draft is editable.

    Graph takes this as a ``comment`` rather than a body so the quoted original survives;
    replacing the body would produce a message that threads but reads like a fresh one.
    The draft body is always HTML (quoted original), so plain ``--body-type text`` must
    be escaped before Graph embeds it.
    """
    if body is None and body_file is None:
        _parse_body_type(body_type)
        return ""
    content, label, _graph_type = resolve_mail_body(
        body=body, body_file=body_file, body_type=body_type
    )
    if label == "text":
        return html_lib.escape(content)
    return content


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


async def _scan_messages(
    client: Any,
    folder: str | None,
    *,
    top: int,
    sort: str,
    criteria: str | None,
    sender: str | None,
    subject: str | None,
) -> tuple[list[Any], int | None, bool | None]:
    """Return up to ``top`` matches, how many messages were read, and whether that was all.

    Without ``--from`` / ``--subject`` this is a single ordered page, and ``scanned`` /
    ``complete`` stay null: we did not walk the rest of the mailbox, so claiming
    completeness would lie. With them it walks the ordered results applying the
    substring match locally, because Graph will not sort a query containing
    ``contains``. Newest-first order is what makes that tractable: the wanted
    message is usually near the front, and the scan stops at ``_MAX_SCANNED``
    rather than reading an entire mailbox. Filling ``--top`` early also leaves
    ``complete`` null — that stop is not an exhaustive match set either.
    """
    wants_text = bool(sender or subject)
    if not wants_text:
        page = await _get_messages(client, folder, top=top, sort=sort, criteria=criteria)
        found = [] if page is None else (page.value or [])
        return list(found), None, None
    matches: list[Any] = []
    scanned = 0
    page = await _get_messages(client, folder, top=_SCAN_PAGE_SIZE, sort=sort, criteria=criteria)
    builder = (
        client.me.messages
        if folder is None
        else client.me.mail_folders.by_mail_folder_id(folder).messages
    )
    while page is not None:
        for msg in page.value or []:
            scanned += 1
            if _matches_text(msg, sender=sender, subject=subject):
                matches.append(msg)
                if len(matches) >= top:
                    # Filled `--top` without seeing the rest of the mailbox — do not
                    # claim completeness (same honesty as a `--search` page).
                    return matches, scanned, None
        link = getattr(page, "odata_next_link", None)
        if not link:
            # Reaching the end is complete even at the cap: nothing was left unread.
            return matches, scanned, True
        if scanned >= _MAX_SCANNED:
            return matches, scanned, False
        page = await builder.with_url(str(link)).get()
    return matches, scanned, True


async def _delete_uploaded_attachments(
    client: Any, message_id: str, uploaded: Sequence[dict[str, Any]]
) -> None:
    """Best-effort undo for attachments posted in the current call."""
    if not uploaded:
        return
    builder = client.me.messages.by_message_id(message_id).attachments
    for item in uploaded:
        aid = item.get("id")
        if not aid:
            continue
        try:
            await builder.by_attachment_id(aid).delete()
        except Exception:
            pass


async def _upload_attachments(
    client: Any, message_id: str, pending: Sequence[tuple[str, bytes]]
) -> list[dict[str, Any]]:
    """Attach already-read files to a draft, one Graph call each, in the order given.

    On any failure, already-uploaded attachments from this call are deleted so a retry
    does not silently duplicate them. Callers that created the draft for this upload
    should still delete the draft itself.
    """
    if not pending:
        return []
    builder = client.me.messages.by_message_id(message_id).attachments
    uploaded: list[dict[str, Any]] = []
    try:
        for name, raw in pending:
            created = await builder.post(
                FileAttachment(
                    odata_type="#microsoft.graph.fileAttachment",
                    content_bytes=raw,
                    name=name,
                )
            )
            uploaded.append(
                {
                    "id": getattr(created, "id", None),
                    "name": getattr(created, "name", None) or name,
                    "size": getattr(created, "size", None) if created is not None else None,
                }
            )
    except Exception:
        await _delete_uploaded_attachments(client, message_id, uploaded)
        raise
    return uploaded


def _validate_orderby(raw: str) -> str:
    label = raw.strip().casefold()
    if label not in _ORDERBY_FIELDS:
        allowed = ", ".join(f"'{name}'" for name in sorted(_ORDERBY_FIELDS))
        raise ValueError(f"--orderby must be one of {allowed}")
    return label


def _validate_search(
    raw: str | None,
    *,
    orderby: str | None,
    sender: str | None,
    since: datetime | None,
    subject: str | None,
    unread: bool,
    until: datetime | None,
) -> str | None:
    """Check --search against the two combinations Graph refuses to serve.

    Graph answers ``SearchWithFilter`` and ``SearchWithOrderBy`` for these, so catching
    them here turns an opaque Graph error into a message naming the flag to drop.
    """
    if raw is None:
        return None
    term = raw.strip()
    if not term:
        raise ValueError("--search cannot be empty")
    if '"' in term:
        # $search takes a double-quoted KQL string with no escape for an inner quote.
        raise ValueError("--search cannot contain a double quote")
    conflicting = [
        name
        for name, used in (
            ("--from", sender is not None),
            ("--since", since is not None),
            ("--subject", subject is not None),
            ("--unread", unread),
            ("--until", until is not None),
        )
        if used
    ]
    if conflicting:
        raise ValueError(
            f"--search cannot be combined with {', '.join(conflicting)}: Graph rejects "
            "$search alongside $filter. Search for the term alone, or filter without it"
        )
    if orderby is not None:
        raise ValueError(
            "--search cannot be combined with --orderby: Graph rejects $search alongside "
            "$orderBy and returns matches by relevance"
        )
    return term


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
