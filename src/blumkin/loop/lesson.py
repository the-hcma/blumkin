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
  never executed and never copied into a reason, a journal row or a notice. Attachment
  names from the mail are replaced by `page-N.ext` so they never reach the agent.
- Exactly one distinct template must match, otherwise the run is `needs_user` and nothing is
  queued.
- A link is accepted only when it is an https Google Docs or Drive document, file or folder
  URL; nothing else is ever sent in a reply.
- The reply is a threaded plain-text reply whose draft is checked before it is sent: every
  recipient must be an address on the trigger's allowlist and there must be no Cc or Bcc, so
  a `Reply-To` header in the mail cannot redirect it. A draft that fails the check is deleted.
- Journals and notices carry ids and outcomes only. The agent's own words (a hand-back
  note) live in the private item file.
- Finishing is safe to repeat and to race. An item is claimed for the duration of a finish
  (a second finish or a hand-back is refused as "in progress"), the draft id is saved as
  soon as the draft exists so a retry sends that draft instead of making another, and a
  closed item is never answered twice.

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
from collections.abc import Callable, Iterator, Mapping, Sequence
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

CLAIM_SECONDS = 120.0
REPLY_TIMEOUT_SECONDS = 30.0

_LOG = logging.getLogger(__name__)

# (profile, message id, body, allowed recipients) -> id of a checked reply draft.
PrepareFn = Callable[[str | None, str, str, Sequence[str]], str]
# (profile, draft id) -> None
SendFn = Callable[[str | None, str], None]


class LessonError(ValueError):
    """A lesson item, link, id or reply is not acceptable."""


@dataclass(frozen=True, slots=True)
class LessonItem:
    """One lesson as stored on disk."""

    claimed_at: str
    created_at: str
    docs: tuple[str, ...]
    draft_id: str
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
            "claimed_at": self.claimed_at,
            "created_at": self.created_at,
            "docs": list(self.docs),
            "draft_id": self.draft_id,
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
    allowed_recipients: Sequence[str] = (),
    prepare: PrepareFn | None = None,
    send: SendFn | None = None,
) -> LessonItem:
    """Reply to the original message with `docs` and close the item (repeat-safe)."""
    _check_id(item_id)
    links = _check_docs(docs)
    allowed = tuple(allowed_recipients)
    if not allowed:
        raise LessonError(
            "Expected the trigger to have an allowlist to check the reply against, got none"
        )
    store = _store(config_dir)
    with _locked(store):
        item = _load(store, item_id)
        if item.state == "done":
            return item
        if _claimed(item):
            raise LessonError(
                f"Expected no other finish in progress for {item.id}, "
                f"got one started at {item.claimed_at}"
            )
        if item.draft_id and item.docs and item.docs != links:
            raise LessonError(
                f"Expected the same --doc links as the earlier attempt for {item.id}, got "
                "different ones; the saved reply draft already carries the earlier links"
            )
        item = _save(store, _with(item, claimed_at=_now(), docs=links))
    try:
        draft_id = item.draft_id
        if not draft_id:
            draft_id = (prepare or prepare_reply)(
                item.profile, item.message_id, _reply_body(links), allowed
            )
            with _locked(store):
                item = _save(store, _with(_load(store, item_id), draft_id=draft_id))
        (send or send_draft)(item.profile, draft_id)
    except LessonError:
        _release(store, item_id)
        raise
    except Exception as exc:  # noqa: BLE001 - keep the item, report the type only
        _release(store, item_id)
        raise LessonReplyError(
            f"Expected the reply to be sent, got {type(exc).__name__}; the documents are "
            f"saved, so run `blumkin loop finish {item.id} --doc ...` again"
        ) from exc
    with _locked(store):
        closed = _save(store, _with(_load(store, item_id), claimed_at="", state="done"))
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


def get_lesson(config_dir: Path, item_id: str) -> LessonItem:
    """One item by id, in any state."""
    _check_id(item_id)
    store = _store(config_dir)
    with _locked(store):
        return _load(store, item_id)


def hand_back_lesson(config_dir: Path, item_id: str, note: str) -> LessonItem:
    """Give an item back to the operator with a short reason (nothing is sent)."""
    _check_id(item_id)
    store = _store(config_dir)
    with _locked(store):
        item = _load(store, item_id)
        if item.state == "done":
            raise LessonError(f"Expected an open lesson item, got {item.id} which is done")
        if _claimed(item):
            raise LessonError(
                f"Expected no finish in progress for {item.id}, got one started at "
                f"{item.claimed_at}"
            )
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
    item = _queue(
        context.config_dir,
        trigger=trigger,
        template=pick.template,
        message_id=context.message_id,
        profile=context.profile,
        instance=context.instance or trigger.instance,
        files=files,
    )
    if item.state == "done":
        return HandlerResult(outcome=RunOutcome.DONE, reason="lesson already answered")
    if item.state == "needs_user":
        return _stop("lesson handed back earlier; see blumkin loop pending --handed-back")
    return _stop(f"{LESSON_WAITING}: {item.template} ({len(item.files)} page image(s))")


def pending_lessons(config_dir: Path, *, handed_back: bool = False) -> list[LessonItem]:
    """The items waiting for an agent, oldest first (and handed-back ones when asked)."""
    store = _existing_store(config_dir)
    if store is None:
        return []
    wanted = {"pending", "needs_user"} if handed_back else {"pending"}
    with _locked(store):
        items = [item for item in _load_all(store) if item.state in wanted]
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


def prepare_reply(
    profile: str | None,
    message_id: str,
    body: str,
    allowed: Sequence[str],
    *,
    provider: Any | None = None,
) -> str:
    """Create a threaded plain-text reply draft and check who it is addressed to.

    Returns the draft id. A draft with a recipient outside `allowed` (a `Reply-To` header
    can add one), or with any Cc or Bcc, is deleted and refused.
    """
    return asyncio.run(_prepare_async(profile, message_id, body, tuple(allowed), provider))


def send_draft(profile: str | None, draft_id: str) -> None:
    """Send a draft made by `prepare_reply`."""
    asyncio.run(_send_async(profile, draft_id))


def subject_tokens(text: str) -> tuple[str, ...]:
    """Lowercase, accent-free alphanumeric words of `text`."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return tuple(re.findall(r"[a-z0-9]+", stripped))


def _addresses(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    found: set[str] = set()
    for part in value.split(","):
        text = part.strip()
        if "<" in text and ">" in text:
            text = text[text.index("<") + 1 : text.index(">")]
        text = text.strip().lower()
        if text:
            found.add(text)
    return found


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
        "Expected an https Google Docs, Sheets, Slides or Drive document, file or folder link "
        f"on docs.google.com or drive.google.com, got {shown}"
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
        and _DOC_PATH.fullmatch(parts.path) is not None
    )
    if not ok:
        raise LessonError(problem)


def _claimed(item: LessonItem) -> bool:
    """Whether a finish started recently enough that it may still be running."""
    if not item.claimed_at:
        return False
    try:
        started = datetime.strptime(item.claimed_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return False
    return (datetime.now(UTC) - started).total_seconds() < CLAIM_SECONDS


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
    """The page images of one mail, renamed to `page-N.ext` (the mail's names are data)."""
    pages: list[Path] = []
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() in _IMAGE_SUFFIXES:
            pages.append(path)
    renamed: list[str] = []
    for number, path in enumerate(pages[:_MAX_IMAGES], start=1):
        target = directory / f"page-{number}{path.suffix.lower()}"
        if path != target:
            if target.exists():
                continue
            path.rename(target)
        renamed.append(str(target))
    return tuple(renamed)


def _parse(path: Path) -> LessonItem:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        profile = data["profile"]
        item = LessonItem(
            claimed_at=str(data.get("claimed_at", "")),
            created_at=str(data["created_at"]),
            docs=tuple(str(d) for d in data["docs"]),
            draft_id=str(data.get("draft_id", "")),
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


async def _prepare_async(
    profile: str | None,
    message_id: str,
    body: str,
    allowed: tuple[str, ...],
    provider: Any | None,
) -> str:
    chosen = provider if provider is not None else _provider_for(profile)

    async def _create_and_check() -> str:
        created = await chosen.mail_reply(
            message_id=message_id, body=body, body_type="text", no_signature=True
        )
        nested = created.get("draft") if isinstance(created, Mapping) else None
        draft = nested if isinstance(nested, Mapping) else created
        draft_id = str(draft.get("id") or "").strip() if isinstance(draft, Mapping) else ""
        if not draft_id:
            raise LessonError("Expected the reply response to carry a draft id, got none")
        problem = _recipient_problem(draft, allowed)
        if problem:
            with contextlib.suppress(Exception):
                await chosen.mail_delete_draft(draft_id=draft_id)
            raise LessonError(problem)
        return draft_id

    return await asyncio.wait_for(_create_and_check(), timeout=REPLY_TIMEOUT_SECONDS)


def _provider_for(profile: str | None) -> Any:
    # Imported here: the provider layer imports a lot and is only needed to send.
    from blumkin.config import load_config
    from blumkin.providers import get_provider

    return get_provider(load_config(profile=profile) if profile else load_config())


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
                claimed_at="",
                created_at=_now(),
                docs=(),
                draft_id="",
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


def _recipient_problem(draft: Mapping[str, Any], allowed: tuple[str, ...]) -> str | None:
    wanted = {address.lower() for address in allowed}
    to = _addresses(draft.get("to"))
    others = _addresses(draft.get("cc")) | _addresses(draft.get("bcc"))
    if to and not others and to <= wanted:
        return None
    return (
        "Expected the reply to go only to an allowlisted sender, got a draft with another or "
        "no recipient (the draft was deleted)"
    )


def _release(store: Path, item_id: str) -> None:
    with _locked(store), contextlib.suppress(LessonNotFoundError):
        _save(store, _with(_load(store, item_id), claimed_at=""))


def _reply_body(docs: Sequence[str]) -> str:
    lines = ["Your lesson is ready:", ""]
    lines.extend(docs)
    lines.extend(["", "Sent by: blumkin loop (lesson task)"])
    return "\n".join(lines) + "\n"


def _save(store: Path, item: LessonItem) -> LessonItem:
    write_private(store / f"{item.id}.json", json.dumps(item.as_dict(), sort_keys=True))
    return item


async def _send_async(profile: str | None, draft_id: str) -> None:
    provider = _provider_for(profile)
    await asyncio.wait_for(
        provider.mail_send_draft(draft_id=draft_id), timeout=REPLY_TIMEOUT_SECONDS
    )


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
    return LessonItem(
        claimed_at=data["claimed_at"],
        created_at=data["created_at"],
        docs=tuple(data["docs"]),
        draft_id=data["draft_id"],
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
_DOC_PATH = re.compile(
    r"/(?:document|spreadsheets|presentation|file)/d/[A-Za-z0-9_-]+(?:/.*)?"
    r"|/drive/(?:u/\d+/)?folders/[A-Za-z0-9_-]+/?"
)
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
