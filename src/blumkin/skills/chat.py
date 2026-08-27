"""Teams chat skills (read + write)."""

from __future__ import annotations

import asyncio
import base64
import html as html_lib
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from kiota_abstractions.method import Method
from kiota_abstractions.request_information import RequestInformation
from kiota_abstractions.serialization.parsable_factory import ParsableFactory
from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.chat_message import ChatMessage
from msgraph.generated.models.item_body import ItemBody
from msgraph.generated.models.o_data_errors.o_data_error import ODataError
from msgraph.generated.users.item.chats.item.messages.item.chat_message_item_request_builder import (  # noqa: E501
    ChatMessageItemRequestBuilder,
)
from msgraph.generated.users.item.chats.item.messages.messages_request_builder import (
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
from blumkin.auth import effective_scopes
from blumkin.config import BlumkinConfig, load_config
from blumkin.graph import create_graph_client, request_config
from blumkin.output import sanitize_terminal

# Teams stores chat files in SharePoint/OneDrive, so bytes come from the shares API
# rather than the chat message itself — that needs a delegated Files.* scope.
FILES_SCOPE_PREFIX = "Files."


class ChatAttachmentNotFoundError(Exception):
    """Attachment id missing on the chat message (not_found)."""


class ChatAttachmentScopeError(Exception):
    """Download needs a delegated Files.* scope the token does not hold (missing_scope)."""


class ChatAttachmentSkippedError(Exception):
    """Attachment is not a downloadable file (usage)."""


class ChatMessageNotFoundError(Exception):
    """Chat message id missing, or no message carries attachments (not_found)."""


async def chat_attachments_download(
    *,
    out: str,
    attachment_id: str | None = None,
    chat_id: str | None = None,
    download_all: bool = False,
    latest: bool = False,
    message_id: str | None = None,
    with_name: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if not out.strip():
        raise ValueError("--out is required")
    aid = (attachment_id or "").strip() or None
    if bool(aid) == bool(download_all):
        raise ValueError("provide exactly one of --attachment-id or --all")
    cfg = config or load_config()
    listed = await chat_attachments_list(
        chat_id=chat_id,
        latest=latest,
        message_id=message_id,
        with_name=with_name,
        config=cfg,
    )
    attachments = listed["attachments"]
    if download_all:
        targets = [item for item in attachments if item["downloadable"]]
        if not targets:
            # Exiting 0 with an empty directory would look like a successful download.
            raise ChatAttachmentNotFoundError(
                f"no downloadable file attachments on message {listed['message_id']!r} "
                f"({len(attachments)} attachment(s) present, none are chat files)"
            )
        out_path = prepare_download_directory(out)
    else:
        match = next((item for item in attachments if item["id"] == aid), None)
        if match is None:
            raise ChatAttachmentNotFoundError(f"attachment not found: {aid}")
        if not match["downloadable"]:
            raise ChatAttachmentSkippedError(match["skip_reason"] or "attachment is not a file")
        targets = [match]
        out_path = resolve_single_download_dest(out, match["name"] or str(match["id"]))
    client = create_graph_client(cfg)
    saved: list[dict[str, Any]] = []
    skipped = [
        {
            "id": item["id"],
            "name": item["name"],
            "reason": item["skip_reason"],
            "share_url": item["content_url"],
        }
        for item in attachments
        if not item["downloadable"] and download_all
    ]
    used_names = existing_entry_names(out_path) if download_all else set()
    for meta in targets:
        content_url = str(meta["content_url"])
        _require_files_scope(cfg, content_url)
        try:
            content = await _fetch_shared_item_bytes(client, content_url)
        except Exception as exc:
            # A 403 here means the share itself is denied (needs Files.ReadWrite, or an
            # ACL block). Re-raise with the URL so the operator can open it in a browser.
            if getattr(exc, "response_status_code", None) != 403:
                raise
            raise _files_access_denied(content_url, detail=str(exc)) from exc
        if download_all:
            filename = unique_filename(
                sanitize_attachment_filename(meta["name"] or str(meta["id"])),
                used_names,
            )
            dest = resolve_attachment_dest(out_path, filename)
        else:
            dest = out_path
        dest.write_bytes(content)
        saved.append(
            {
                "attachment_id": meta["id"],
                "content_type": meta["content_type"],
                "name": meta["name"],
                "saved_path": str(dest.resolve()),
                "share_url": content_url,
                "size": len(content),
            }
        )
    return {
        "chat": listed["chat"],
        "chat_id": listed["chat_id"],
        "message_id": listed["message_id"],
        "saved": saved,
        "skipped": skipped,
    }


async def chat_attachments_list(
    *,
    chat_id: str | None = None,
    latest: bool = False,
    message_id: str | None = None,
    with_name: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    mid = (message_id or "").strip() or None
    if bool(mid) == bool(latest):
        raise ValueError("provide exactly one of --message-id or --latest")
    cfg = config or load_config()
    chat, target_id, partial, skipped = await _resolve_chat_target(
        chat_id=chat_id, with_name=with_name, config=cfg
    )
    client = create_graph_client(cfg)
    message = (
        await _latest_message_with_attachments(client, target_id)
        if latest
        else await _require_chat_message(client, target_id, str(mid))
    )
    return {
        "attachments": [
            _attachment_to_dict(att) for att in (getattr(message, "attachments", None) or [])
        ],
        "chat": chat,
        "chat_id": target_id,
        "message": _message_to_dict(message),
        "message_id": message.id,
        "partial": partial,
        "skipped": skipped,
    }


async def chat_delete(
    *,
    chat_id: str,
    message_id: str,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    cid = chat_id.strip()
    mid = message_id.strip()
    if not cid or not mid:
        raise ValueError("--chat-id and --message-id are required")
    cfg = config or load_config()
    client = create_graph_client(cfg)
    await client.me.chats.by_chat_id(cid).messages.by_chat_message_id(mid).soft_delete.post()
    return {"chat_id": cid, "deleted": mid}


async def chat_edit(
    *,
    chat_id: str,
    message_id: str,
    text: str,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    cid = chat_id.strip()
    mid = message_id.strip()
    body_text = text.strip()
    if not cid or not mid:
        raise ValueError("--chat-id and --message-id are required")
    if not body_text:
        raise ValueError("--text must be non-empty")
    cfg = config or load_config()
    client = create_graph_client(cfg)
    patch = ChatMessage(body=ItemBody(content=body_text, content_type=BodyType.Text))
    updated = await client.me.chats.by_chat_id(cid).messages.by_chat_message_id(mid).patch(patch)
    if updated is None:
        updated = await client.me.chats.by_chat_id(cid).messages.by_chat_message_id(mid).get()
    if updated is None:
        raise RuntimeError("chat message patch returned empty response")
    return {"chat_id": cid, "message": _message_to_dict(updated)}


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
            if not _is_ordinary_message(msg):
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


async def chat_send(
    *,
    text: str,
    with_name: str | None = None,
    chat_id: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    body_text = text.strip()
    if not body_text:
        raise ValueError("--text must be non-empty")
    cfg = config or load_config()
    query = (with_name or "").strip() or None
    chat, target_id, partial, skipped = await _resolve_chat_target(
        chat_id=chat_id, with_name=with_name, config=cfg
    )
    client = create_graph_client(cfg)
    message = ChatMessage(body=ItemBody(content=body_text, content_type=BodyType.Text))
    created = await client.me.chats.by_chat_id(target_id).messages.post(message)
    if created is None:
        raise RuntimeError("chat message create returned empty response")
    return {
        "chat": chat,
        "message": _message_to_dict(created),
        "partial": partial,
        "query": query,
        "skipped": skipped,
    }


def format_attachments_download_human(payload: dict[str, Any]) -> list[str]:
    saved = payload.get("saved") or []
    lines = [f"Saved {len(saved)} chat attachment(s) from message {payload.get('message_id')!r}"]
    for item in saved:
        name = sanitize_terminal(str(item.get("name") or ""))
        saved_path = sanitize_terminal(str(item.get("saved_path") or ""))
        lines.append(f"  • {name!r} → {saved_path}")
    for item in payload.get("skipped") or []:
        name = sanitize_terminal(str(item.get("name") or item.get("id") or ""))
        reason = sanitize_terminal(str(item.get("reason") or ""))
        lines.append(f"  • skipped {name!r}: {reason}")
    return lines


def format_attachments_human(payload: dict[str, Any]) -> list[str]:
    attachments = payload.get("attachments") or []
    lines = [f"Attachments on chat message {payload.get('message_id')!r}: {len(attachments)}"]
    if not attachments:
        lines.append("  (none)")
        return lines
    for item in attachments:
        name = sanitize_terminal(str(item.get("name") or item.get("id") or ""))
        content_type = sanitize_terminal(str(item.get("content_type") or "unknown"))
        source = sanitize_terminal(str(item.get("source") or "unknown"))
        lines.append(f"  • {name!r} [{content_type}] source={source} id={item.get('id')}")
        if url := item.get("content_url"):
            lines.append(f"    url={sanitize_terminal(str(url))}")
        if reason := item.get("skip_reason"):
            lines.append(f"    not downloadable: {sanitize_terminal(str(reason))}")
    return lines


def format_delete_human(payload: dict[str, Any]) -> list[str]:
    deleted = payload.get("deleted")
    chat_id = payload.get("chat_id")
    return [f"Chat message soft-deleted: {deleted!r} (chat={chat_id!r})"]


def format_edit_human(payload: dict[str, Any]) -> list[str]:
    msg = payload.get("message") or {}
    text = sanitize_terminal(str(msg.get("body_text") or ""))
    return [
        f"Chat message updated: {msg.get('id')!r} (chat={payload.get('chat_id')!r})",
        f"  text: {text}",
    ]


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


def format_send_human(payload: dict[str, Any]) -> list[str]:
    chat = payload.get("chat") or {}
    msg = payload.get("message") or {}
    topic = sanitize_terminal(str(chat.get("topic") or "(no topic)"))
    text = sanitize_terminal(str(msg.get("body_text") or ""))
    lines = [
        f"Sent message in {topic!r} ({chat.get('id')})",
        f"  id={msg.get('id')}",
        f"  text: {text}",
    ]
    skipped = int(payload.get("skipped") or 0)
    if payload.get("partial") or skipped:
        lines.append(f"  (skipped {skipped} chat(s) while matching; results may be partial)")
    return lines


_CARD_CONTENT_TYPE_PREFIX = "application/vnd.microsoft.card."
_LATEST_SCAN_PAGE_SIZE = 50
_MEMBER_FETCH_CONCURRENCY = 8
_MESSAGE_REFERENCE_TYPES = frozenset({"forwardedmessagereference", "messagereference"})
_REFERENCE_CONTENT_TYPE = "reference"
_SKIPPED = object()
_TAG_RE = re.compile(r"<[^>]+>")


def _attachment_source(content_url: str | None) -> str | None:
    if not content_url:
        return None
    host = (urlparse(content_url).hostname or "").casefold()
    if not host:
        return None
    if host.endswith("-my.sharepoint.com"):
        return "onedrive"
    if host.endswith("sharepoint.com"):
        return "sharepoint"
    return "other"


def _attachment_to_dict(attachment: Any) -> dict[str, Any]:
    content_type = (getattr(attachment, "content_type", None) or "").strip()
    content_url = getattr(attachment, "content_url", None) or None
    reason = _skip_reason(content_type, content_url)
    return {
        "content_type": content_type or None,
        "content_url": content_url,
        "downloadable": reason is None,
        "id": getattr(attachment, "id", None),
        "name": getattr(attachment, "name", None),
        "skip_reason": reason,
        "source": _attachment_source(content_url),
    }


def _body_is_html(content_type: Any) -> bool:
    if content_type is None:
        # Graph chat reads usually return HTML when type is omitted.
        return True
    if content_type is BodyType.Html:
        return True
    if content_type is BodyType.Text:
        return False
    return "html" in str(content_type).lower()


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


async def _fetch_shared_item_bytes(client: Any, content_url: str) -> bytes:
    """Download a Teams chat file through the Graph ``/shares`` driveItem endpoint."""
    request_info = RequestInformation(
        Method.GET,
        "https://graph.microsoft.com/v1.0/shares/{shareId}/driveItem/content",
        {"shareId": _sharing_token(content_url)},
    )
    error_mapping: dict[str, ParsableFactory] = {"4XX": ODataError, "5XX": ODataError}
    result = await client.request_adapter.send_primitive_async(request_info, "bytes", error_mapping)
    if result is None:
        raise RuntimeError(f"Graph returned empty content for chat file: {content_url}")
    return bytes(result)


def _files_access_denied(content_url: str, *, detail: str) -> ChatAttachmentScopeError:
    return ChatAttachmentScopeError(
        "Graph denied access to this Teams chat file (403). It may need "
        "Files.ReadWrite or a share grant. Open it in a browser instead: "
        f"{sanitize_terminal(content_url)} ({sanitize_terminal(detail)})"
    )


def _html_to_text(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _is_ordinary_message(msg: Any) -> bool:
    """True for user messages, excluding system events.

    ``messageType`` deserializes to ``ChatMessageType``, whose ``str()`` is
    ``"ChatMessageType.Message"`` — compare the enum value, not its repr.
    """
    msg_type = getattr(msg, "message_type", None)
    if msg_type is None:
        return True
    return str(getattr(msg_type, "value", msg_type)) == "message"


def _is_reauth_error(exc: BaseException) -> bool:
    """True for HTTP 401 (expired token); re-raise so CLI maps to EXIT_AUTH."""
    return getattr(exc, "response_status_code", None) == 401


async def _latest_message_with_attachments(client: Any, chat_id: str) -> Any:
    """Newest ordinary message in the chat that carries at least one attachment."""
    query = MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters(
        expand=["attachments"],
        orderby=["createdDateTime desc"],
        top=_LATEST_SCAN_PAGE_SIZE,
    )
    page = await client.me.chats.by_chat_id(chat_id).messages.get(request_config(query))
    while page is not None:
        for msg in page.value or []:
            if not _is_ordinary_message(msg):
                continue
            if getattr(msg, "attachments", None):
                return msg
        link = getattr(page, "odata_next_link", None)
        if not link:
            break
        page = await client.me.chats.by_chat_id(chat_id).messages.with_url(link).get()
    raise ChatMessageNotFoundError(f"no message with attachments found in chat: {chat_id}")


def _message_to_dict(msg: Any) -> dict[str, Any]:
    body = msg.body
    raw = body.content if body is not None else None
    content_type = getattr(body, "content_type", None) if body is not None else None
    is_html = _body_is_html(content_type)
    if raw is None:
        body_html = None
        body_text = ""
    elif is_html:
        body_html = raw
        body_text = _html_to_text(raw)
    else:
        body_html = None
        body_text = raw
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


async def _require_chat_message(client: Any, chat_id: str, message_id: str) -> Any:
    query = ChatMessageItemRequestBuilder.ChatMessageItemRequestBuilderGetQueryParameters(
        expand=["attachments"],
    )
    message = (
        await client.me.chats.by_chat_id(chat_id)
        .messages.by_chat_message_id(message_id)
        .get(request_config(query))
    )
    if message is None or not message.id:
        raise ChatMessageNotFoundError(f"chat message not found: {message_id}")
    return message


def _require_files_scope(config: BlumkinConfig, content_url: str) -> None:
    if any(scope.startswith(FILES_SCOPE_PREFIX) for scope in effective_scopes(config)):
        return
    raise ChatAttachmentScopeError(
        "downloading Teams chat files needs a delegated Files.Read scope, which this "
        "sign-in does not hold. Set files_scopes = true in config.toml (or "
        "BLUMKIN_FILES_SCOPES=1) once the tenant grants it, then wipe the token cache "
        "and re-login. Until then, open the file in a browser: "
        f"{sanitize_terminal(content_url)}"
    )


async def _resolve_chat_target(
    *,
    chat_id: str | None,
    with_name: str | None,
    config: BlumkinConfig,
) -> tuple[dict[str, Any], str, bool, int]:
    """Resolve ``--chat-id`` / ``--with`` to one chat; refuse ambiguous or partial matches."""
    name = (with_name or "").strip() or None
    explicit_id = (chat_id or "").strip() or None
    if bool(name) == bool(explicit_id):
        raise ValueError("exactly one of --with or --chat-id is required")
    if explicit_id is not None:
        chat = {"chat_type": None, "id": explicit_id, "members": [], "topic": None}
        return chat, explicit_id, False, 0
    assert name is not None
    found = await chat_find(with_name=name, config=config)
    items = found["items"]
    if found["partial"]:
        raise ValueError(
            f"chat match for {name!r} is partial "
            f"(skipped {int(found['skipped'])} chat(s)); "
            "retry later or pass --chat-id from `chat find`"
        )
    if not items:
        raise LookupError(f"no chat matched {name!r}")
    if len(items) > 1:
        ids = ", ".join(str(item.get("id")) for item in items)
        raise ValueError(
            f"ambiguous chat match for {name!r} ({len(items)} chats); "
            f"pass --chat-id with one of: {ids}"
        )
    chat = items[0]
    return chat, str(chat["id"]), bool(found["partial"]), int(found["skipped"])


def _sharing_token(content_url: str) -> str:
    """Encode a sharing URL as a Graph ``shares`` id (``u!`` + unpadded base64url)."""
    encoded = base64.urlsafe_b64encode(content_url.encode()).decode().rstrip("=")
    return f"u!{encoded}"


def _skip_reason(content_type: str, content_url: str | None) -> str | None:
    """Why an attachment cannot be downloaded, or ``None`` when it is a fetchable file.

    ``content_type`` is sender-controlled and reaches stderr through
    ``ChatAttachmentSkippedError``, so it is sanitized before being embedded.
    """
    folded = content_type.casefold()
    if folded.startswith(_CARD_CONTENT_TYPE_PREFIX):
        return "adaptive card attachment carries no file content"
    if folded in _MESSAGE_REFERENCE_TYPES:
        return "message reference attachment carries no file content"
    safe_type = sanitize_terminal(content_type)
    if not content_url:
        return f"{safe_type or 'attachment'} has no content URL to download from"
    if folded and folded != _REFERENCE_CONTENT_TYPE:
        return f"{safe_type} is not a downloadable chat file in v1"
    return None
