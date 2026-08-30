"""Google Gmail read skills (skill-shaped payloads)."""

from __future__ import annotations

import base64
import email.utils
import html as html_lib
import re
from datetime import UTC, datetime
from email.header import decode_header, make_header
from typing import Any, Literal

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from blumkin.config import BlumkinConfig, load_config
from blumkin.providers.google_auth import get_credentials
from blumkin.skills.mail import MailFolderNotFoundError, MailMessageNotFoundError

MailBodyType = Literal["html", "text"]


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
        raw = service.users().messages().get(userId="me", id=mid, format="full").execute()
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
    listing = service.users().messages().list(**list_kwargs).execute()
    refs = listing.get("messages") or []
    items: list[dict[str, Any]] = []
    for ref in refs:
        mid = ref.get("id")
        if not mid:
            continue
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=mid, format="metadata", metadataHeaders=_LIST_HEADERS)
            .execute()
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


def _gmail_phrase(value: str) -> str:
    """Quote a Gmail operator value so multi-word phrases stay atomic."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _gmail_service(cfg: BlumkinConfig) -> Any:
    creds = get_credentials(cfg, allow_interactive=False)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


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
    body, body_type = _body_from_payload(msg.get("payload") or {}, wanted=wanted)
    label_ids = set(msg.get("labelIds") or [])
    received = _ms_to_iso(msg.get("internalDate"))
    sent = _parse_date_header(headers.get("date")) or received
    return {
        "attachments": [],
        "body": body,
        "body_preview": msg.get("snippet"),
        "body_type": body_type,
        "cc": [],
        "conversation_id": msg.get("threadId"),
        "created": received,
        "from_email": from_email,
        "from_name": from_name,
        "has_attachments": False,
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
        "has_attachments": False,
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
