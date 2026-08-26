"""Mail read and draft skills."""

from __future__ import annotations

import html as html_lib
import re
from pathlib import Path
from typing import Any, Literal

from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.email_address import EmailAddress
from msgraph.generated.models.item_body import ItemBody
from msgraph.generated.models.message import Message
from msgraph.generated.models.recipient import Recipient
from msgraph.generated.users.item.messages.messages_request_builder import (
    MessagesRequestBuilder,
)

from blumkin.config import BlumkinConfig, load_config
from blumkin.graph import create_graph_client, request_config
from blumkin.output import sanitize_terminal

MailBodyType = Literal["html", "text"]


class MailBodyFileError(Exception):
    """--body-file could not be read (usage, not auth)."""


class MailDraftNotFoundError(Exception):
    """Draft id missing or not a draft (not_found)."""


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
        updated = existing
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
