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

from blumkin.browser.ledger import LedgerError, LedgerNotFoundError
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "copied": self.copied,
            "dest": str(self.dest),
            "message_id": self.message_id,
            "name": self.name,
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
    require_high_confidence: bool = True,
) -> RehomeResult:
    """Move or copy ``source`` into the managed folder under its canonical name."""
    validate_task_name(task)
    if not _INSTANCE.fullmatch(instance):
        raise RehomeError(
            "Expected an instance name of 1-64 lowercase letters, digits, '-' or '_' "
            f"starting and ending with a letter or digit, got {instance!r}"
        )
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
    ensure_private_dir(dest_dir)
    name = canonical_receipt_name(
        date=date,
        vendor=vendor,
        amount=amount,
        currency=currency,
        sha256=digest,
        suffix=src.suffix,
    )
    dest = dest_dir / name
    if dest.exists() and (not dest.is_file() or file_sha256(dest) != digest):
        # Same canonical stem, different file: keep uniqueness via the hash (already in name).
        raise RehomeError(
            f"Expected a free destination name {name!r} in the managed folder, got a different file"
        )
    if dest.exists() and file_sha256(dest) == digest:
        raise AlreadyFiledError(
            f"Expected a new receipt, got sha256 {digest[:8]}… already at {dest.name}"
        )
    copy_only = message_id is not None
    _append_move(
        config_dir,
        task,
        instance,
        {
            "event": "intent",
            "copied": copy_only,
            "dest_name": name,
            "message_id": message_id,
            "sha256": digest,
            "source": str(src.resolve()),
        },
    )
    try:
        _place(src, dest, digest=digest, copy_only=copy_only)
    except Exception:
        # Leave the intent line; a later restore or retry can finish or undo.
        raise
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
    moves = _read_moves(config_dir, task, instance)
    results: list[RestoreResult] = []
    seen: set[str] = set()
    for entry in reversed(moves):
        if entry.get("event") != "result":
            continue
        digest = str(entry.get("sha256") or "")
        if not _SHA256.fullmatch(digest) or digest in seen:
            continue
        seen.add(digest)
        dest = Path(str(entry.get("dest") or ""))
        source = Path(str(entry.get("source") or ""))
        copied = bool(entry.get("copied"))
        restored = False
        if dest.is_file() and not dest.is_symlink() and file_sha256(dest) == digest:
            if copied:
                # Email copy: remove the managed copy only; leave the mailbox download alone.
                dest.unlink()
                restored = True
            else:
                source.parent.mkdir(parents=True, exist_ok=True)
                if source.exists():
                    raise RehomeError(
                        f"Expected original path {str(source)!r} to be free for restore, "
                        "got an existing file"
                    )
                _place(dest, source, digest=digest, copy_only=False)
                restored = True
        results.append(RestoreResult(dest=dest, restored=restored, sha256=digest, source=source))
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
        elif event == "restored":
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


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _place(src: Path, dest: Path, *, digest: str, copy_only: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Always copy+verify first so a cross-filesystem move cannot leave a torn dest.
    shutil.copy2(src, dest)
    if file_sha256(dest) != digest:
        dest.unlink(missing_ok=True)
        raise RehomeError("Expected the managed copy to match the source sha256, got a mismatch")
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


def _suffix(suffix: str) -> str:
    text = suffix.strip().lower()
    if not text:
        return ""
    if not text.startswith("."):
        text = f".{text}"
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", text):
        raise RehomeError(f"Expected a short file suffix like .jpg, got {suffix!r}")
    return text
