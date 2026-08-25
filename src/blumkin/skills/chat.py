"""Teams chat read skills."""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

from blumkin.config import BlumkinConfig, load_config
from blumkin.graph import create_graph_client

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
    matches: list[dict[str, Any]] = []
    chats = await client.me.chats.get()
    for chat in [] if chats is None else (chats.value or []):
        chat_id = chat.id
        if not chat_id:
            continue
        members = await client.me.chats.by_chat_id(chat_id).members.get()
        member_names: list[str] = []
        for member in [] if members is None else (members.value or []):
            name = getattr(member, "display_name", None)
            if name:
                member_names.append(str(name))
        if not any(_name_matches(needle, name) for name in member_names):
            continue
        matches.append(
            {
                "chat_type": str(chat.chat_type) if chat.chat_type is not None else None,
                "id": chat_id,
                "members": sorted(member_names),
                "topic": chat.topic,
            }
        )
    matches.sort(key=_chat_sort_key)
    return {"items": matches, "query": with_name}


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
        return {"chat": None, "items": [], "query": with_name}
    chat = items[0]
    cfg = config or load_config()
    client = create_graph_client(cfg)
    messages = await client.me.chats.by_chat_id(chat["id"]).messages.get()
    raw = [] if messages is None else (messages.value or [])
    # Graph returns newest-first; take first n ordinary message rows.
    selected: list[dict[str, Any]] = []
    for msg in raw:
        msg_type = getattr(msg, "message_type", None)
        if msg_type is not None and str(msg_type) != "message":
            continue
        selected.append(_message_to_dict(msg))
        if len(selected) >= n:
            break
    return {"chat": chat, "items": selected, "query": with_name}


def format_find_human(payload: dict[str, Any]) -> list[str]:
    lines = [f"Chats matching {payload['query']!r}: {len(payload['items'])}"]
    if not payload["items"]:
        lines.append("  (none)")
        return lines
    for item in payload["items"]:
        topic = item.get("topic") or "(no topic)"
        members = ", ".join(item.get("members") or [])
        lines.append(f"  • {topic} [{item.get('chat_type')}] members={members}")
        lines.append(f"    id={item.get('id')}")
    return lines


def format_last_human(payload: dict[str, Any]) -> list[str]:
    chat = payload.get("chat")
    if chat is None:
        return [f"No chat matched {payload['query']!r}"]
    topic = chat.get("topic") or "(no topic)"
    lines = [f"Last messages in {topic!r} ({chat.get('id')}):"]
    if not payload["items"]:
        lines.append("  (none)")
        return lines
    for item in payload["items"]:
        who = item.get("from_name") or "(unknown)"
        text = item.get("body_text") or ""
        lines.append(f"  • {item.get('created')} {who}: {text}")
    return lines


def _chat_sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
    # Prefer 1:1 chats, then topic / id for stability.
    chat_type = (item.get("chat_type") or "").lower()
    prefer = 0 if "oneonone" in chat_type else 1
    return (prefer, str(item.get("topic") or ""), str(item.get("id") or ""))


def _html_to_text(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _name_matches(needle: str, display_name: str) -> bool:
    """Match when every whitespace token in ``needle`` appears in ``display_name``."""
    hay = display_name.lower()
    tokens = [t for t in needle.split() if t]
    if not tokens:
        return False
    return all(token in hay for token in tokens)


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
