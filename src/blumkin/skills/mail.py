"""Mail read skills."""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

from msgraph.generated.users.item.messages.messages_request_builder import (
    MessagesRequestBuilder,
)

from blumkin.config import BlumkinConfig, load_config
from blumkin.graph import create_graph_client, request_config
from blumkin.output import sanitize_terminal

_TAG_RE = re.compile(r"<[^>]+>")


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
