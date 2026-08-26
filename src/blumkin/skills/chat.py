"""Teams chat read skills."""

from __future__ import annotations

import asyncio
import html as html_lib
import re
from collections.abc import Awaitable, Callable
from typing import Any

from msgraph.generated.users.item.chats.item.messages.messages_request_builder import (
    MessagesRequestBuilder,
)

from blumkin.config import BlumkinConfig, load_config
from blumkin.graph import create_graph_client, request_config
from blumkin.output import sanitize_terminal

_MEMBER_FETCH_CONCURRENCY = 8
_SKIPPED = object()
_TAG_RE = re.compile(r"<[^>]+>")


async def chat_find(
    *,
    with_name: str,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    needle = with_name.strip().lower()
    if not needle:
        raise ValueError("--with requires a non-empty display name")
    cfg = config or load_config()
    client = create_graph_client(cfg)
    chats = await _collect_pages(
        await client.me.chats.get(),
        lambda url: client.me.chats.with_url(url).get(),
    )
    sem = asyncio.Semaphore(_MEMBER_FETCH_CONCURRENCY)
    skip_errors: list[BaseException] = []

    async def _match(chat: Any) -> Any:
        chat_id = chat.id
        if not chat_id:
            return None
        async with sem:
            try:
                members_page = await client.me.chats.by_chat_id(chat_id).members.get()
                members = await _collect_pages(
                    members_page,
                    lambda url: client.me.chats.by_chat_id(chat_id).members.with_url(url).get(),
                )
            except Exception as exc:
                if _is_reauth_error(exc):
                    raise
                skip_errors.append(exc)
                # Skip chats Graph refuses (403 ACL, throttle, lost access); keep searching.
                return _SKIPPED
        member_names = [
            str(name) for member in members if (name := getattr(member, "display_name", None))
        ]
        if not any(_name_matches(needle, name) for name in member_names):
            return None
        return {
            "chat_type": str(chat.chat_type) if chat.chat_type is not None else None,
            "id": chat_id,
            "members": sorted(member_names),
            "topic": chat.topic,
        }

    matched = await asyncio.gather(*[_match(chat) for chat in chats])
    skipped = sum(1 for item in matched if item is _SKIPPED)
    attempted = sum(1 for chat in chats if chat.id)
    matches = [item for item in matched if item is not None and item is not _SKIPPED]
    if attempted and skipped == attempted and not matches:
        if skip_errors and all(
            getattr(exc, "response_status_code", None) == 403 for exc in skip_errors
        ):
            raise skip_errors[-1]
        raise RuntimeError(f"Graph member fetch failed for all {skipped} chats")
    matches.sort(key=_chat_sort_key)
    return {
        "items": matches,
        "partial": skipped > 0,
        "query": with_name,
        "skipped": skipped,
    }


async def chat_last(
    *,
    with_name: str,
    n: int = 3,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if n < 1:
        raise ValueError("--n must be >= 1")
    found = await chat_find(with_name=with_name, config=config)
    items = found["items"]
    if not items:
        return {
            "chat": None,
            "items": [],
            "partial": found["partial"],
            "query": with_name,
            "skipped": found["skipped"],
        }
    chat = items[0]
    cfg = config or load_config()
    client = create_graph_client(cfg)
    chat_id = chat["id"]
    query = MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters(
        orderby=["createdDateTime desc"],
        top=50,
    )
    page = await client.me.chats.by_chat_id(chat_id).messages.get(request_config(query))
    # Newest-first via $orderby; stop once we have n ordinary messages.
    selected: list[dict[str, Any]] = []
    while page is not None and len(selected) < n:
        for msg in page.value or []:
            msg_type = getattr(msg, "message_type", None)
            if msg_type is not None and str(msg_type) != "message":
                continue
            selected.append(_message_to_dict(msg))
            if len(selected) >= n:
                break
        if len(selected) >= n:
            break
        link = getattr(page, "odata_next_link", None)
        if not link:
            break
        page = await client.me.chats.by_chat_id(chat_id).messages.with_url(link).get()
    return {
        "chat": chat,
        "items": selected,
        "partial": found["partial"],
        "query": with_name,
        "skipped": found["skipped"],
    }


def format_find_human(payload: dict[str, Any]) -> list[str]:
    lines = [f"Chats matching {payload['query']!r}: {len(payload['items'])}"]
    skipped = int(payload.get("skipped") or 0)
    if skipped:
        lines.append(f"  (skipped {skipped} chat(s) due to Graph errors; results may be partial)")
    if not payload["items"]:
        lines.append("  (none)")
        return lines
    for item in payload["items"]:
        topic = sanitize_terminal(str(item.get("topic") or "(no topic)"))
        members = sanitize_terminal(", ".join(item.get("members") or []))
        lines.append(f"  • {topic} [{item.get('chat_type')}] members={members}")
        lines.append(f"    id={item.get('id')}")
    return lines


def format_last_human(payload: dict[str, Any]) -> list[str]:
    chat = payload.get("chat")
    skipped = int(payload.get("skipped") or 0)
    if chat is None:
        lines = [f"No chat matched {payload['query']!r}"]
        if skipped:
            lines.append(
                f"  (skipped {skipped} chat(s) due to Graph errors; results may be partial)"
            )
        return lines
    topic = sanitize_terminal(str(chat.get("topic") or "(no topic)"))
    lines = [f"Last messages in {topic!r} ({chat.get('id')}):"]
    if skipped:
        lines.append(f"  (skipped {skipped} chat(s) while matching; results may be partial)")
    if not payload["items"]:
        lines.append("  (none)")
        return lines
    for item in payload["items"]:
        who = sanitize_terminal(str(item.get("from_name") or "(unknown)"))
        text = sanitize_terminal(str(item.get("body_text") or ""))
        lines.append(f"  • {item.get('created')} {who}: {text}")
    return lines


def _chat_sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
    # Prefer 1:1 chats, then topic / id for stability.
    chat_type = (item.get("chat_type") or "").lower()
    prefer = 0 if "oneonone" in chat_type else 1
    return (prefer, str(item.get("topic") or ""), str(item.get("id") or ""))


async def _collect_pages(
    first_page: Any,
    fetch_next: Callable[[str], Awaitable[Any]],
) -> list[Any]:
    """Walk ``@odata.nextLink`` until exhausted; return concatenated ``value`` lists."""
    items: list[Any] = []
    page = first_page
    while page is not None:
        items.extend(page.value or [])
        link = getattr(page, "odata_next_link", None)
        if not link:
            break
        page = await fetch_next(link)
    return items


def _html_to_text(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _is_reauth_error(exc: BaseException) -> bool:
    """True for HTTP 401 (expired token); re-raise so CLI maps to EXIT_AUTH."""
    return getattr(exc, "response_status_code", None) == 401


def _message_to_dict(msg: Any) -> dict[str, Any]:
    body = msg.body
    body_html = body.content if body is not None else None
    body_text = _html_to_text(body_html) if body_html else ""
    from_name = None
    from_user = None
    if msg.from_ and msg.from_.user:
        from_name = msg.from_.user.display_name
        from_user = msg.from_.user.id
    return {
        "body_html": body_html,
        "body_text": body_text,
        "created": str(msg.created_date_time) if msg.created_date_time else None,
        "from_name": from_name,
        "from_user": from_user,
        "id": msg.id,
    }


def _name_matches(needle: str, display_name: str) -> bool:
    """Match when every whitespace token in ``needle`` appears in ``display_name``."""
    hay = display_name.lower()
    tokens = [t for t in needle.split() if t]
    if not tokens:
        return False
    return all(token in hay for token in tokens)
