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

from blumkin.config import BlumkinConfig, load_config
from blumkin.providers.google_auth import get_credentials
from blumkin.providers.google_http import build_api_service, execute
from blumkin.skills.chat import _MAX_SCANNED, _chat_last_filters, _name_matches

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
    service = _chat_service(cfg)
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
    service = _chat_service(cfg)
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


def _chat_service(cfg: BlumkinConfig) -> Any:
    creds = get_credentials(cfg, allow_interactive=False)
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
