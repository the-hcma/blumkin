"""The `lesson` loop task: queue a page photo for the operator's agent, then reply (issue #506).

blumkin calls no model and the loop starts no agent. A mail with a photo of a lesson page
and a keyword in the subject (a name, say) makes this handler record a *pending lesson*:
the template the keyword selects, the saved page image(s) and the message to answer. The
run ends `needs_user` with a short "lesson waiting" notice. The operator then runs an agent
session and tells it to run the loop: `blumkin loop pending` lists what is waiting, the
agent follows the named `tasks/<template>.md` template for each item (the saved photo
replaces the "find it in mail" steps), and `blumkin loop finish ITEM --doc URL` replies to
the original message with the document link(s) and closes the item.

Rules this module keeps:

- The subject is only matched against configured keywords (case and accents folded). It is
  never executed and never copied into a reason, a journal row or a notice.
- Exactly one distinct template must match, otherwise the run is `needs_user` and nothing is
  queued.
- A link is accepted only when it is an https Google Docs or Drive URL; nothing else is ever
  sent in a reply. The reply goes to the sender of the message only, as plain text.
- Journals and notices carry ids and outcomes only. The agent's own words (a hand-back
  note) live in the private item file.
- Finishing is safe to repeat: a closed item is never answered twice, and an item whose reply
  failed keeps its documents so `finish` can be run again.

Items are JSON files under `<config dir>/loop/lessons/` (mode `0600`, directory `0700`),
written atomically under a lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from blumkin.browser.profiles import BrowserProfileError, ensure_private_dir, write_private
from blumkin.loop.handlers import HandlerResult
from blumkin.loop.journal import append_run
from blumkin.loop.mail_intake import LoopIntakeError, intake_dir
from blumkin.loop.types import (
    LESSON_TASK,
    LESSON_WAITING,
    RunContext,
    RunOutcome,
    TriggerKind,
    TriggerSpec,
)

REPLY_TIMEOUT_SECONDS = 30.0

_LOG = logging.getLogger(__name__)

ReplyFn = Callable[[str | None, str, str], None]


class LessonError(ValueError):
    """A lesson item, link or id is not acceptable."""


@dataclass(frozen=True, slots=True)
class LessonItem:
    """One pending (or closed) lesson as stored on disk."""

    created_at: str
    docs: tuple[str, ...]
    files: tuple[str, ...]
    id: str
    instance: str
    message_id: str
    note: str
    profile: str | None
    state: str
    template: str
    trigger: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "docs": list(self.docs),
            "files": list(self.files),
            "id": self.id,
            "instance": self.instance,
            "message_id": self.message_id,
            "note": self.note,
            "profile": self.profile,
            "state": self.state,
            "template": self.template,
            "trigger": self.trigger,
        }


class LessonNotFoundError(LookupError):
    """No lesson item has that id."""


class LessonReplyError(LessonError):
    """The reply could not be sent; the item is kept so `finish` can be repeated."""


@dataclass(frozen=True, slots=True)
class TemplatePick:
    """Which template a subject selects, or why none."""

    reason: str
    template: str | None


def finish_lesson(
    config_dir: Path,
    item_id: str,
    docs: Sequence[str],
    *,
    reply: ReplyFn | None = None,
) -> LessonItem:
    """Reply to the original message with `docs` and close the item (repeat-safe)."""
    _check_id(item_id)
    links = _check_docs(docs)
    store = _store(config_dir)
    with _locked(store):
        item = _load(store, item_id)
        if item.state == "done":
            return item
        item = _save(store, _with(item, docs=links))
    body = _reply_body(links)
    try:
        (reply or send_reply)(item.profile, item.message_id, body)
    except Exception as exc:  # noqa: BLE001 - keep the item, report the type only
        raise LessonReplyError(
            f"Expected the reply to be sent, got {type(exc).__name__}; the documents are "
            f"saved, so run `blumkin loop finish {item.id} --doc ...` again"
        ) from exc
    with _locked(store):
        current = _load(store, item_id)
        if current.state == "done":
            return current
        closed = _save(store, _with(current, state="done"))
        append_run(
            config_dir,
            instance=closed.instance,
            outcome=RunOutcome.DONE,
            reason=f"lesson done: {len(links)} document(s)",
            task=LESSON_TASK,
            trigger=closed.trigger,
            trigger_kind=TriggerKind.LOOP,
        )
        return closed


def fold_keyword(text: str) -> str | None:
    """`text` as the single lowercase accent-free word a subject keyword must be, or None."""
    tokens = subject_tokens(text)
    return tokens[0] if len(tokens) == 1 else None


def hand_back_lesson(config_dir: Path, item_id: str, note: str) -> LessonItem:
    """Give an item back to the operator with a short reason (nothing is sent)."""
    _check_id(item_id)
    store = _store(config_dir)
    with _locked(store):
        item = _load(store, item_id)
        if item.state == "done":
            raise LessonError(f"Expected an open lesson item, got {item.id} which is done")
        handed = _save(store, _with(item, note=_clean_note(note), state="needs_user"))
        append_run(
            config_dir,
            instance=handed.instance,
            outcome=RunOutcome.NEEDS_USER,
            reason="lesson handed back by the agent",
            task=LESSON_TASK,
            trigger=handed.trigger,
            trigger_kind=TriggerKind.LOOP,
        )
        return handed


def lesson(trigger: TriggerSpec, *, context: RunContext | None = None) -> HandlerResult:
    """The registered handler: queue the mailed page photo for the operator's agent."""
    if context is None or context.config_dir is None or not context.message_id:
        return _stop("lesson not queued: it needs a mail message")
    pick = pick_template(trigger, context.subject)
    if pick.template is None:
        return _stop(f"lesson not queued: {pick.reason}")
    if not context.intake_ok:
        return _stop("lesson not queued: the page image was not saved")
    try:
        files = _page_images(
            intake_dir(context.config_dir, trigger=trigger.name, message_id=context.message_id)
        )
    except LoopIntakeError:
        return _stop("lesson not queued: the intake folder is not usable")
    if not files:
        return _stop("lesson not queued: no page image was saved")
    _queue(
        context.config_dir,
        trigger=trigger,
        template=pick.template,
        message_id=context.message_id,
        profile=context.profile,
        instance=context.instance or trigger.instance,
        files=files,
    )
    return _stop(f"{LESSON_WAITING}: {pick.template} ({len(files)} page image(s))")


def pending_lessons(config_dir: Path) -> list[LessonItem]:
    """The items waiting for an agent, oldest first."""
    store = _existing_store(config_dir)
    if store is None:
        return []
    with _locked(store):
        items = [item for item in _load_all(store) if item.state == "pending"]
    return sorted(items, key=lambda item: (item.created_at, item.id))


def pick_template(trigger: TriggerSpec, subject: str | None) -> TemplatePick:
    """The template the subject's keyword selects; exactly one distinct template must match."""
    tokens = set(subject_tokens(subject or ""))
    matched = sorted({template for keyword, template in trigger.templates if keyword in tokens})
    if len(matched) == 1:
        return TemplatePick(reason="", template=matched[0])
    if matched:
        return TemplatePick(reason="the subject names more than one lesson", template=None)
    keywords = ", ".join(sorted(keyword for keyword, _ in trigger.templates))
    return TemplatePick(
        reason=f"no lesson keyword in the subject (expected one of: {keywords})",
        template=None,
    )


def send_reply(profile: str | None, message_id: str, body: str) -> None:
    """Reply to `message_id` (sender only, plain text) and send it."""
    asyncio.run(_send_reply_async(profile, message_id, body))


def subject_tokens(text: str) -> tuple[str, ...]:
    """Lowercase, accent-free alphanumeric words of `text`."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return tuple(re.findall(r"[a-z0-9]+", stripped))


def _check_docs(docs: Sequence[str]) -> tuple[str, ...]:
    if not 1 <= len(docs) <= _MAX_DOCS:
        raise LessonError(f"Expected 1-{_MAX_DOCS} document links, got {len(docs)}")
    for url in docs:
        _check_url(url)
    return tuple(dict.fromkeys(docs))


def _check_id(item_id: str) -> None:
    if not isinstance(item_id, str) or not _ITEM_ID.fullmatch(item_id):
        raise LessonError(f"Expected a lesson item id of 16 hex digits, got {item_id!r:.40}")


def _check_url(url: str) -> None:
    shown = f"{url!r:.80}" if isinstance(url, str) else repr(type(url).__name__)
    problem = (
        "Expected an https Google Docs or Drive link (docs.google.com or drive.google.com), "
        f"got {shown}"
    )
    if not isinstance(url, str) or not url or len(url) > _MAX_URL:
        raise LessonError(problem)
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise LessonError(problem)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise LessonError(problem) from exc
    ok = (
        parts.scheme == "https"
        and parts.hostname in _DOC_HOSTS
        and parts.username is None
        and parts.password is None
        and port is None
        and "@" not in parts.netloc
        and len(parts.path) > 1
    )
    if not ok:
        raise LessonError(problem)


def _clean_note(note: str) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in note)
    return " ".join(text.split())[:_MAX_NOTE]


def _existing_store(config_dir: Path) -> Path | None:
    root = config_dir / _LOOP_DIRNAME / _STORE_DIRNAME
    return _store(config_dir) if root.is_dir() else None


def _item_id(trigger: str, profile: str | None, message_id: str) -> str:
    key = f"{trigger}\0{profile or ''}\0{message_id}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _load(store: Path, item_id: str) -> LessonItem:
    path = store / f"{item_id}.json"
    if not path.is_file() or path.is_symlink():
        raise LessonNotFoundError(f"Expected a lesson item {item_id}, got none")
    return _parse(path)


def _load_all(store: Path) -> list[LessonItem]:
    items: list[LessonItem] = []
    for path in sorted(store.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            items.append(_parse(path))
        except LessonError:
            _LOG.warning("skipping an unreadable lesson item file")
    return items


@contextlib.contextmanager
def _locked(store: Path) -> Iterator[None]:
    fd = os.open(store / _LOCK_FILENAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _page_images(directory: Path) -> tuple[str, ...]:
    found: list[str] = []
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() in _IMAGE_SUFFIXES:
            found.append(str(path))
    return tuple(found[:_MAX_IMAGES])


def _parse(path: Path) -> LessonItem:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        profile = data["profile"]
        item = LessonItem(
            created_at=str(data["created_at"]),
            docs=tuple(str(d) for d in data["docs"]),
            files=tuple(str(f) for f in data["files"]),
            id=str(data["id"]),
            instance=str(data["instance"]),
            message_id=str(data["message_id"]),
            note=str(data["note"]),
            profile=None if profile is None else str(profile),
            state=str(data["state"]),
            template=str(data["template"]),
            trigger=str(data["trigger"]),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LessonError(f"Expected a valid lesson item file, got {type(exc).__name__}") from exc
    if item.state not in _STATES or not _ITEM_ID.fullmatch(item.id) or path.stem != item.id:
        raise LessonError("Expected a lesson item with a known state and its own id, got neither")
    return item


def _queue(
    config_dir: Path,
    *,
    trigger: TriggerSpec,
    template: str,
    message_id: str,
    profile: str | None,
    instance: str,
    files: tuple[str, ...],
) -> LessonItem:
    store = _store(config_dir)
    item_id = _item_id(trigger.name, profile, message_id)
    with _locked(store):
        if (store / f"{item_id}.json").is_file():
            return _load(store, item_id)
        return _save(
            store,
            LessonItem(
                created_at=_now(),
                docs=(),
                files=files,
                id=item_id,
                instance=instance,
                message_id=message_id,
                note="",
                profile=profile,
                state="pending",
                template=template,
                trigger=trigger.name,
            ),
        )


def _reply_body(docs: Sequence[str]) -> str:
    lines = ["Your lesson is ready:", ""]
    lines.extend(docs)
    lines.extend(["", "Sent by: blumkin loop (lesson task)"])
    return "\n".join(lines) + "\n"


def _save(store: Path, item: LessonItem) -> LessonItem:
    write_private(store / f"{item.id}.json", json.dumps(item.as_dict(), sort_keys=True))
    return item


async def _send_reply_async(profile: str | None, message_id: str, body: str) -> None:
    # Imported here: the provider layer imports a lot and is only needed to send.
    from blumkin.config import load_config
    from blumkin.providers import get_provider

    config = load_config(profile=profile) if profile else load_config()
    provider = get_provider(config)

    async def _reply_and_send() -> None:
        created = await provider.mail_reply(
            message_id=message_id, body=body, body_type="text", no_signature=True
        )
        draft_id = str(created.get("id") or created.get("draft_id") or "").strip()
        if not draft_id:
            raise ValueError("reply response missing id")
        await provider.mail_send_draft(draft_id=draft_id)

    await asyncio.wait_for(_reply_and_send(), timeout=REPLY_TIMEOUT_SECONDS)


def _stop(reason: str) -> HandlerResult:
    return HandlerResult(outcome=RunOutcome.NEEDS_USER, reason=reason)


def _store(config_dir: Path) -> Path:
    try:
        return ensure_private_dir(ensure_private_dir(config_dir / _LOOP_DIRNAME) / _STORE_DIRNAME)
    except BrowserProfileError as exc:
        raise LessonError(str(exc)) from exc


def _with(item: LessonItem, **changes: Any) -> LessonItem:
    data = item.as_dict()
    data.update(changes)
    if isinstance(data["docs"], tuple):
        data["docs"] = list(data["docs"])
    return LessonItem(
        created_at=data["created_at"],
        docs=tuple(data["docs"]),
        files=tuple(data["files"]),
        id=data["id"],
        instance=data["instance"],
        message_id=data["message_id"],
        note=data["note"],
        profile=data["profile"],
        state=data["state"],
        template=data["template"],
        trigger=data["trigger"],
    )


_DOC_HOSTS = frozenset({"docs.google.com", "drive.google.com"})
_IMAGE_SUFFIXES = frozenset(
    {".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".pdf", ".png", ".tif", ".tiff", ".webp"}
)
_ITEM_ID = re.compile(r"[0-9a-f]{16}")
_LOCK_FILENAME = ".lock"
_LOOP_DIRNAME = "loop"
_MAX_DOCS = 10
_MAX_IMAGES = 10
_MAX_NOTE = 200
_MAX_URL = 512
_STATES = frozenset({"done", "needs_user", "pending"})
_STORE_DIRNAME = "lessons"
