"""Rename and rehome expense receipts into a managed instance folder (issue #491).

A confirmed receipt is renamed to a canonical name (date, vendor, amount, currency and a
short SHA-256) and placed under a per-instance folder inside one of the task's
``upload_dirs``. An intent-then-result journal makes a crash recoverable: a half-finished
move can be completed or restored. Cross-filesystem moves copy, verify the hash, then
remove the source. Email attachments are copied (not moved) and the message id is recorded.
Unconfirmed fields leave the file untouched. Nothing is deleted without the operator's
approval (``restore`` on discard puts files back).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from blumkin.browser.ledger import LedgerError, LedgerNotFoundError, Phase, instance_status
from blumkin.browser.profiles import ensure_private_dir, validate_task_name
from blumkin.receipts import Confidence, ReceiptFields

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AMOUNT = re.compile(r"^\d+(?:\.\d{1,2})?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INSTANCE = re.compile(r"^[a-z0-9]([a-z0-9_-]{0,62}[a-z0-9])?$")
_MAX_VENDOR = 48
_MAX_JOURNAL_BYTES = 1_000_000
_MOVES_FILENAME = "receipt-moves.jsonl"
_SUPERSEDED_DIRNAME = "superseded"
_STORE_DIRNAME = "browser-tasks"


class RehomeError(ValueError):
    """A receipt cannot be renamed or rehomed."""


class AlreadyFiledError(RehomeError):
    """A receipt with this SHA-256 is already in the managed folder."""


class UnconfirmedError(RehomeError):
    """One or more receipt fields are not confirmed; the file is left alone."""


@dataclass(frozen=True, slots=True)
class RehomeResult:
    """Where a receipt ended up after a successful rehome."""

    dest: Path
    name: str
    sha256: str
    source: Path
    copied: bool
    message_id: str | None
    #: Name of the earlier file this one replaced (kept aside, not deleted), if any.
    replaced: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "copied": self.copied,
            "dest": str(self.dest),
            "message_id": self.message_id,
            "name": self.name,
            "replaced": self.replaced,
            "sha256": self.sha256,
            "source": str(self.source),
        }


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """One file put back to its original path."""

    dest: Path
    restored: bool
    sha256: str
    source: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "dest": str(self.dest),
            "restored": self.restored,
            "sha256": self.sha256,
            "source": str(self.source),
        }


def canonical_receipt_name(
    *,
    date: str,
    vendor: str,
    amount: str,
    currency: str,
    sha256: str,
    suffix: str,
) -> str:
    """Build ``YYYY-MM-DD_vendor_amount-CURRENCY_hash8.ext`` from confirmed fields."""
    if not _DATE.fullmatch(date):
        raise RehomeError(f"Expected a confirmed date YYYY-MM-DD, got {date!r}")
    if not _AMOUNT.fullmatch(amount):
        raise RehomeError(f"Expected a confirmed amount like 12.50, got {amount!r}")
    if not _CURRENCY.fullmatch(currency):
        raise RehomeError(f"Expected a confirmed ISO currency code, got {currency!r}")
    if not _SHA256.fullmatch(sha256):
        raise RehomeError(f"Expected a sha256 of 64 lowercase hex digits, got {sha256!r}")
    clean_vendor = sanitize_vendor_for_name(vendor)
    if not clean_vendor:
        raise RehomeError("Expected a confirmed vendor that yields a usable name, got none")
    ext = _suffix(suffix)
    short = sha256[:8]
    return f"{date}_{clean_vendor}_{amount}-{currency}_{short}{ext}"


def file_sha256(path: Path) -> str:
    """SHA-256 of a regular file's contents."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def find_filed(config_dir: Path, task: str, instance: str, ref: str) -> RehomeResult:
    """The filed receipt named ``ref`` (its file name, or at least 8 hex digits of its SHA-256)."""
    wanted = ref.strip().lower()
    filed = _completed_by_hash(config_dir, task, instance)
    matches = [
        result
        for digest, result in filed.items()
        if result.name.lower() == wanted
        or (len(wanted) >= 8 and re.fullmatch(r"[0-9a-f]+", wanted) and digest.startswith(wanted))
    ]
    if len(matches) != 1:
        found = "none" if not matches else f"{len(matches)} matches"
        raise RehomeError(
            f"Expected one filed receipt matching {ref!r} (a file name or 8+ hex digits of its "
            f"sha256), got {found}"
        )
    return matches[0]


def managed_instance_dir(upload_dirs: Sequence[str], instance: str) -> Path:
    """Per-instance folder under the first ``upload_dirs`` entry."""
    if not upload_dirs:
        raise RehomeError("Expected at least one upload_dirs folder for the task, got none")
    if not _INSTANCE.fullmatch(instance):
        raise RehomeError(
            "Expected an instance name of 1-64 lowercase letters, digits, '-' or '_' "
            f"starting and ending with a letter or digit, got {instance!r}"
        )
    root = Path(upload_dirs[0]).expanduser()
    if not root.is_absolute():
        raise RehomeError(
            "Expected an absolute upload_dirs folder (or one starting with ~/), "
            f"got {upload_dirs[0]!r}"
        )
    return root / instance


def rehome_receipt(
    config_dir: Path,
    task: str,
    instance: str,
    source: Path,
    *,
    fields: ReceiptFields | Mapping[str, Mapping[str, Any]],
    upload_dirs: Sequence[str],
    message_id: str | None = None,
    replace: str | None = None,
    require_high_confidence: bool = True,
) -> RehomeResult:
    """Move or copy ``source`` into the managed folder under its canonical name.

    With ``replace`` (a filed receipt's name or hash), the new file is filed first and the old one
    is then set aside under the instance's ``superseded`` folder, so a bad render can be swapped
    without hand-deleting a hash-named file and nothing is ever lost.
    """
    if replace is not None:
        # Before anything is looked up or moved: a closed instance accepts no more changes.
        _require_open_instance(config_dir, task, instance)
    old = find_filed(config_dir, task, instance, replace) if replace is not None else None
    if old is None:
        return _rehome_one(
            config_dir,
            task,
            instance,
            source,
            fields=fields,
            upload_dirs=upload_dirs,
            message_id=message_id,
            require_high_confidence=require_high_confidence,
        )
    _check_replaceable(config_dir, task, instance, old)
    src = source.expanduser()
    digest = file_sha256(src) if src.is_file() and not src.is_symlink() else ""
    if digest == old.sha256:
        raise RehomeError(
            f"Expected a replacement that differs from {old.name!r}, got the same bytes"
        )
    filed = _completed_by_hash(config_dir, task, instance).get(digest)
    if filed is not None:
        # An earlier --replace filed the new receipt and stopped before setting the old one
        # aside: finish that instead of failing on "already filed".
        result = filed
    else:
        result = _rehome_one(
            config_dir,
            task,
            instance,
            source,
            fields=fields,
            upload_dirs=upload_dirs,
            message_id=message_id,
            require_high_confidence=require_high_confidence,
        )
    _supersede(config_dir, task, instance, old)
    return dataclasses.replace(result, replaced=old.name)


def _rehome_one(
    config_dir: Path,
    task: str,
    instance: str,
    source: Path,
    *,
    fields: ReceiptFields | Mapping[str, Mapping[str, Any]],
    upload_dirs: Sequence[str],
    message_id: str | None,
    require_high_confidence: bool,
) -> RehomeResult:
    _require_open_instance(config_dir, task, instance)
    src = source.expanduser()
    if not src.is_file() or src.is_symlink():
        raise RehomeError(f"Expected a regular receipt file, got {str(source)!r}")
    date, vendor, amount, currency = _confirmed_fields(fields, require_high=require_high_confidence)
    digest = file_sha256(src)
    completed = _completed_by_hash(config_dir, task, instance)
    if digest in completed:
        raise AlreadyFiledError(
            f"Expected a new receipt, got sha256 {digest[:8]}… already filed as "
            f"{completed[digest].name}"
        )
    dest_dir = managed_instance_dir(upload_dirs, instance)
    name = canonical_receipt_name(
        date=date,
        vendor=vendor,
        amount=amount,
        currency=currency,
        sha256=digest,
        suffix=src.suffix,
    )
    dest = dest_dir / name
    copy_only = message_id is not None
    if src.resolve() == dest.resolve():
        raise RehomeError(
            f"Expected a source outside the managed destination {dest.name!r}, got the same path"
        )
    ensure_private_dir(dest_dir)
    if dest.is_file() and not dest.is_symlink() and file_sha256(dest) == digest:
        # Dest matches: either already filed (result journaled) or an interrupted move.
        if digest in completed:
            raise AlreadyFiledError(
                f"Expected a new receipt, got sha256 {digest[:8]}… already at {dest.name}"
            )
        intent = _live_intents(_read_moves(config_dir, task, instance)).get(digest)
        if intent is None or not _intent_matches(
            intent,
            dest=dest.resolve(),
            source=src.resolve(),
            copied=copy_only,
        ):
            raise RehomeError(
                f"Expected a live journaled intent for sha256 {digest[:8]}… before resume, got none"
            )
        if not copy_only and src.is_file() and not src.is_symlink():
            src.unlink()
        _append_move(
            config_dir,
            task,
            instance,
            {
                "event": "result",
                "copied": copy_only,
                "dest": str(dest.resolve()),
                "message_id": message_id,
                "sha256": digest,
                "source": str(src.resolve()),
            },
        )
        return RehomeResult(
            dest=dest.resolve(),
            name=name,
            sha256=digest,
            source=src.resolve(),
            copied=copy_only,
            message_id=message_id,
        )
    if dest.exists():
        raise RehomeError(
            f"Expected a free destination name {name!r} in the managed folder, got a different file"
        )
    _append_move(
        config_dir,
        task,
        instance,
        {
            "event": "intent",
            "copied": copy_only,
            "dest": str(dest.resolve()),
            "dest_name": name,
            "message_id": message_id,
            "sha256": digest,
            "source": str(src.resolve()),
        },
    )
    _place(src, dest, digest=digest, copy_only=copy_only)
    _append_move(
        config_dir,
        task,
        instance,
        {
            "event": "result",
            "copied": copy_only,
            "dest": str(dest.resolve()),
            "message_id": message_id,
            "sha256": digest,
            "source": str(src.resolve()),
        },
    )
    return RehomeResult(
        dest=dest.resolve(),
        name=name,
        sha256=digest,
        source=src.resolve(),
        copied=copy_only,
        message_id=message_id,
    )


def restore_rehomed(
    config_dir: Path,
    task: str,
    instance: str,
    *,
    yes: bool,
) -> list[RestoreResult]:
    """Put every successfully rehomed file back to its original path (discard undo)."""
    if not yes:
        raise RehomeError(
            f"Expected --yes to restore receipts for {instance!r} of task {task!r}, got none"
        )
    validate_task_name(task)
    if not _INSTANCE.fullmatch(instance):
        raise RehomeError(
            "Expected an instance name of 1-64 lowercase letters, digits, '-' or '_' "
            f"starting and ending with a letter or digit, got {instance!r}"
        )
    moves = _read_moves(config_dir, task, instance)
    results: list[RestoreResult] = []
    seen: set[str] = set()
    for entry in reversed(moves):
        event = entry.get("event")
        digest = str(entry.get("sha256") or "")
        if event in {"restored", "superseded"}:
            # A superseded file was set aside on purpose; discard leaves it where it is.
            if _SHA256.fullmatch(digest):
                seen.add(digest)
            continue
        if event not in {"result", "intent"}:
            continue
        if not _SHA256.fullmatch(digest) or digest in seen:
            continue
        dest = Path(str(entry.get("dest") or ""))
        if not dest.parts:
            continue
        seen.add(digest)
        source = Path(str(entry.get("source") or ""))
        copied = bool(entry.get("copied"))
        if not _restore_one(dest=dest, source=source, digest=digest, copied=copied):
            raise RehomeError(
                f"Expected to restore sha256 {digest[:8]}… from {str(dest)!r}, "
                "got a missing or mismatched managed file"
            )
        results.append(RestoreResult(dest=dest, restored=True, sha256=digest, source=source))
        _append_move(
            config_dir,
            task,
            instance,
            {
                "event": "restored",
                "copied": copied,
                "dest": str(dest),
                "sha256": digest,
                "source": str(source),
            },
        )
    return list(reversed(results))


def sanitize_vendor_for_name(vendor: str) -> str:
    """Fold vendor text into a lowercase path segment (no separators or controls)."""
    text = unicodedata.normalize("NFKC", vendor).strip().lower()
    cleaned: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("C") or category in {"Zl", "Zp"}:
            continue
        if char.isspace() or char in {"_", "-", ".", "/", "\\", ":", "*", "?", '"', "<", ">", "|"}:
            cleaned.append("-")
            continue
        if char.isalnum():
            cleaned.append(char)
    folded = re.sub(r"-{2,}", "-", "".join(cleaned)).strip("-")
    return folded[:_MAX_VENDOR]


def _append_move(config_dir: Path, task: str, instance: str, entry: Mapping[str, Any]) -> None:
    directory = _instance_dir(config_dir, task, instance)
    ensure_private_dir(directory)
    journal = directory / _MOVES_FILENAME
    line = json.dumps({"at": _now(), **entry}, sort_keys=True) + "\n"
    fd = os.open(journal, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _completed_by_hash(config_dir: Path, task: str, instance: str) -> dict[str, RehomeResult]:
    found: dict[str, RehomeResult] = {}
    for entry in _read_moves(config_dir, task, instance):
        event = entry.get("event")
        digest = str(entry.get("sha256") or "")
        if not _SHA256.fullmatch(digest):
            continue
        if event == "result":
            found[digest] = RehomeResult(
                dest=Path(str(entry.get("dest") or "")),
                name=Path(str(entry.get("dest") or "")).name,
                sha256=digest,
                source=Path(str(entry.get("source") or "")),
                copied=bool(entry.get("copied")),
                message_id=(
                    str(entry["message_id"]) if entry.get("message_id") is not None else None
                ),
            )
        elif event in {"restored", "superseded"}:
            found.pop(digest, None)
    return found


def _confirmed_fields(
    fields: ReceiptFields | Mapping[str, Mapping[str, Any]], *, require_high: bool
) -> tuple[str, str, str, str]:
    payload = fields.to_payload() if isinstance(fields, ReceiptFields) else fields
    values: dict[str, str] = {}
    for key in ("date", "vendor", "total", "currency"):
        row = payload.get(key) if isinstance(payload, Mapping) else None
        if not isinstance(row, Mapping):
            raise UnconfirmedError(f"Expected confirmed field {key!r}, got none")
        confidence = str(row.get("confidence") or "")
        value = row.get("value")
        if require_high and confidence != Confidence.HIGH.value:
            raise UnconfirmedError(
                f"Expected {key!r} at high confidence before rehome, got {confidence!r}"
            )
        if not isinstance(value, str) or not value.strip():
            raise UnconfirmedError(f"Expected a confirmed value for {key!r}, got none")
        values[key] = value.strip()
    return values["date"], values["vendor"], values["total"], values["currency"].upper()


def _instance_dir(config_dir: Path, task: str, instance: str) -> Path:
    path = config_dir / _STORE_DIRNAME / task / instance
    if not path.is_dir() or path.is_symlink():
        raise LedgerNotFoundError(f"Expected an instance {instance!r} of task {task!r}, got none")
    return path


def _intent_matches(intent: Mapping[str, Any], *, dest: Path, source: Path, copied: bool) -> bool:
    return (
        bool(intent.get("copied")) is copied
        and Path(str(intent.get("dest") or "")).resolve() == dest
        and Path(str(intent.get("source") or "")).resolve() == source
    )


def _live_intents(moves: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Intents that have not yet been closed by a result or restored event."""
    live: dict[str, Mapping[str, Any]] = {}
    for entry in moves:
        event = entry.get("event")
        digest = str(entry.get("sha256") or "")
        if not _SHA256.fullmatch(digest):
            continue
        if event == "intent":
            live[digest] = entry
        elif event in {"result", "restored", "superseded"}:
            live.pop(digest, None)
    return live


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _original_intact(source: Path, digest: str) -> bool:
    return source.is_file() and not source.is_symlink() and file_sha256(source) == digest


def _place(src: Path, dest: Path, *, digest: str, copy_only: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Copy to a temporary name, verify, then rename so a crash never leaves a torn dest.
    tmp = dest.with_name(f".{dest.name}.part")
    tmp.unlink(missing_ok=True)
    shutil.copy2(src, tmp)
    if file_sha256(tmp) != digest:
        tmp.unlink(missing_ok=True)
        raise RehomeError("Expected the managed copy to match the source sha256, got a mismatch")
    os.replace(tmp, dest)
    if not copy_only:
        src.unlink()


def _read_moves(config_dir: Path, task: str, instance: str) -> list[dict[str, Any]]:
    try:
        directory = _instance_dir(config_dir, task, instance)
    except LedgerNotFoundError:
        return []
    journal = directory / _MOVES_FILENAME
    if not journal.is_file() or journal.is_symlink():
        return []
    data = journal.read_bytes()
    if len(data) > _MAX_JOURNAL_BYTES:
        raise LedgerError(
            f"Expected a receipt-moves journal of at most {_MAX_JOURNAL_BYTES} bytes, "
            f"got {len(data)}"
        )
    text = data.decode("utf-8")
    if text and not text.endswith("\n"):
        text = text[: text.rfind("\n") + 1]
    entries: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(
                f"Expected a valid receipt-moves entry on line {number}, got {type(exc).__name__}"
            ) from exc
        if not isinstance(payload, dict):
            raise LedgerError(f"Expected a receipt-moves object on line {number}, got a non-object")
        entries.append(payload)
    return entries


def _restore_one(*, dest: Path, source: Path, digest: str, copied: bool) -> bool:
    # Intent never placed, or restore already moved bytes back: dest gone, source intact.
    if not dest.is_symlink() and not dest.exists() and _original_intact(source, digest):
        return True
    if not dest.is_file() or dest.is_symlink() or file_sha256(dest) != digest:
        return False
    if copied:
        # Email copy: remove the managed copy only; leave the mailbox download alone.
        dest.unlink()
        return True
    source.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        if _original_intact(source, digest):
            # Half-finished move: source already has the bytes; drop the managed copy.
            dest.unlink()
            return True
        raise RehomeError(
            f"Expected original path {str(source)!r} to be free for restore, got an existing file"
        )
    _place(dest, source, digest=digest, copy_only=False)
    return True


def _aside_path(aside_dir: Path, old: RehomeResult) -> Path:
    """Where the old file goes (or already went): its own name, else a numbered one."""
    for number in range(0, 1000):
        name = old.name if number == 0 else f"{number}-{old.name}"
        candidate = aside_dir / name
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        if (
            candidate.is_file()
            and not candidate.is_symlink()
            and file_sha256(candidate) == old.sha256
        ):
            return candidate
    raise RehomeError(f"Expected a free name for {old.name!r} in the superseded folder, got none")


def _check_replaceable(config_dir: Path, task: str, instance: str, old: RehomeResult) -> None:
    """Fail before anything is filed when the old receipt cannot be set aside."""
    aside_dir = _instance_dir(config_dir, task, instance) / _SUPERSEDED_DIRNAME
    intact = (
        old.dest.is_file() and not old.dest.is_symlink() and file_sha256(old.dest) == old.sha256
    )
    already_aside = aside_dir.is_dir() and any(
        path.is_file() and not path.is_symlink() and file_sha256(path) == old.sha256
        for path in aside_dir.iterdir()
    )
    if not intact and not already_aside:
        raise RehomeError(
            f"Expected filed receipt {old.name!r} to be intact before replacing it, "
            "got a missing or changed file"
        )
    if intact:
        _aside_path(aside_dir, old)


def _require_open_instance(config_dir: Path, task: str, instance: str) -> None:
    validate_task_name(task)
    if not _INSTANCE.fullmatch(instance):
        raise RehomeError(
            "Expected an instance name of 1-64 lowercase letters, digits, '-' or '_' "
            f"starting and ending with a letter or digit, got {instance!r}"
        )
    status = instance_status(config_dir, task, instance)
    if status.phase is not Phase.OPEN:
        raise RehomeError(
            f"Expected an open ledger instance to rehome a receipt, got a {status.phase.value} one"
        )


def _supersede(config_dir: Path, task: str, instance: str, old: RehomeResult) -> None:
    """Move a filed receipt out of the upload folder into ``superseded`` and journal it.

    Safe to repeat: when an earlier run already moved the file but did not journal it, only the
    journal entry is written.
    """
    aside_dir = _instance_dir(config_dir, task, instance) / _SUPERSEDED_DIRNAME
    ensure_private_dir(aside_dir)
    aside = _aside_path(aside_dir, old)
    if old.dest.is_file() and not old.dest.is_symlink():
        if file_sha256(old.dest) != old.sha256:
            raise RehomeError(
                f"Expected filed receipt {old.name!r} to be unchanged, got a different file"
            )
        if not aside.exists():
            _place(old.dest, aside, digest=old.sha256, copy_only=False)
        else:
            old.dest.unlink()
    elif not (aside.is_file() and file_sha256(aside) == old.sha256):
        raise RehomeError(
            f"Expected filed receipt {old.name!r} to be intact before replacing it, "
            "got a missing or changed file"
        )
    _append_move(
        config_dir,
        task,
        instance,
        {
            "event": "superseded",
            "aside": str(aside.resolve()),
            "dest": str(old.dest),
            "sha256": old.sha256,
        },
    )


def _suffix(suffix: str) -> str:
    text = suffix.strip().lower()
    if not text:
        return ""
    if not text.startswith("."):
        text = f".{text}"
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", text):
        raise RehomeError(f"Expected a short file suffix like .jpg, got {suffix!r}")
    return text
