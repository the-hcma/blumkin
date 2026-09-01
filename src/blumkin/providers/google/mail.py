"""Google Gmail read skills (skill-shaped payloads)."""

from __future__ import annotations

import base64
import email.utils
import html as html_lib
import re
from datetime import UTC, datetime
from email.header import decode_header, make_header
from typing import Any, Literal

from googleapiclient.errors import HttpError

from blumkin.attachments import (
    existing_entry_names,
    prepare_download_directory,
    resolve_attachment_dest,
    resolve_single_download_dest,
    sanitize_attachment_filename,
    unique_filename,
)
from blumkin.config import BlumkinConfig, load_config
from blumkin.providers.google_auth import get_credentials
from blumkin.providers.google_http import build_api_service, execute
from blumkin.skills.mail import (
    MailAttachmentNotFoundError,
    MailFolderNotFoundError,
    MailMessageNotFoundError,
)

MailBodyType = Literal["html", "text"]

# Gmail exposes system labels that behave like mail folders; the rest (STARRED,
# IMPORTANT, CATEGORY_*, …) are views, not containers, so they are left out of
# `mail folders`. Values are the display names used by the Microsoft provider.
_FOLDER_SYSTEM_LABELS = {
    "DRAFT": "Drafts",
    "INBOX": "Inbox",
    "SENT": "Sent Items",
    "SPAM": "Junk Email",
    "TRASH": "Deleted Items",
}
# Bound the per-label count fan-out (one labels.get each). Mailboxes rarely have
# this many labels; a bigger set is truncated like the Microsoft folder walk.
_MAX_MAIL_FOLDERS = 300


async def mail_attachments_download(
    *,
    message_id: str,
    out: str,
    attachment_id: str | None = None,
    download_all: bool = False,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    mid = message_id.strip()
    if not mid:
        raise ValueError("--message-id is required")
    if not out.strip():
        raise ValueError("--out is required")
    if download_all == bool(attachment_id and attachment_id.strip()):
        raise ValueError("provide exactly one of --attachment-id or --all")
    cfg = config or load_config()
    service = _gmail_service(cfg)
    attachments = _attachment_entries(_full_message(service, mid).get("payload") or {})
    if download_all:
        out_path = prepare_download_directory(out)
        targets = list(attachments)
        used_names = existing_entry_names(out_path)
    else:
        aid = attachment_id.strip() if attachment_id else ""
        match = next((item for item in attachments if item.get("id") == aid), None)
        if match is None:
            raise MailAttachmentNotFoundError(f"attachment not found: {aid}")
        targets = [match]
        out_path = resolve_single_download_dest(out, match.get("name") or aid)
        used_names = set()
    saved: list[dict[str, Any]] = []
    for meta in targets:
        aid = str(meta["id"])
        content = _attachment_bytes(_attachment_data(service, mid, aid))
        if download_all:
            filename = unique_filename(
                sanitize_attachment_filename(meta.get("name") or aid), used_names
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
    return {"message_id": mid, "saved": saved, "skipped": []}


async def mail_attachments_list(
    *,
    message_id: str,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    mid = message_id.strip()
    if not mid:
        raise ValueError("--id is required")
    cfg = config or load_config()
    service = _gmail_service(cfg)
    payload = _full_message(service, mid).get("payload") or {}
    return {"attachments": _attachment_entries(payload), "message_id": mid}


async def mail_folders(*, config: BlumkinConfig | None = None) -> dict[str, Any]:
    """List Gmail labels that behave as mail folders, with lag-prone message counts.

    ``total`` / ``unread`` come from ``labels.get`` (``messagesTotal`` /
    ``messagesUnread``), which trail the real mailbox; prove existence with
    ``mail list --folder`` / ``mail get``, not a zero count.
    """
    cfg = config or load_config()
    service = _gmail_service(cfg)
    listing = execute(service.users().labels().list(userId="me"))
    folders: list[dict[str, Any]] = []
    # users.labels.list is not paginated (it returns the whole label set), but
    # honor a nextPageToken defensively rather than claim completeness if one appears.
    truncated = bool(listing.get("nextPageToken"))
    for label in listing.get("labels") or []:
        label_id = label.get("id")
        if not isinstance(label_id, str):
            continue
        if label.get("type") == "system":
            path = _FOLDER_SYSTEM_LABELS.get(label_id)
            if path is None:
                continue
        else:
            path = str(label.get("name") or label_id)
        if len(folders) >= _MAX_MAIL_FOLDERS:
            truncated = True
            break
        # One label deleted mid-listing (404) or a transient error shouldn't sink
        # the whole listing — list the folder with unknown counts instead.
        try:
            detail = execute(service.users().labels().get(userId="me", id=label_id))
        except HttpError:
            detail = {}
        folders.append(
            {
                "id": label_id,
                "path": path,
                "total": detail.get("messagesTotal"),
                "unread": detail.get("messagesUnread"),
            }
        )
    folders.sort(key=lambda item: str(item["path"]).casefold())
    return {
        "counts_may_lag": True,
        "folders": folders,
        "limits": {"max_depth": None, "max_folders": _MAX_MAIL_FOLDERS},
        "truncated": truncated,
    }


async def mail_get(
    *,
    message_id: str,
    body_type: str = "text",
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if not message_id.strip():
        raise ValueError("--id is required")
    wanted = _parse_body_type(body_type)
    cfg = config or load_config()
    service = _gmail_service(cfg)
    mid = message_id.strip()
    try:
        raw = execute(service.users().messages().get(userId="me", id=mid, format="full"))
    except HttpError as exc:
        if _http_not_found(exc):
            raise MailMessageNotFoundError(f"message not found: {mid}") from exc
        raise
    return {"message": _message_detail(raw, wanted=wanted)}


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
        folder="inbox",
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
    if top < 1:
        raise ValueError("--top must be >= 1")
    if top > 500:
        raise ValueError("--top must be <= 500 (Gmail maxResults limit)")
    label = None if folder is None else folder.strip()
    if folder is not None and not label:
        raise ValueError("--folder cannot be empty")
    if since is not None and until is not None and until <= since:
        raise ValueError("--until must be after --since")
    if orderby is not None:
        key = orderby.strip().lower()
        if key not in {"created", "received", "sent"}:
            raise ValueError("--orderby must be created, received, or sent")
        raise ValueError(
            "--orderby is not supported for provider=google yet "
            "(Gmail messages.list returns recency/relevance order only)"
        )
    cfg = config or load_config()
    service = _gmail_service(cfg)
    well_known = None if label is None else label.casefold()
    label_ids = _label_ids_for_folder(well_known)
    if label is not None and well_known not in _FOLDER_LABELS:
        raise MailFolderNotFoundError(f"mail folder not found: {label}")
    if label is not None and well_known in _FOLDER_LABELS and _FOLDER_LABELS[well_known] is None:
        raise MailFolderNotFoundError(
            f"mail folder {label!r} not supported for provider=google yet"
        )

    query = _build_gmail_query(
        search=search,
        sender=sender,
        since=since,
        subject=subject,
        unread=unread,
        until=until,
    )
    list_kwargs: dict[str, Any] = {"userId": "me", "maxResults": top}
    if label_ids:
        list_kwargs["labelIds"] = label_ids
    if query:
        list_kwargs["q"] = query
    listing = execute(service.users().messages().list(**list_kwargs))
    refs = listing.get("messages") or []
    items: list[dict[str, Any]] = []
    for ref in refs:
        mid = ref.get("id")
        if not mid:
            continue
        msg = execute(
            service.users()
            .messages()
            .get(userId="me", id=mid, format="metadata", metadataHeaders=_LIST_HEADERS)
        )
        items.append(_message_to_dict(msg))

    # Single capped page: nextPageToken means the match set continues — do not claim
    # exhaustiveness (mirrors Microsoft null semantics for a truncated top page).
    truncated = bool(listing.get("nextPageToken"))
    return {
        "filters": {
            "complete": None if truncated else True,
            "from": sender,
            "matched_locally": False,
            "scanned": None if truncated else len(items),
            "search": search,
            "since": _iso_z(since),
            "subject": subject,
            "unread": unread,
            "until": _iso_z(until),
        },
        "folder": well_known or label,
        "items": items,
        "orderby": None,
        "outbound": well_known in _OUTBOUND_FOLDERS,
        "top": top,
    }


_FOLDER_LABELS = {
    "archive": None,
    "deleteditems": "TRASH",
    "drafts": "DRAFT",
    "inbox": "INBOX",
    "junkemail": "SPAM",
    "outbox": None,
    "sentitems": "SENT",
}

_LIST_HEADERS = ["From", "Subject", "To", "Date"]

_OUTBOUND_FOLDERS = frozenset({"drafts", "outbox", "sentitems"})


def _attachment_bytes(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode() + b"=" * (-len(data) % 4))


def _attachment_data(service: Any, message_id: str, attachment_id: str) -> str:
    fetched = execute(
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
    )
    data = fetched.get("data")
    if not isinstance(data, str):
        raise MailAttachmentNotFoundError(f"attachment not found: {attachment_id}")
    return data


def _attachment_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    _collect_attachment_parts(payload, out)
    return out


def _body_from_payload(
    payload: dict[str, Any], *, wanted: MailBodyType
) -> tuple[str | None, MailBodyType]:
    html_parts: list[str] = []
    text_parts: list[str] = []
    _collect_bodies(payload, html_parts=html_parts, text_parts=text_parts)
    if wanted == "html":
        if html_parts:
            return "".join(html_parts), "html"
        if text_parts:
            return html_lib.escape("\n".join(text_parts)), "html"
        return None, "html"
    if text_parts:
        return "\n".join(text_parts), "text"
    if html_parts:
        return _html_to_text("".join(html_parts)), "text"
    return None, "text"


def _build_gmail_query(
    *,
    search: str | None,
    sender: str | None,
    since: datetime | None,
    subject: str | None,
    unread: bool,
    until: datetime | None,
) -> str | None:
    parts: list[str] = []
    if search:
        parts.append(search.strip())
    if sender:
        parts.append(f"from:{_gmail_phrase(sender.strip())}")
    if subject:
        parts.append(f"subject:{_gmail_phrase(subject.strip())}")
    if unread:
        parts.append("is:unread")
    if since is not None:
        parts.append(f"after:{int(since.astimezone(UTC).timestamp())}")
    if until is not None:
        parts.append(f"before:{int(until.astimezone(UTC).timestamp())}")
    return " ".join(parts) if parts else None


def _collect_attachment_parts(part: dict[str, Any], out: list[dict[str, Any]]) -> None:
    body = part.get("body") or {}
    attachment_id = body.get("attachmentId")
    if isinstance(attachment_id, str) and attachment_id:
        headers = {
            str(h.get("name") or "").casefold(): str(h.get("value") or "")
            for h in (part.get("headers") or [])
            if isinstance(h, dict)
        }
        mime = str(part.get("mimeType") or "application/octet-stream")
        # filename is optional on Content-Disposition (RFC 2183); Gmail still
        # gives the part an attachmentId, so surface it under a fallback name
        # rather than dropping it silently.
        filename = str(part.get("filename") or "").strip() or attachment_id
        out.append(
            {
                "attachment_type": mime,
                "content_type": mime,
                "id": attachment_id,
                "is_inline": _is_inline_disposition(headers.get("content-disposition", ""))
                or "content-id" in headers,
                "name": filename,
                "size": body.get("size"),
                "skipped": False,
            }
        )
    for child in part.get("parts") or []:
        if isinstance(child, dict):
            _collect_attachment_parts(child, out)


def _collect_bodies(
    payload: dict[str, Any],
    *,
    html_parts: list[str],
    text_parts: list[str],
) -> None:
    mime = str(payload.get("mimeType") or "")
    body = payload.get("body") or {}
    data = body.get("data")
    if data and mime == "text/plain":
        text_parts.append(_decode_b64url(data))
    elif data and mime == "text/html":
        html_parts.append(_decode_b64url(data))
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            _collect_bodies(part, html_parts=html_parts, text_parts=text_parts)


def _decode_b64url(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="replace")


def _decode_header_value(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return str(make_header(decode_header(text)))
    except Exception:
        return text


def _full_message(service: Any, message_id: str) -> dict[str, Any]:
    try:
        return execute(service.users().messages().get(userId="me", id=message_id, format="full"))
    except HttpError as exc:
        if _http_not_found(exc):
            raise MailMessageNotFoundError(f"message not found: {message_id}") from exc
        raise


def _gmail_phrase(value: str) -> str:
    """Quote a Gmail operator value so multi-word phrases stay atomic."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _gmail_service(cfg: BlumkinConfig) -> Any:
    creds = get_credentials(cfg, allow_interactive=False)
    return build_api_service("gmail", "v1", creds=creds, config=cfg)


def _header_map(msg: dict[str, Any]) -> dict[str, str]:
    headers = ((msg.get("payload") or {}).get("headers")) or []
    out: dict[str, str] = {}
    for item in headers:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").casefold()
        value = item.get("value")
        if name and value is not None:
            out[name] = str(value)
    return out


def _html_to_text(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"(?s)<.*?>", "", text)
    return html_lib.unescape(text).strip()


def _http_not_found(exc: HttpError) -> bool:
    status = getattr(exc.resp, "status", None) if getattr(exc, "resp", None) else None
    return status == 404


def _is_inline_disposition(content_disposition: str) -> bool:
    """True when the Content-Disposition *directive* (not the whole header) is inline.

    A plain substring check would misclassify e.g. ``attachment; filename="inline-report.pdf"``.
    """
    directive = content_disposition.split(";", 1)[0].strip().casefold()
    return directive == "inline"


def _iso_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _label_ids_for_folder(well_known: str | None) -> list[str] | None:
    if well_known is None:
        return None
    label = _FOLDER_LABELS.get(well_known)
    if label is None:
        return None
    return [label]


def _message_detail(msg: dict[str, Any], *, wanted: MailBodyType) -> dict[str, Any]:
    headers = _header_map(msg)
    from_name, from_email = _parse_from(headers.get("from"))
    payload = msg.get("payload") or {}
    body, body_type = _body_from_payload(payload, wanted=wanted)
    label_ids = set(msg.get("labelIds") or [])
    received = _ms_to_iso(msg.get("internalDate"))
    sent = _parse_date_header(headers.get("date")) or received
    return {
        "attachments": [],
        "body": body,
        "body_preview": msg.get("snippet"),
        "body_type": body_type,
        "cc": _parse_address_list(headers.get("cc")),
        "conversation_id": msg.get("threadId"),
        "created": received,
        "from_email": from_email,
        "from_name": from_name,
        "has_attachments": _payload_has_attachments(payload),
        "id": msg.get("id"),
        "internet_message_id": headers.get("message-id"),
        "is_draft": "DRAFT" in label_ids,
        "is_read": "UNREAD" not in label_ids,
        "received": received,
        "sent": sent,
        "subject": _decode_header_value(headers.get("subject")),
        "to": _parse_address_list(headers.get("to")),
        "web_link": None,
    }


def _message_to_dict(msg: dict[str, Any]) -> dict[str, Any]:
    headers = _header_map(msg)
    from_name, from_email = _parse_from(headers.get("from"))
    label_ids = set(msg.get("labelIds") or [])
    received = _ms_to_iso(msg.get("internalDate"))
    sent = _parse_date_header(headers.get("date")) or received
    to_addrs = _parse_address_list(headers.get("to"))
    return {
        "body_html": None,
        "body_preview": msg.get("snippet"),
        "body_text": None,
        "created": received,
        "from_email": from_email,
        "from_name": from_name,
        # metadata format has headers only — attachment presence unknown
        "has_attachments": None,
        "id": msg.get("id"),
        "is_read": "UNREAD" not in label_ids,
        "received": received,
        "sent": sent,
        "subject": _decode_header_value(headers.get("subject")),
        "to_email": to_addrs[0]["email"] if to_addrs else None,
    }


def _ms_to_iso(raw: Any) -> str | None:
    if raw is None:
        return None
    try:
        ms = int(raw)
    except TypeError, ValueError:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).isoformat()


def _parse_address_list(raw: str | None) -> list[dict[str, str | None]]:
    if not raw:
        return []
    out: list[dict[str, str | None]] = []
    for name, addr in email.utils.getaddresses([raw]):
        if not addr:
            continue
        out.append({"email": addr, "name": name or None})
    return out


def _parse_body_type(raw: str) -> MailBodyType:
    label = raw.strip().lower()
    if label not in {"html", "text"}:
        raise ValueError("--body-type must be 'html' or 'text'")
    return label  # type: ignore[return-value]


def _parse_date_header(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _parse_from(raw: str | None) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    name, addr = email.utils.parseaddr(raw)
    return (name or None), (addr or None)


def _payload_has_attachments(payload: dict[str, Any]) -> bool:
    """True when any MIME part looks like a file attachment (not body text)."""
    filename = str(payload.get("filename") or "").strip()
    body = payload.get("body") or {}
    if filename or body.get("attachmentId"):
        return True
    for part in payload.get("parts") or []:
        if isinstance(part, dict) and _payload_has_attachments(part):
            return True
    return False
