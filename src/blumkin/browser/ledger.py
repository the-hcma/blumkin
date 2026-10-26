"""The task instance ledger: what has been filed for one run of a browser task (issue #488).

An instance is a task plus a key (this pay period's time sheet, that trip's expense report).
Its state is an append-only journal of JSON lines under
`<config dir>/browser-tasks/<task>/<instance>/journal.jsonl`, so the question "what is filed
so far?" has an answer that survives a restarted session. Each item (a day, a receipt) goes
staged, then filled, then verified (read back from the page) or failed; a failed item can be
staged again. The ledger behaves like a transaction that accumulates changes until the user
submits: recording that the user submitted is its own explicit call and nothing infers it.

Entries are appended under an exclusive lock with one write and an fsync, so concurrent
writers cannot interleave and a crash leaves at most a torn last line. A reader ignores a
torn last line, and the next writer cuts it off; a damaged line anywhere else is an error
rather than a silently skipped one. The journal holds identifiers, states, model versions and
file hashes only, never a field value, an amount, a vendor or a path. It is kept private
(`0700` directories, `0600` file).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from blumkin.browser.profiles import BrowserProfileError, ensure_private_dir, validate_task_name


class ItemKind(StrEnum):
    """What kind of thing is being filed."""

    DAY = "day"
    RECEIPT = "receipt"


@dataclass(frozen=True, slots=True)
class ItemRecord:
    """One item as the journal last left it."""

    at: str
    item: str
    kind: ItemKind
    model_version: int | None
    sha256: str | None
    state: ItemState


class ItemState(StrEnum):
    """Where an item is: staged, filled into the form, then verified or failed."""

    STAGED = "staged"
    FILLED = "filled"
    VERIFIED = "verified"
    FAILED = "failed"


class LedgerError(ValueError):
    """A name, a state change or a journal is not acceptable."""


class LedgerJournalError(LedgerError):
    """The journal is damaged somewhere other than a torn last line."""


class LedgerNotFoundError(LookupError):
    """The task has no such instance."""


@dataclass(frozen=True, slots=True)
class LedgerStatus:
    """The folded state of one instance."""

    expected: Mapping[ItemKind, int]
    instance: str
    items: tuple[ItemRecord, ...]
    phase: Phase
    task: str

    def count(self, kind: ItemKind, state: ItemState) -> int:
        """How many items of `kind` are in `state`."""
        return sum(1 for i in self.items if i.kind is kind and i.state is state)

    def summary(self) -> str:
        """One line, for example `3 of 10 days filed, 4 receipts filed, not submitted`."""
        parts: list[str] = []
        for kind in ItemKind:
            total = self.expected.get(kind)
            seen = any(i.kind is kind for i in self.items)
            if total is None and not seen:
                continue
            filed = self.count(kind, ItemState.VERIFIED)
            if total is None:
                parts.append(f"{filed} {_noun(kind, filed)} filed")
            else:
                parts.append(f"{filed} of {total} {_noun(kind, total)} filed")
        if not parts:
            parts.append("nothing filed yet")
        pending = sum(1 for i in self.items if i.state in (ItemState.STAGED, ItemState.FILLED))
        failed = sum(1 for i in self.items if i.state is ItemState.FAILED)
        if pending:
            parts.append(f"{pending} in progress")
        if failed:
            parts.append(f"{failed} failed")
        parts.append(_PHASE_TEXT[self.phase])
        return ", ".join(parts)


class Phase(StrEnum):
    """Whether the instance still accepts changes."""

    OPEN = "open"
    SUBMITTED = "submitted"
    DISCARDED = "discarded"


def begin_instance(
    config_dir: Path,
    task: str,
    instance: str,
    *,
    expected: Mapping[ItemKind, int] | None = None,
) -> LedgerStatus:
    """Start an instance, optionally saying how many items of each kind to expect."""
    _check_task(task)
    _check_instance(instance)
    totals = _check_expected(expected or {})
    directory = _ensure_instance_dir(config_dir, task, instance)
    with _writing(directory) as journal:
        if _fold(_read(journal, task, instance), task, instance) is not None:
            raise LedgerError(
                f"Expected a new instance, got {instance!r} which already exists for task {task!r}"
            )
        _append(journal, {"event": "begin", "expected": {k.value: n for k, n in totals.items()}})
        _fsync_dir(directory)
        return _status(journal, task, instance)


def discard_instance(config_dir: Path, task: str, instance: str) -> LedgerStatus:
    """Close the instance as discarded; the journal is kept."""
    return _close(config_dir, task, instance, "discarded", "discard")


def instance_status(config_dir: Path, task: str, instance: str) -> LedgerStatus:
    """The folded state of one instance."""
    _check_task(task)
    _check_instance(instance)
    directory = _existing_instance_dir(config_dir, task, instance)
    if directory is None:
        raise LedgerNotFoundError(f"Expected an instance {instance!r} of task {task!r}, got none")
    with _reading(directory):
        return _status(directory / _JOURNAL_FILENAME, task, instance)


def list_instances(config_dir: Path, task: str) -> list[tuple[str, Phase]]:
    """The task's instances with their phase, sorted by name."""
    _check_task(task)
    root = config_dir / _STORE_DIRNAME / task
    if not root.is_dir() or root.is_symlink():
        return []
    found: list[tuple[str, Phase]] = []
    for child in sorted(root.iterdir()):
        if not _INSTANCE.fullmatch(child.name) or child.is_symlink() or not child.is_dir():
            continue
        with contextlib.suppress(LedgerNotFoundError):
            found.append((child.name, instance_status(config_dir, task, child.name).phase))
    return found


def mark_submitted(config_dir: Path, task: str, instance: str) -> LedgerStatus:
    """Record that the user submitted the form; nothing else ever infers it."""
    return _close(config_dir, task, instance, "submitted", "mark submitted")


def record_item(
    config_dir: Path,
    task: str,
    instance: str,
    kind: ItemKind,
    item: str,
    state: ItemState,
    *,
    model_version: int | None = None,
    sha256: str | None = None,
) -> ItemRecord:
    """Move one item to `state`; a receipt carries the SHA-256 of its file from the start."""
    _check_task(task)
    _check_instance(instance)
    if not _ITEM.fullmatch(item):
        raise LedgerError(
            "Expected an item id of 1-64 lowercase letters, digits, '.', '_' or '-' starting "
            f"with a letter or digit, got {item!r}"
        )
    if model_version is not None and (
        isinstance(model_version, bool) or not isinstance(model_version, int) or model_version < 1
    ):
        raise LedgerError(f"Expected a model version of 1 or more, got {model_version!r}")
    if sha256 is not None and (kind is not ItemKind.RECEIPT or not _SHA256.fullmatch(sha256)):
        raise LedgerError(
            "Expected a sha256 of 64 lowercase hex digits for a receipt only, "
            f"got {sha256!r} for a {kind.value}"
        )
    directory = _existing_instance_dir(config_dir, task, instance)
    if directory is None:
        raise LedgerNotFoundError(f"Expected an instance {instance!r} of task {task!r}, got none")
    with _writing(directory) as journal:
        folded = _fold(_read(journal, task, instance), task, instance)
        if folded is None:
            raise LedgerNotFoundError(
                f"Expected an instance {instance!r} of task {task!r}, got none"
            )
        _require_open(folded, "record an item")
        previous = folded.items.get((kind, item))
        entry = _item_entry(kind, item, state, previous, model_version, sha256)
        _append(journal, entry)
        return _record(entry, _now())


def _append(journal: Path, entry: dict[str, Any]) -> None:
    """Write one entry after cutting off a torn last line; the lock is held."""
    fd = os.open(journal, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        data = _read_fd(fd)
        complete = data[: data.rfind(b"\n") + 1]
        if len(complete) != len(data):
            os.ftruncate(fd, len(complete))
        seq = complete.count(b"\n") + 1
        line = json.dumps({"at": _now(), "seq": seq, **entry}, sort_keys=True) + "\n"
        payload = line.encode("utf-8")
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_expected(expected: Mapping[ItemKind, int]) -> dict[ItemKind, int]:
    checked: dict[ItemKind, int] = {}
    for kind, total in expected.items():
        if isinstance(total, bool) or not isinstance(total, int) or not 1 <= total <= _MAX_EXPECTED:
            raise LedgerError(
                f"Expected a total of 1-{_MAX_EXPECTED} {kind.value} items, got {total!r}"
            )
        checked[ItemKind(kind)] = total
    return checked


def _check_instance(instance: str) -> None:
    if not _INSTANCE.fullmatch(instance):
        raise LedgerError(
            "Expected an instance name of 1-64 lowercase letters, digits, '-' or '_' starting "
            f"and ending with a letter or digit, got {instance!r}"
        )


def _check_task(task: str) -> None:
    try:
        validate_task_name(task)
    except BrowserProfileError as exc:
        raise LedgerError(str(exc)) from exc


def _close(config_dir: Path, task: str, instance: str, event: str, action: str) -> LedgerStatus:
    _check_task(task)
    _check_instance(instance)
    directory = _existing_instance_dir(config_dir, task, instance)
    if directory is None:
        raise LedgerNotFoundError(f"Expected an instance {instance!r} of task {task!r}, got none")
    with _writing(directory) as journal:
        folded = _fold(_read(journal, task, instance), task, instance)
        if folded is None:
            raise LedgerNotFoundError(
                f"Expected an instance {instance!r} of task {task!r}, got none"
            )
        _require_open(folded, action)
        _append(journal, {"event": event})
        return _status(journal, task, instance)


def _ensure_instance_dir(config_dir: Path, task: str, instance: str) -> Path:
    try:
        path = config_dir
        for part in (_STORE_DIRNAME, task, instance):
            path = ensure_private_dir(path / part)
    except BrowserProfileError as exc:
        raise LedgerError(str(exc)) from exc
    return path


def _existing_instance_dir(config_dir: Path, task: str, instance: str) -> Path | None:
    path = config_dir / _STORE_DIRNAME / task / instance
    if path.is_symlink():
        raise LedgerError(f"Expected a real directory at {path}, got a symlink")
    return path if path.is_dir() else None


def _fold(data: bytes, task: str, instance: str) -> _Folded | None:
    """The state the journal describes, or None when it holds no `begin` yet."""
    folded: _Folded | None = None
    lines = data.split(b"\n")
    complete = lines[:-1]  # the last piece has no newline: empty, or a torn write
    for number, raw in enumerate(complete, start=1):
        try:
            entry = json.loads(raw, parse_constant=_no_constants)
            if not isinstance(entry, dict):
                raise ValueError("not an object")
            if entry.get("seq") != number:
                raise ValueError("out of sequence")
            folded = _apply(folded, entry)
        except (ValueError, KeyError, TypeError, LedgerError) as exc:
            raise LedgerJournalError(
                f"Expected a valid journal entry on line {number} of the journal for "
                f"{task!r}/{instance!r}, got {type(exc).__name__}"
            ) from exc
    return folded


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _item_entry(
    kind: ItemKind,
    item: str,
    state: ItemState,
    previous: ItemRecord | None,
    model_version: int | None,
    sha256: str | None,
) -> dict[str, Any]:
    if previous is None:
        if state is not ItemState.STAGED:
            raise LedgerError(
                f"Expected a new {kind.value} {item!r} to be staged first, got {state.value}"
            )
        if kind is ItemKind.RECEIPT and sha256 is None:
            raise LedgerError(f"Expected a sha256 for the new receipt {item!r}, got none")
    else:
        if state not in _NEXT[previous.state]:
            raise LedgerError(
                f"Expected {item!r} to go from {previous.state.value} to one of "
                f"{', '.join(s.value for s in _NEXT[previous.state]) or 'nothing'}, "
                f"got {state.value}"
            )
        if sha256 is not None and sha256 != previous.sha256:
            raise LedgerError(f"Expected the sha256 of {item!r} to stay the same, got another")
    return {
        "event": "item",
        "item": item,
        "kind": kind.value,
        "model_version": model_version
        if model_version is not None
        else (previous.model_version if previous else None),
        "sha256": sha256 if sha256 is not None else (previous.sha256 if previous else None),
        "state": state.value,
    }


def _no_constants(name: str) -> Any:
    raise ValueError(f"unexpected constant {name}")


def _noun(kind: ItemKind, count: int) -> str:
    return kind.value if count == 1 else f"{kind.value}s"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(journal: Path, task: str, instance: str) -> bytes:
    try:
        fd = os.open(journal, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return b""
    try:
        if os.fstat(fd).st_size > _MAX_JOURNAL_BYTES:
            raise LedgerJournalError(
                f"Expected a journal of at most {_MAX_JOURNAL_BYTES} bytes for "
                f"{task!r}/{instance!r}, got more"
            )
        return _read_fd(fd)
    finally:
        os.close(fd)


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while chunk := os.pread(fd, 1 << 16, offset):
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


@contextlib.contextmanager
def _reading(directory: Path) -> Iterator[None]:
    """Hold the shared lock, so an append cannot be seen half done."""
    lock = directory / _LOCK_FILENAME
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)


def _record(entry: dict[str, Any], at: str) -> ItemRecord:
    return ItemRecord(
        at=at,
        item=entry["item"],
        kind=ItemKind(entry["kind"]),
        model_version=entry["model_version"],
        sha256=entry["sha256"],
        state=ItemState(entry["state"]),
    )


def _require_open(folded: _Folded, action: str) -> None:
    if folded.phase is not Phase.OPEN:
        raise LedgerError(f"Expected an open instance to {action}, got a {folded.phase.value} one")


def _status(journal: Path, task: str, instance: str) -> LedgerStatus:
    folded = _fold(_read(journal, task, instance), task, instance)
    if folded is None:
        raise LedgerNotFoundError(f"Expected an instance {instance!r} of task {task!r}, got none")
    return LedgerStatus(
        expected=dict(folded.expected),
        instance=instance,
        items=tuple(sorted(folded.items.values(), key=lambda i: (i.kind.value, i.item))),
        phase=folded.phase,
        task=task,
    )


@contextlib.contextmanager
def _writing(directory: Path) -> Iterator[Path]:
    """Hold the exclusive lock and yield the journal path."""
    fd = os.open(directory / _LOCK_FILENAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory / _JOURNAL_FILENAME
    finally:
        os.close(fd)


@dataclass(slots=True)
class _Folded:
    expected: dict[ItemKind, int]
    items: dict[tuple[ItemKind, str], ItemRecord]
    phase: Phase


def _apply(folded: _Folded | None, entry: dict[str, Any]) -> _Folded:
    """Apply one journal entry to the state so far, checking it the way a writer would."""
    event = entry["event"]
    at = entry["at"]
    if not isinstance(at, str):
        raise ValueError("bad time")
    if event == "begin":
        if folded is not None:
            raise ValueError("begin twice")
        expected = {ItemKind(k): v for k, v in dict(entry["expected"]).items()}
        return _Folded(expected=_check_expected(expected), items={}, phase=Phase.OPEN)
    if folded is None:
        raise ValueError("entry before begin")
    if folded.phase is not Phase.OPEN:
        raise ValueError("entry after the instance was closed")
    if event == "item":
        record = _record(entry, at)
        if not _ITEM.fullmatch(record.item) or (
            record.sha256 is not None and not _SHA256.fullmatch(record.sha256)
        ):
            raise ValueError("bad item")
        previous = folded.items.get((record.kind, record.item))
        _item_entry(
            record.kind, record.item, record.state, previous, record.model_version, record.sha256
        )
        folded.items[(record.kind, record.item)] = record
    elif event == "submitted":
        folded.phase = Phase.SUBMITTED
    elif event == "discarded":
        folded.phase = Phase.DISCARDED
    else:
        raise ValueError("unknown event")
    return folded


_ITEM = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_INSTANCE = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?")
_JOURNAL_FILENAME = "journal.jsonl"
_LOCK_FILENAME = ".lock"
_MAX_EXPECTED = 1000
_MAX_JOURNAL_BYTES = 8 * 1024 * 1024
_NEXT: dict[ItemState, tuple[ItemState, ...]] = {
    ItemState.STAGED: (ItemState.FILLED, ItemState.FAILED),
    ItemState.FILLED: (ItemState.VERIFIED, ItemState.FAILED),
    ItemState.VERIFIED: (),
    ItemState.FAILED: (ItemState.STAGED,),
}
_PHASE_TEXT = {
    Phase.OPEN: "not submitted",
    Phase.SUBMITTED: "submitted",
    Phase.DISCARDED: "discarded",
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STORE_DIRNAME = "browser-tasks"
