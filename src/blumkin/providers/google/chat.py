"""Google Chat reads, shaped like ``blumkin.skills.chat``.

Google models a 1:1 as a *space* whose members are the two people, so matching a
display name means listing spaces and then their memberships - the same
member-name scan the Graph path does, with the same fail-closed rules: a space
whose membership listing is refused is counted as skipped and makes the result
``partial``, and callers refuse to act on a partial or multi-match rather than
guessing which conversation you meant.

Ids in the skill payload are Google resource names (``spaces/AAA``,
``spaces/AAA/messages/BBB``), which is what every other Chat call wants back.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
from blumkin.providers.google_auth import CHAT_READ_SCOPES, CHAT_SCOPES, get_credentials
from blumkin.providers.google_http import build_api_service, execute
from blumkin.skills.chat import (
    _MAX_SCANNED,
    ChatAttachmentNotFoundError,
    ChatAttachmentSkippedError,
    ChatMessageNotFoundError,
    _chat_last_filters,
    _name_matches,
)

_MESSAGE_PAGE_SIZE = 50
_SPACE_PAGE_SIZE = 100


async def chat_find(
    *,
    with_name: str,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    needle = with_name.strip().lower()
    if not needle:
        raise ValueError("--with requires a non-empty display name")
    cfg = config or load_config()
    service = _chat_service(cfg, required_scopes=CHAT_READ_SCOPES)
    matches: list[dict[str, Any]] = []
    skipped = 0
    attempted = 0
    skip_errors: list[HttpError] = []
    for space in _list_spaces(service):
        name = space.get("name")
        if not name:
            continue
        attempted += 1
        try:
            member_names = _member_names(service, str(name))
        except HttpError as exc:
            # Membership refused (ACL, throttle, a space we lost access to). Count it
            # so the caller can tell "no match" from "did not finish looking".
            skipped += 1
            skip_errors.append(exc)
            continue
        if not any(_name_matches(needle, member) for member in member_names):
            continue
        matches.append(
            {
                "chat_type": _chat_type(space),
                "id": str(name),
                "members": sorted(member_names),
                "topic": space.get("displayName") or None,
            }
        )
    if attempted and skipped == attempted and not matches:
        # Every space was refused: "no chat found" would be a lie, and an expired or
        # under-scoped token looks exactly like this. Raise so the CLI reports
        # missing_scope / auth_required instead of exiting 0 with an empty list.
        raise skip_errors[-1]
    matches.sort(key=lambda item: (str(item.get("topic") or ""), str(item["id"])))
    return {
        "items": matches,
        "partial": skipped > 0,
        "query": with_name,
        "skipped": skipped,
    }


async def chat_last(
    *,
    with_name: str | None = None,
    chat_id: str | None = None,
    contains: str | None = None,
    n: int = 3,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if n < 1:
        raise ValueError("--n must be >= 1")
    name = (with_name or "").strip() or None
    explicit_id = (chat_id or "").strip() or None
    term = (contains or "").strip() or None
    if bool(name) == bool(explicit_id):
        raise ValueError("exactly one of --with or --chat-id is required")
    cfg = config or load_config()
    if explicit_id is not None:
        chat = {"chat_type": None, "id": explicit_id, "members": [], "topic": None}
        found: dict[str, Any] = {"partial": False, "skipped": 0}
    else:
        assert name is not None
        found = await chat_find(with_name=name, config=cfg)
        items = found["items"]
        if found["partial"]:
            raise ValueError(
                f"chat match for {name!r} is partial "
                f"(skipped {int(found['skipped'])} chat(s)); "
                "retry later or pass --chat-id from `chat find`"
            )
        if not items:
            return {
                "chat": None,
                "filters": _chat_last_filters(term, scanned=None, complete=None),
                "items": [],
                "partial": found["partial"],
                "query": name,
                "skipped": found["skipped"],
            }
        if len(items) > 1:
            ids = ", ".join(str(item.get("id")) for item in items)
            raise ValueError(
                f"ambiguous chat match for {name!r} ({len(items)} chats); "
                f"pass --chat-id with one of: {ids}"
            )
        chat = items[0]
    service = _chat_service(cfg, required_scopes=CHAT_READ_SCOPES)
    selected: list[dict[str, Any]] = []
    scanned = 0
    exhausted = False
    hit_cap = False
    page_token: str | None = None
    while True:
        response = execute(
            service.spaces()
            .messages()
            .list(
                parent=str(chat["id"]),
                pageSize=_MESSAGE_PAGE_SIZE,
                orderBy="createTime DESC",
                pageToken=page_token,
            )
        )
        for raw in response.get("messages") or []:
            if not isinstance(raw, Mapping):
                continue
            if not _is_ordinary_message(raw):
                continue
            if term is not None and scanned >= _MAX_SCANNED:
                hit_cap = True
                break
            scanned += 1
            item = _message_to_dict(raw)
            if term is not None and term.casefold() not in str(item["body_text"] or "").casefold():
                continue
            selected.append(item)
            if len(selected) >= n:
                break
        if len(selected) >= n or hit_cap:
            break
        page_token = response.get("nextPageToken")
        if not page_token:
            exhausted = True
            break
    return {
        "chat": chat,
        "filters": _chat_last_filters(
            term,
            scanned=scanned if term is not None else None,
            complete=exhausted if term is not None else None,
        ),
        "items": selected,
        "partial": found["partial"],
        "query": name,
        "skipped": found["skipped"],
    }


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
    service = _chat_service(cfg, required_scopes=CHAT_READ_SCOPES)
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
        content = _download_attachment(service, str(meta["id"]))
        if download_all:
            filename = unique_filename(
                sanitize_attachment_filename(meta["name"] or str(meta["id"])), used_names
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
                "share_url": meta["content_url"],
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
    service = _chat_service(cfg, required_scopes=CHAT_READ_SCOPES)
    message = (
        _latest_message_with_attachments(service, target_id)
        if latest
        else _require_message(service, str(mid))
    )
    return {
        "attachments": [_attachment_to_dict(att) for att in (message.get("attachments") or [])],
        "chat": chat,
        "chat_id": target_id,
        "message": _message_to_dict(message),
        "message_id": message.get("name"),
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
    service = _chat_service(cfg)
    execute(service.spaces().messages().delete(name=mid), num_retries=0)
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
    service = _chat_service(cfg)
    updated = execute(
        service.spaces().messages().patch(name=mid, updateMask="text", body={"text": body_text}),
        num_retries=0,
    )
    return {"chat_id": cid, "message": _message_to_dict(updated)}


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
    service = _chat_service(cfg)
    created = execute(
        service.spaces().messages().create(parent=target_id, body={"text": body_text}),
        # Sending messages a person: a blind retry past an ambiguous failure could
        # post twice, and Chat has no idempotency key on this call.
        num_retries=0,
    )
    return {
        "chat": chat,
        "message": _message_to_dict(created),
        "partial": partial,
        "query": query,
        "skipped": skipped,
    }


def _chat_service(cfg: BlumkinConfig, *, required_scopes: frozenset[str] = CHAT_SCOPES) -> Any:
    creds = get_credentials(cfg, allow_interactive=False, required_scopes=required_scopes)
    return build_api_service("chat", "v1", creds=creds, config=cfg)


def _chat_type(space: Mapping[str, Any]) -> str | None:
    """Google spaceType in the Graph vocabulary the --json contract already uses."""
    raw = str(space.get("spaceType") or "")
    if raw == "DIRECT_MESSAGE":
        return "oneOnOne"
    # GROUP_CHAT is an unnamed multi-person DM; SPACE is a named room. Graph's
    # vocabulary has one bucket for both.
    if raw in {"GROUP_CHAT", "SPACE"}:
        return "group"
    return None


def _is_ordinary_message(msg: Mapping[str, Any]) -> bool:
    """True for a message a person actually wrote.

    Chat history carries membership and space events alongside real messages, and
    a deleted message survives as a tombstone. Counting either toward the
    --contains scan, or returning one as an item, would diverge from the Graph
    path, which filters system events before it counts anything.
    """
    if msg.get("deletionMetadata"):
        return False
    return bool(msg.get("text") or msg.get("attachments") or msg.get("formattedText"))


def _list_spaces(service: Any) -> list[Mapping[str, Any]]:
    spaces: list[Mapping[str, Any]] = []
    page_token: str | None = None
    while True:
        response = execute(service.spaces().list(pageSize=_SPACE_PAGE_SIZE, pageToken=page_token))
        spaces.extend(item for item in (response.get("spaces") or []) if isinstance(item, Mapping))
        page_token = response.get("nextPageToken")
        if not page_token:
            return spaces


def _member_names(service: Any, space: str) -> list[str]:
    names: list[str] = []
    page_token: str | None = None
    while True:
        response = execute(
            service.spaces()
            .members()
            .list(parent=space, pageSize=_SPACE_PAGE_SIZE, pageToken=page_token)
        )
        for membership in response.get("memberships") or []:
            if not isinstance(membership, Mapping):
                continue
            member = membership.get("member") or {}
            display = member.get("displayName") if isinstance(member, Mapping) else None
            if display:
                names.append(str(display))
        page_token = response.get("nextPageToken")
        if not page_token:
            return names


def _message_to_dict(msg: Mapping[str, Any]) -> dict[str, Any]:
    sender = msg.get("sender") or {}
    sender = sender if isinstance(sender, Mapping) else {}
    # A card message documents text as empty and carries its content in
    # formattedText, so reading only text would count it as scanned while leaving
    # --contains nothing to match and surfacing it with an empty body.
    text = msg.get("text")
    if text is None or str(text) == "":
        text = msg.get("formattedText")
    return {
        # Chat bodies are text (formattedText is the same content with markup);
        # there is no HTML body to mirror Graph's, so it stays null.
        "body_html": None,
        "body_text": str(text) if text is not None else "",
        "created": str(msg.get("createTime")) if msg.get("createTime") else None,
        "from_name": sender.get("displayName"),
        "from_user": sender.get("name"),
        "id": msg.get("name"),
    }


def _attachment_to_dict(attachment: Mapping[str, Any]) -> dict[str, Any]:
    """One Chat attachment in the shared shape.

    A Drive-backed attachment has no bytes on the Chat media endpoint - it is a
    Drive file with its own ACL - so it is reported as present but not
    downloadable, the same way the Graph path reports a non-file attachment.
    """
    data_ref = attachment.get("attachmentDataRef") or {}
    resource = data_ref.get("resourceName") if isinstance(data_ref, Mapping) else None
    drive_ref = attachment.get("driveDataRef") or {}
    drive_id = drive_ref.get("driveFileId") if isinstance(drive_ref, Mapping) else None
    reason = None
    if not resource:
        reason = (
            "attachment is a Drive file; open it in Drive"
            if drive_id
            else "attachment has no downloadable content"
        )
    return {
        "content_type": attachment.get("contentType") or None,
        "content_url": attachment.get("downloadUri") or attachment.get("thumbnailUri") or None,
        "downloadable": reason is None,
        "id": resource or attachment.get("name"),
        "name": attachment.get("contentName") or None,
        "skip_reason": reason,
        "source": "drive" if drive_id else "chat",
    }


def _download_attachment(service: Any, resource_name: str) -> bytes:
    """Fetch attachment bytes from the Chat media endpoint.

    The discovery method is ``chat.media.download``; google-api-python-client also
    generates a ``_media`` variant for methods that support media download, and
    which of the two exists is a property of the served discovery document rather
    than something worth guessing at import time. Prefer the media variant, fall
    back to the plain one, and say so plainly if neither is there.
    """
    media = service.media()
    request_factory = getattr(media, "download_media", None) or getattr(media, "download", None)
    if request_factory is None:  # pragma: no cover - depends on the served discovery doc
        raise ChatAttachmentNotFoundError(
            "the Chat discovery document exposes no media download method; "
            "open the attachment in Chat instead"
        )
    data = execute(request_factory(resourceName=resource_name))
    if isinstance(data, bytes | bytearray):
        return bytes(data)
    raise ChatAttachmentNotFoundError(f"attachment returned no bytes: {resource_name}")


def _latest_message_with_attachments(service: Any, space: str) -> Mapping[str, Any]:
    page_token: str | None = None
    scanned = 0
    while scanned < _MAX_SCANNED:
        response = execute(
            service.spaces()
            .messages()
            .list(
                parent=space,
                pageSize=_MESSAGE_PAGE_SIZE,
                orderBy="createTime DESC",
                pageToken=page_token,
            )
        )
        for raw in response.get("messages") or []:
            if not isinstance(raw, Mapping):
                continue
            scanned += 1
            if raw.get("attachments"):
                return raw
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    raise ChatMessageNotFoundError(
        f"no message with attachments found in {space!r} "
        f"(scanned {scanned} message(s)); pass --message-id"
    )


def _require_message(service: Any, message_id: str) -> Mapping[str, Any]:
    try:
        return execute(service.spaces().messages().get(name=message_id))
    except HttpError as exc:
        status = getattr(exc, "status_code", None)
        if status == 404:
            raise ChatMessageNotFoundError(f"chat message not found: {message_id}") from exc
        raise


async def _resolve_chat_target(
    *,
    chat_id: str | None,
    with_name: str | None,
    config: BlumkinConfig,
) -> tuple[dict[str, Any], str, bool, int]:
    """Resolve --chat-id / --with to one space; refuse ambiguous or partial matches."""
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
