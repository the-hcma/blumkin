"""Append-only run journal for the loop runner (issue #501).

Stored at ``<config dir>/loop/journal.jsonl``. Each line is one run: trigger, task,
instance, outcome and ids only — never message bodies, amounts or file contents.
Writes use an exclusive lock, one write and an fsync so a crash leaves at most a
torn last line (ignored by readers). When the file nears ``_MAX_JOURNAL_BYTES``,
``append_run`` compacts it to the newest run per trigger so ``loop status`` stays
readable.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from blumkin.browser.profiles import BrowserProfileError, ensure_private_dir
from blumkin.loop.types import RunOutcome, RunRecord, TriggerKind

_COMPACT_AT_BYTES = 24 * 1024 * 1024
_JOURNAL_FILENAME = "journal.jsonl"
_LOCK_FILENAME = "journal.lock"
_MAX_JOURNAL_BYTES = 32 * 1024 * 1024
_STORE_DIRNAME = "loop"


class LoopJournalError(ValueError):
    """The run journal is damaged somewhere other than a torn last line."""


def append_run(
    config_dir: Path,
    *,
    instance: str,
    outcome: RunOutcome,
    reason: str,
    task: str,
    trigger: str,
    trigger_kind: TriggerKind,
    run_id: str | None = None,
) -> RunRecord:
    """Append one run record and return it."""
    directory = _ensure_loop_dir(config_dir)
    record = RunRecord(
        at=_now(),
        instance=instance,
        outcome=outcome,
        reason=reason,
        run_id=run_id or uuid.uuid4().hex,
        task=task,
        trigger=trigger,
        trigger_kind=trigger_kind,
    )
    with _writing(directory) as journal:
        _append(
            journal,
            {
                "event": "run",
                "instance": record.instance,
                "outcome": record.outcome.value,
                "reason": record.reason,
                "run_id": record.run_id,
                "task": record.task,
                "trigger": record.trigger,
                "trigger_kind": record.trigger_kind.value,
            },
            at=record.at,
        )
        _maybe_compact(journal)
    return record


def list_runs(config_dir: Path, *, limit: int | None = None) -> list[RunRecord]:
    """Recent runs, oldest first; `limit` keeps only the newest N when set."""
    directory = config_dir / _STORE_DIRNAME
    journal = directory / _JOURNAL_FILENAME
    if not journal.is_file() or journal.is_symlink():
        return []
    with _writing(directory) as path:
        # Exclusive: compact an oversized journal left by an older build, then read.
        if path.is_file() and path.stat().st_size > _COMPACT_AT_BYTES:
            _maybe_compact(path)
        records = _read_records(path, allow_oversize=False)
    if limit is not None and limit >= 0:
        records = records[-limit:]
    return records


def recent_by_trigger(config_dir: Path) -> dict[str, RunRecord]:
    """The newest run per trigger name."""
    latest: dict[str, RunRecord] = {}
    for record in list_runs(config_dir):
        latest[record.trigger] = record
    return latest


def _append(journal: Path, entry: dict[str, Any], *, at: str) -> None:
    fd = os.open(journal, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        data = _read_fd(fd)
        complete = data[: data.rfind(b"\n") + 1]
        if len(complete) != len(data):
            os.ftruncate(fd, len(complete))
        seq = complete.count(b"\n") + 1
        line = json.dumps({"at": at, "seq": seq, **entry}, sort_keys=True) + "\n"
        payload = line.encode("utf-8")
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_loop_dir(config_dir: Path) -> Path:
    try:
        return ensure_private_dir(config_dir / _STORE_DIRNAME)
    except BrowserProfileError as exc:
        raise LoopJournalError(str(exc)) from exc


def _maybe_compact(journal: Path) -> None:
    """Rewrite to the newest run per trigger when the file is large. Lock held."""
    if not journal.is_file():
        return
    size = journal.stat().st_size
    if size < _COMPACT_AT_BYTES:
        return
    records = _read_records(journal, allow_oversize=True)
    latest: dict[str, RunRecord] = {}
    for record in records:
        latest[record.trigger] = record
    kept = sorted(latest.values(), key=lambda r: (r.at, r.run_id))
    _rewrite(journal, kept)


def _no_constants(name: str) -> Any:
    raise ValueError(f"unexpected constant {name}")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_record(entry: dict[str, Any]) -> RunRecord:
    if entry.get("event") != "run":
        raise ValueError("not a run event")
    return RunRecord(
        at=str(entry["at"]),
        instance=str(entry["instance"]),
        outcome=RunOutcome(str(entry["outcome"])),
        reason=str(entry.get("reason", "")),
        run_id=str(entry["run_id"]),
        task=str(entry["task"]),
        trigger=str(entry["trigger"]),
        trigger_kind=TriggerKind(str(entry["trigger_kind"])),
    )


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while True:
        chunk = os.pread(fd, 65536, offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _read_records(journal: Path, *, allow_oversize: bool) -> list[RunRecord]:
    try:
        fd = os.open(journal, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    try:
        size = os.fstat(fd).st_size
        if size > _MAX_JOURNAL_BYTES and not allow_oversize:
            raise LoopJournalError(
                f"Expected a run journal of at most {_MAX_JOURNAL_BYTES} bytes, got more"
            )
        data = _read_fd(fd)
    finally:
        os.close(fd)
    records: list[RunRecord] = []
    lines = data.split(b"\n")
    complete = lines[:-1]
    for number, raw in enumerate(complete, start=1):
        try:
            entry = json.loads(raw, parse_constant=_no_constants)
            if not isinstance(entry, dict):
                raise ValueError("not an object")
            if entry.get("seq") != number:
                raise ValueError("out of sequence")
            records.append(_parse_record(entry))
        except (ValueError, KeyError, TypeError) as exc:
            raise LoopJournalError(
                f"Expected a valid run journal entry on line {number}, got {type(exc).__name__}"
            ) from exc
    return records


def _rewrite(journal: Path, records: list[RunRecord]) -> None:
    """Replace the journal with `records` (re-sequenced). Lock held."""
    tmp = journal.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        for seq, record in enumerate(records, start=1):
            line = (
                json.dumps(
                    {
                        "at": record.at,
                        "event": "run",
                        "instance": record.instance,
                        "outcome": record.outcome.value,
                        "reason": record.reason,
                        "run_id": record.run_id,
                        "seq": seq,
                        "task": record.task,
                        "trigger": record.trigger,
                        "trigger_kind": record.trigger_kind.value,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            payload = line.encode("utf-8")
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, journal)


@contextlib.contextmanager
def _writing(directory: Path) -> Iterator[Path]:
    lock_path = directory / _LOCK_FILENAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory / _JOURNAL_FILENAME
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
