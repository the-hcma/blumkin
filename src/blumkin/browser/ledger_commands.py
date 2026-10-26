"""Payloads behind the `blumkin browser ledger` commands (issue #488).

The ledger records what has been filed for one instance of a browser task (a pay period's
time sheet, a trip's expense report). These functions turn its calls into the dictionaries
the CLI prints. Closing an instance (`submitted`, `discard`) needs `yes`: it is the user's
statement about the real form, so a command line cannot make it by accident.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from blumkin.browser.ledger import (
    ItemKind,
    ItemState,
    LedgerError,
    LedgerStatus,
    begin_instance,
    discard_instance,
    instance_status,
    list_instances,
    mark_submitted,
    record_item,
)
from blumkin.config import BrowserSetup


def ledger_begin(
    config: BrowserSetup, task: str, instance: str, *, expect: Sequence[str]
) -> dict[str, Any]:
    """Start an instance; `expect` is `KIND=COUNT` strings such as `day=10`."""
    return _payload(begin_instance(config.config_dir, task, instance, expected=_expected(expect)))


def ledger_discard(config: BrowserSetup, task: str, instance: str, *, yes: bool) -> dict[str, Any]:
    """Close the instance as discarded (the journal is kept)."""
    _require_yes(yes, task, instance, "discard", "the journal is kept")
    return _payload(discard_instance(config.config_dir, task, instance))


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


def _payload(status: LedgerStatus) -> dict[str, Any]:
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
                "model_version": item.model_version,
                "sha256": item.sha256,
                "state": item.state.value,
            }
            for item in status.items
        ],
        "ok": True,
        "phase": status.phase.value,
        "summary": status.summary(),
        "task": status.task,
    }


def _require_yes(yes: bool, task: str, instance: str, action: str, note: str) -> None:
    if not yes:
        raise LedgerError(
            f"Expected --yes to {action} {instance!r} of task {task!r}, got none; {note}"
        )
