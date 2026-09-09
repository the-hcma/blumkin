"""Probe whether Outlook auto-inserts a signature into drafts this account creates.

Microsoft Graph has no API for the client-side "add a signature to new messages
and replies" setting, so blumkin finds out empirically: create a draft with a
known one-line body, read it back, delete it. If Outlook's compose pipeline put
anything else in the body (text, or an image-only signature block), the account
has an auto-signature and ``append_mail_signature`` should stand down (see
``blumkin.mail_signature_state``).

The probe stays in the operator's own mailbox - create + delete a draft, never a
send - so it is safe to run from ``auth login`` / ``doctor``. A leftover probe
draft from a run whose delete failed is swept on the next run, so at most one can
sit in Drafts between runs. The probe covers Outlook's *new message* signature
only; the reply / forward toggle is configured separately (see issue #231).
"""

from __future__ import annotations

import re

from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.item_body import ItemBody
from msgraph.generated.models.message import Message
from msgraph.generated.users.item.messages.messages_request_builder import (
    MessagesRequestBuilder,
)

from blumkin.config import BlumkinConfig, load_config
from blumkin.graph import create_graph_client, request_config

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_EMBED_RE = re.compile(r"<(?:img|table|object|svg|video|audio)\b", re.IGNORECASE)
_PROBE_SENTINEL = "blumkin-signature-probe-marker"
_PROBE_SUBJECT = "blumkin signature probe (safe to delete)"
_STYLE_RE = re.compile(r"<(style|script)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_SWEEP_MAX = 25
_TAG_RE = re.compile(r"<[^>]+>")


async def probe_outlook_signature(config: BlumkinConfig | None = None) -> bool | None:
    """Create a throwaway draft, read it back, delete it; report whether Outlook signed it.

    ``True``  - the draft came back with body content blumkin did not send
                (Outlook added a signature).
    ``False`` - the draft body was exactly what blumkin sent.
    ``None``  - the probe could not run (no Mail scope, offline, Graph error);
                the caller then leaves the cached state untouched.
    """
    cfg = config or load_config()
    try:
        client = create_graph_client(cfg)
        await _sweep_probe_drafts(client)
        draft = await client.me.messages.post(
            Message(
                subject=_PROBE_SUBJECT,
                body=ItemBody(content_type=BodyType.Html, content=f"<p>{_PROBE_SENTINEL}</p>"),
            )
        )
        if draft is None or not draft.id:
            return None
        mid = draft.id
        try:
            fetched = await client.me.messages.by_message_id(mid).get()
        finally:
            await client.me.messages.by_message_id(mid).delete()
    except Exception:
        return None
    body = getattr(getattr(fetched, "body", None), "content", None)
    if not isinstance(body, str):
        return None
    return _body_carries_extra_content(body)


def _body_carries_extra_content(html: str) -> bool:
    """True when the round-tripped draft body has content beyond blumkin's sentinel.

    Catches a text signature and an image-only one (a logo with no text node).
    """
    if _EMBED_RE.search(html):
        return True
    stripped = _STYLE_RE.sub(" ", html)
    stripped = _COMMENT_RE.sub(" ", stripped)
    stripped = _TAG_RE.sub(" ", stripped)
    for entity in ("&nbsp;", "&#160;", "&zwnj;", "&#8203;"):
        stripped = stripped.replace(entity, " ")
    stripped = stripped.replace(_PROBE_SENTINEL, " ")
    return bool(stripped.split())


async def _sweep_probe_drafts(client: object) -> None:
    """Best-effort: delete any probe drafts a previous run failed to clean up."""
    query = MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters(
        filter=f"subject eq '{_PROBE_SUBJECT}'",
        select=["id"],
        top=_SWEEP_MAX,
    )
    page = await client.me.messages.get(request_config(query))  # type: ignore[attr-defined]
    for message in getattr(page, "value", None) or []:
        if message.id:
            await client.me.messages.by_message_id(message.id).delete()  # type: ignore[attr-defined]
