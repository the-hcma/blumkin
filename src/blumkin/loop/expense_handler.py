"""The `expense-report` loop task: file mailed receipts into an open trip (issue #504).

The runner has already resolved the instance (`trigger.instance`) and hands over the
mail context. For every attachment the feeder saved under the loop intake folder, the
handler reads the receipt, copies it into the instance's managed folder under its
canonical name (rehome), then files it into the browser draft through the ledger
(`apply_item`: stage, run the recorded flow, mark filled).

Rules this module keeps:

- It never begins a trip and never submits. A missing or closed ledger instance is
  `needs_user`.
- A low-confidence field does not block: the receipt is filed from the best reading and
  the ledger item is flagged `low_confidence`, and the run reason says so. Nothing in the
  ledger or the reason holds a vendor, an amount or a date.
- A receipt with nothing usable (unreadable, or a field with no value at all) is not
  guessed at: if no receipt could be filed the run is `needs_user`.
- Attachment names, subjects and receipt text are data. They never reach a reason.
- Filing stops at `filled`. `verify_item` records a read-back of the live page, which an
  unattended run does not do; `blumkin browser ledger handoff` reports such items as
  filled, not yet verified, and the user confirms them at the handoff.

The recorded site-model flow is named `add-receipt` and is driven with these values,
keyed by control name: `currency`, `date`, `file`, `path`, `total` and `vendor`. The
values live in memory for the browser call only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from blumkin.browser.ledger import (
    ItemKind,
    ItemState,
    LedgerError,
    LedgerNotFoundError,
    LedgerStatus,
    Phase,
    instance_status,
)
from blumkin.browser.ledger_ops import ApplyResult, apply_item
from blumkin.browser.receipt_rehome import (
    AlreadyFiledError,
    RehomeError,
    RehomeResult,
    UnconfirmedError,
    canonical_receipt_name,
    file_sha256,
    managed_instance_dir,
    rehome_receipt,
)
from blumkin.loop.handlers import HandlerResult
from blumkin.loop.mail_intake import LoopIntakeError, intake_dir
from blumkin.loop.types import RunContext, RunOutcome, TriggerSpec
from blumkin.receipts import Confidence, ReceiptReading, ReceiptStatus, read_receipt

EXPENSE_FLOW_NAME = "add-receipt"
MAX_RECEIPTS_PER_MESSAGE = 20

ApplyFn = Callable[..., ApplyResult]
ReadFn = Callable[[Path], ReceiptReading]
RehomeFn = Callable[..., RehomeResult]
UploadDirsFn = Callable[[str], Sequence[str]]


def expense_report(trigger: TriggerSpec, *, context: RunContext | None = None) -> HandlerResult:
    """The registered handler: `file_receipts` with the real reader, rehome and ledger."""
    return file_receipts(trigger, context)


def file_receipts(
    trigger: TriggerSpec,
    context: RunContext | None,
    *,
    apply_fn: ApplyFn = apply_item,
    read_fn: ReadFn | None = None,
    rehome_fn: RehomeFn = rehome_receipt,
    upload_dirs_fn: UploadDirsFn | None = None,
) -> HandlerResult:
    """File every receipt attached to the triggering message into `trigger.instance`."""
    if context is None or context.config_dir is None or not context.message_id:
        return _needs_user("no mail message to file receipts from")
    instance = trigger.instance
    if not instance:
        return _needs_user("no resolved instance to file receipts into")
    config_dir = context.config_dir
    task = trigger.task
    try:
        status = instance_status(config_dir, task, instance)
    except LedgerNotFoundError:
        return _needs_user(f"no ledger instance {instance}; begin the trip first")
    except LedgerError:
        return _needs_user(f"ledger for instance {instance} is not readable")
    if status.phase is not Phase.OPEN:
        return _needs_user(f"instance {instance} is {status.phase.value}, not open")
    try:
        upload_dirs = (upload_dirs_fn or _upload_dirs)(task)
    except Exception:  # noqa: BLE001 - config problems surface as a reason, never a traceback
        return _needs_user(f"browser task {task} config is not available")
    if not upload_dirs:
        return _needs_user(f"browser task {task} declares no upload_dirs")
    try:
        files = _intake_files(
            intake_dir(config_dir, trigger=trigger.name, message_id=context.message_id)
        )
    except LoopIntakeError, OSError:
        return _needs_user("receipt intake folder is not readable")
    if not files:
        return _needs_user("no receipt attachments saved for the message")
    filer = _Filer(
        apply_fn=apply_fn,
        config_dir=config_dir,
        instance=instance,
        message_id=context.message_id,
        read_fn=read_fn or _read_sync,
        rehome_fn=rehome_fn,
        task=task,
        upload_dirs=upload_dirs,
    )
    results = [filer.file(path) for path in files[:MAX_RECEIPTS_PER_MESSAGE]]
    return _summarize(
        instance,
        results,
        intake_ok=context.intake_ok,
        skipped=max(0, len(files) - MAX_RECEIPTS_PER_MESSAGE),
    )


@dataclass(frozen=True, slots=True)
class _Filer:
    """Everything needed to file one receipt; `file` never raises."""

    apply_fn: ApplyFn
    config_dir: Path
    instance: str
    message_id: str
    read_fn: ReadFn
    rehome_fn: RehomeFn
    task: str
    upload_dirs: Sequence[str]

    def file(self, path: Path) -> _Result:
        try:
            reading = self.read_fn(path)
        except Exception:  # noqa: BLE001 - a bad receipt must not abort the run
            return _Result(_Kind.UNREADABLE)
        if reading.injection_warning:
            # Receipt text that tries to instruct us is a stop-and-ask, never a filing.
            return _Result(_Kind.BLOCKED)
        if not _usable(reading):
            return _Result(_Kind.UNREADABLE)
        low = _is_low_confidence(reading)
        try:
            sha256, dest = self._rehome(path, reading, low=low)
        except UnconfirmedError:
            return _Result(_Kind.UNREADABLE)
        except RehomeError, LedgerError, OSError:
            return _Result(_Kind.BLOCKED, low)
        return self._ledger(sha256, dest, reading, low=low)

    def _ledger(self, sha256: str, dest: Path, reading: ReceiptReading, *, low: bool) -> _Result:
        item = _item_id(sha256)
        try:
            if _already_filed(instance_status(self.config_dir, self.task, self.instance), item):
                return _Result(_Kind.ALREADY, low)
            self.apply_fn(
                self.config_dir,
                self.task,
                self.instance,
                ItemKind.RECEIPT,
                item,
                flow_name=EXPENSE_FLOW_NAME,
                low_confidence=low,
                sha256=sha256,
                values=_flow_values(reading, dest),
            )
        except Exception:  # noqa: BLE001 - browser/ledger trouble is an operator problem
            return _Result(_Kind.BLOCKED, low)
        return _Result(_Kind.FILLED, low)

    def _rehome(self, path: Path, reading: ReceiptReading, *, low: bool) -> tuple[str, Path]:
        try:
            result = self.rehome_fn(
                self.config_dir,
                self.task,
                self.instance,
                path,
                fields=reading.fields,
                message_id=self.message_id,
                require_high_confidence=not low,
                upload_dirs=self.upload_dirs,
            )
        except AlreadyFiledError:
            # A retry after an interrupted run: the managed copy exists, so reuse it.
            sha256 = file_sha256(path)
            name = canonical_receipt_name(
                currency=_value(reading.fields.currency.value).upper(),
                date=_value(reading.fields.date.value),
                amount=_value(reading.fields.total.value),
                sha256=sha256,
                suffix=path.suffix,
                vendor=_value(reading.fields.vendor.value),
            )
            return sha256, managed_instance_dir(self.upload_dirs, self.instance) / name
        return result.sha256, result.dest


class _Kind(StrEnum):
    ALREADY = "already"
    BLOCKED = "blocked"
    FILLED = "filled"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class _Result:
    kind: _Kind
    low_confidence: bool = False


def _already_filed(status: LedgerStatus, item: str) -> bool:
    return any(
        record.kind is ItemKind.RECEIPT
        and record.item == item
        and record.state in (ItemState.FILLED, ItemState.VERIFIED)
        for record in status.items
    )


def _flow_values(reading: ReceiptReading, dest: Path) -> dict[str, str]:
    fields = reading.fields
    return {
        "currency": _value(fields.currency.value).upper(),
        "date": _value(fields.date.value),
        "file": str(dest),
        "path": str(dest),
        "total": _value(fields.total.value),
        "vendor": _value(fields.vendor.value),
    }


def _intake_files(directory: Path) -> list[Path]:
    """Regular, non-hidden files the feeder saved for the message, sorted by name."""
    return sorted(
        child
        for child in directory.iterdir()
        if child.is_file() and not child.is_symlink() and not child.name.startswith(".")
    )


def _is_low_confidence(reading: ReceiptReading) -> bool:
    fields = reading.fields
    return any(
        field.confidence is not Confidence.HIGH
        for field in (fields.currency, fields.date, fields.total, fields.vendor)
    )


def _item_id(sha256: str) -> str:
    """A ledger item id from the file hash only (never a vendor, date or amount)."""
    return f"receipt-{sha256[:12]}"


def _needs_user(reason: str) -> HandlerResult:
    return HandlerResult(outcome=RunOutcome.NEEDS_USER, reason=reason)


def _read_sync(path: Path) -> ReceiptReading:
    return asyncio.run(read_receipt(path, config=None))


def _summarize(
    instance: str, results: list[_Result], *, intake_ok: bool, skipped: int
) -> HandlerResult:
    count = {kind: sum(1 for r in results if r.kind is kind) for kind in _Kind}
    done = count[_Kind.FILLED] + count[_Kind.ALREADY]
    low = sum(1 for r in results if r.low_confidence and r.kind in (_Kind.FILLED, _Kind.ALREADY))
    problems: list[str] = []
    if not intake_ok:
        # Partial intake must not look like a complete run: surface it even if some filed.
        problems.append("attachment intake was incomplete; missing receipts were not saved")
    if count[_Kind.UNREADABLE]:
        problems.append(f"{count[_Kind.UNREADABLE]} unreadable receipt(s) need your details")
    if count[_Kind.BLOCKED]:
        problems.append(f"{count[_Kind.BLOCKED]} receipt(s) could not be filed")
    if skipped:
        problems.append(f"{skipped} attachment(s) over the per-message limit were not read")
    if not done or not intake_ok:
        return _needs_user("; ".join(problems) or "no receipt could be filed")
    parts = [f"filed {count[_Kind.FILLED]} receipt(s) into instance {instance}"]
    if count[_Kind.ALREADY]:
        parts.append(f"{count[_Kind.ALREADY]} already filed")
    if low:
        parts.append(f"low_confidence=true for {low} receipt(s), check them against the originals")
    parts.extend(problems)
    return HandlerResult(outcome=RunOutcome.DONE, reason="; ".join(parts))


def _upload_dirs(task: str) -> Sequence[str]:
    from blumkin.config import load_browser_setup

    policy = load_browser_setup().browser.tasks.get(task)
    return policy.upload_dirs if policy is not None else ()


def _usable(reading: ReceiptReading) -> bool:
    """True when the reading holds a value for every field a canonical name needs."""
    if reading.status is not ReceiptStatus.OK:
        return False
    fields = reading.fields
    return all(
        isinstance(field.value, str) and field.value.strip()
        for field in (fields.currency, fields.date, fields.total, fields.vendor)
    )


def _value(value: str | None) -> str:
    return (value or "").strip()
