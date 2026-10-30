"""Payloads behind the `blumkin browser ledger` commands (issue #488).

The ledger records what has been filed for one instance of a browser task (a pay period's
time sheet, a trip's expense report). These functions turn its calls into the dictionaries
the CLI prints. Closing an instance (`submitted`, `discard`) needs `yes`: it is the user's
statement about the real form, so a command line cannot make it by accident.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from blumkin.browser.commands import close_task_browser
from blumkin.browser.handoff import HandoffResult
from blumkin.browser.ledger import (
    ItemKind,
    ItemState,
    LedgerError,
    LedgerStatus,
    Phase,
    SessionOutcome,
    begin_instance,
    discard_instance,
    instance_status,
    list_instances,
    mark_submitted,
    record_item,
    record_report,
    record_session,
)
from blumkin.browser.ledger_ops import (
    apply_item,
    handoff_from_ledger,
    reconcile,
    stage_item,
    verify_item,
)
from blumkin.browser.receipt_rehome import RehomeError, restore_rehomed
from blumkin.config import BrowserSetup


def ledger_apply(
    config: BrowserSetup,
    task: str,
    instance: str,
    kind: str,
    item: str,
    *,
    flow: str,
    value: Sequence[str] = (),
    sha256: str | None = None,
) -> dict[str, Any]:
    """Run a recorded flow for one item and mark it filled (no-op if already verified)."""
    result = apply_item(
        config.config_dir,
        task,
        instance,
        ItemKind(kind),
        item,
        flow_name=flow,
        values=_value_map(value),
        sha256=sha256,
    )
    return result.as_dict() | {"ok": True}


def ledger_begin(
    config: BrowserSetup, task: str, instance: str, *, expect: Sequence[str]
) -> dict[str, Any]:
    """Start an instance; `expect` is `KIND=COUNT` strings such as `day=10`."""
    return _payload(begin_instance(config.config_dir, task, instance, expected=_expected(expect)))


def ledger_discard(config: BrowserSetup, task: str, instance: str, *, yes: bool) -> dict[str, Any]:
    """Close the instance as discarded (the journal is kept; rehomed receipts are restored)."""
    _require_yes(yes, task, instance, "discard", "the journal is kept")
    status = instance_status(config.config_dir, task, instance)
    if status.phase is not Phase.OPEN:
        raise LedgerError(f"Expected an open instance to discard, got a {status.phase.value} one")
    try:
        restored = restore_rehomed(config.config_dir, task, instance, yes=True)
    except RehomeError as exc:
        raise LedgerError(str(exc)) from exc
    payload = _payload(discard_instance(config.config_dir, task, instance))
    payload["receipts_restored"] = [row.as_dict() for row in restored]
    return payload


def ledger_handoff(
    config: BrowserSetup,
    task: str,
    instance: str,
    *,
    step: Sequence[str],
    decision: Sequence[str] = (),
) -> dict[str, Any]:
    """Build the filled-in / NOT-submitted handoff from the ledger."""
    result: HandoffResult = handoff_from_ledger(
        config.config_dir, task, instance, steps=step, decisions=decision
    )
    return result.to_payload() | {"instance": instance, "ok": True}


def ledger_list(config: BrowserSetup, task: str) -> dict[str, Any]:
    """The task's instances and whether each is still open."""
    return {
        "instances": [
            {"instance": name, "phase": phase.value}
            for name, phase in list_instances(config.config_dir, task)
        ],
        "ok": True,
        "task": task,
    }


def ledger_record(
    config: BrowserSetup,
    task: str,
    instance: str,
    kind: str,
    item: str,
    state: str,
    *,
    model_version: int | None,
    sha256: str | None,
) -> dict[str, Any]:
    """Move one item to a new state and say where the instance stands."""
    record = record_item(
        config.config_dir,
        task,
        instance,
        ItemKind(kind),
        item,
        ItemState(state),
        model_version=model_version,
        sha256=sha256,
    )
    status = instance_status(config.config_dir, task, instance)
    return {
        "instance": instance,
        "item": record.item,
        "kind": record.kind.value,
        "model_version": record.model_version,
        "ok": True,
        "sha256": record.sha256,
        "state": record.state.value,
        "summary": status.summary(),
        "task": task,
    }


def ledger_reconcile(
    config: BrowserSetup,
    task: str,
    instance: str,
    *,
    kind: str,
    page_item: Sequence[str],
) -> dict[str, Any]:
    """Compare ledger items of ``kind`` with ids observed on the live page (read-only)."""
    return reconcile(
        config.config_dir,
        task,
        instance,
        kind=ItemKind(kind),
        page_items=page_item,
    ).as_dict()


def ledger_stage(
    config: BrowserSetup,
    task: str,
    instance: str,
    kind: str,
    item: str,
    *,
    sha256: str | None = None,
    model_version: int | None = None,
) -> dict[str, Any]:
    """Stage one item unless it is already verified (then a no-op)."""
    result = stage_item(
        config.config_dir,
        task,
        instance,
        ItemKind(kind),
        item,
        sha256=sha256,
        model_version=model_version,
    )
    return result.as_dict() | {"ok": True}


def ledger_status(config: BrowserSetup, task: str, instance: str) -> dict[str, Any]:
    """Where the instance stands: counts, per-item state and whether it was submitted."""
    return _payload(instance_status(config.config_dir, task, instance))


def ledger_submitted(
    config: BrowserSetup, task: str, instance: str, *, yes: bool
) -> dict[str, Any]:
    """Record that the user submitted the real form (nothing else ever records it)."""
    _require_yes(
        yes, task, instance, "mark submitted", "say yes only if you submitted the form yourself"
    )
    return _payload(mark_submitted(config.config_dir, task, instance))


def ledger_verify(
    config: BrowserSetup,
    task: str,
    instance: str,
    kind: str,
    item: str,
    *,
    present: bool,
    sha256: str | None = None,
    model_version: int | None = None,
) -> dict[str, Any]:
    """Mark an item verified when the page shows it, otherwise failed."""
    result = verify_item(
        config.config_dir,
        task,
        instance,
        ItemKind(kind),
        item,
        present_on_page=present,
        sha256=sha256,
        model_version=model_version,
    )
    return result.as_dict() | {"ok": True}


def _expected(values: Sequence[str]) -> dict[ItemKind, int]:
    expected: dict[ItemKind, int] = {}
    for value in values:
        name, sep, count = value.partition("=")
        try:
            kind = ItemKind(name)
            total = int(count) if sep else None
        except ValueError:
            kind, total = None, None
        if kind is None or total is None:
            kinds = ", ".join(k.value for k in ItemKind)
            raise LedgerError(f"Expected KIND=COUNT with KIND one of {kinds}, got {value!r}")
        expected[kind] = total
    return expected


def ledger_close(
    config: BrowserSetup,
    task: str,
    instance: str,
    *,
    close_browser: bool = False,
    outcome: str,
    yes: bool,
) -> dict[str, Any]:
    """Record how the session ended (done or pending), then optionally close the browser.

    The user's answer is the `outcome`; the browser is closed only after it is recorded, and its
    profile (the saved sign-in) is kept. `--yes` says the user gave that answer.
    """
    _require_yes(
        yes,
        task,
        instance,
        "close the session",
        "say yes only after the user chose done or pending",
    )
    try:
        chosen = SessionOutcome(outcome)
    except ValueError:
        raise LedgerError(f"Expected an outcome of done or pending, got {outcome!r}") from None
    status = record_session(config.config_dir, task, instance, chosen)
    payload = _payload(status)
    if close_browser:
        closed = close_task_browser(config.config_dir, task)
        payload["browser_closed"] = closed["was_running"]
    return payload


def ledger_report(
    config: BrowserSetup,
    task: str,
    instance: str,
    *,
    draft_saved: bool | None,
    report_id: str | None,
    total_confirmed: bool | None,
) -> dict[str, Any]:
    """Record the report itself: its server-side id, that a draft is saved, a confirmed total."""
    return _payload(
        record_report(
            config.config_dir,
            task,
            instance,
            draft_saved=draft_saved,
            report_id=report_id,
            total_confirmed=total_confirmed,
        )
    )


def _payload(status: LedgerStatus) -> dict[str, Any]:
    report = status.report
    session = status.session
    return {
        "counts": {
            kind.value: {state.value: status.count(kind, state) for state in ItemState}
            for kind in ItemKind
        },
        "expected": {kind.value: total for kind, total in status.expected.items()},
        "instance": status.instance,
        "items": [
            {
                "at": item.at,
                "item": item.item,
                "kind": item.kind.value,
                "low_confidence": item.low_confidence,
                "model_version": item.model_version,
                "sha256": item.sha256,
                "state": item.state.value,
            }
            for item in status.items
        ],
        "local_only": [i.item for i in status.local_only()],
        "ok": True,
        "on_server": [i.item for i in status.on_server()],
        "phase": status.phase.value,
        "report": None
        if report is None
        else {
            "at": report.at,
            "draft_saved": report.draft_saved,
            "report_id": report.report_id,
            "total_confirmed": report.total_confirmed,
        },
        "resume": status.resume_hint(),
        "session": None
        if session is None
        else {"at": session.at, "outcome": session.outcome.value},
        "submitted": status.phase is Phase.SUBMITTED,
        "summary": status.summary(),
        "task": status.task,
    }


def _require_yes(yes: bool, task: str, instance: str, action: str, note: str) -> None:
    if not yes:
        raise LedgerError(
            f"Expected --yes to {action} {instance!r} of task {task!r}, got none; {note}"
        )


def _value_map(values: Sequence[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        key, sep, text = value.partition("=")
        if not sep or not key.strip():
            raise LedgerError(f"Expected KEY=VALUE for a flow binding, got {value!r}")
        parsed[key.strip()] = text
    return parsed
