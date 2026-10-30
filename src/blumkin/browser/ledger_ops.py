"""Ledger-backed filing: stage, apply, verify, reconcile and handoff (issue #489).

Before filing, check the ledger (and optionally the live page) so an item is never
added twice. Reconcile is read-only. The handoff is always generated from the ledger
and always reports ``submitted: false``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blumkin.browser.handoff import HandoffResult
from blumkin.browser.ledger import (
    ItemKind,
    ItemRecord,
    ItemState,
    LedgerError,
    LedgerStatus,
    instance_status,
    record_item,
)
from blumkin.browser.model import Flow, FlowStep, SiteModel, load_model


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Outcome of applying a recorded flow for one ledger item."""

    item: ItemRecord
    noop: bool
    status: LedgerStatus

    def as_dict(self) -> dict[str, Any]:
        return {
            "item": {
                "item": self.item.item,
                "kind": self.item.kind.value,
                "state": self.item.state.value,
            },
            "noop": self.noop,
            "status": _status_payload(self.status),
        }


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Read-only comparison of ledger items against what the live page shows."""

    only_in_ledger: tuple[str, ...]
    only_on_page: tuple[str, ...]
    matched: tuple[str, ...]
    task: str
    instance: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "instance": self.instance,
            "matched": list(self.matched),
            "ok": True,
            "only_in_ledger": list(self.only_in_ledger),
            "only_on_page": list(self.only_on_page),
            "task": self.task,
        }


# Click / fill / goto / upload callbacks used while applying a flow step.
StepRunner = Callable[[FlowStep, Mapping[str, str]], None]


def apply_item(
    config_dir: Path,
    task: str,
    instance: str,
    kind: ItemKind,
    item: str,
    *,
    flow_name: str,
    values: Mapping[str, str] | None = None,
    sha256: str | None = None,
    low_confidence: bool | None = None,
    model: SiteModel | None = None,
    run_step: StepRunner | None = None,
) -> ApplyResult:
    """Run a recorded flow for ``item``, then mark it filled (or no-op if already verified)."""
    status = instance_status(config_dir, task, instance)
    existing = _find(status, kind, item)
    if existing is not None and existing.state is ItemState.VERIFIED:
        return ApplyResult(item=existing, noop=True, status=status)
    if existing is None or existing.state is ItemState.FAILED:
        stage_item(
            config_dir,
            task,
            instance,
            kind,
            item,
            sha256=sha256,
            low_confidence=low_confidence,
            model_version=(model.version if model is not None else None),
        )
    site = model if model is not None else load_model(config_dir, task)
    if site is None:
        raise LedgerError(f"Expected a saved site model for task {task!r}, got none")
    flow = _require_flow(site, flow_name)
    runner = run_step or _default_run_step(task)
    bindings = dict(values or {})
    bindings.setdefault("item", item)
    try:
        for step in flow.steps:
            runner(step, bindings)
    except Exception:
        record_item(
            config_dir,
            task,
            instance,
            kind,
            item,
            ItemState.FAILED,
            low_confidence=low_confidence,
            model_version=site.version,
            sha256=sha256 if kind is ItemKind.RECEIPT else None,
        )
        raise
    record = record_item(
        config_dir,
        task,
        instance,
        kind,
        item,
        ItemState.FILLED,
        low_confidence=low_confidence,
        model_version=site.version,
        sha256=sha256 if kind is ItemKind.RECEIPT else None,
    )
    return ApplyResult(
        item=record,
        noop=False,
        status=instance_status(config_dir, task, instance),
    )


def handoff_from_ledger(
    config_dir: Path,
    task: str,
    instance: str,
    *,
    steps: Sequence[str],
    decisions: Sequence[str] = (),
) -> HandoffResult:
    """Build the filled-in / NOT-submitted handoff from the current ledger status."""
    if not steps:
        raise LedgerError("Expected at least one final-step instruction for the handoff, got none")
    status = instance_status(config_dir, task, instance)
    summary = [status.summary()]
    summary.append(
        f"on the server (read back from the page): {len(status.on_server())} item(s); "
        f"only local or not yet read back: {len(status.local_only())} item(s)"
    )
    for record in status.items:
        if record.state is ItemState.VERIFIED:
            summary.append(f"{record.kind.value} {record.item}: verified")
        elif record.state is ItemState.FILLED:
            summary.append(f"{record.kind.value} {record.item}: filled (not yet verified)")
        elif record.state is ItemState.FAILED:
            summary.append(f"{record.kind.value} {record.item}: failed")
        elif record.state is ItemState.STAGED:
            summary.append(f"{record.kind.value} {record.item}: staged")
    open_decisions = list(decisions)
    pending = [r for r in status.items if r.state in (ItemState.STAGED, ItemState.FILLED)]
    failed = [r for r in status.items if r.state is ItemState.FAILED]
    if pending:
        open_decisions.append(f"{len(pending)} item(s) still in progress on instance {instance}")
    if failed:
        open_decisions.append(f"{len(failed)} item(s) failed on instance {instance}")
    return HandoffResult(
        instructions=tuple(steps),
        open_decisions=tuple(open_decisions),
        summary=tuple(summary),
        task=task,
    )


def reconcile(
    config_dir: Path,
    task: str,
    instance: str,
    *,
    kind: ItemKind,
    page_items: Sequence[str],
) -> ReconcileReport:
    """Compare verified/filled ledger items of ``kind`` with ids observed on the page."""
    status = instance_status(config_dir, task, instance)
    ledger_ids = {
        r.item
        for r in status.items
        if r.kind is kind and r.state in (ItemState.FILLED, ItemState.VERIFIED)
    }
    page_ids = {item.strip() for item in page_items if item and item.strip()}
    matched = tuple(sorted(ledger_ids & page_ids))
    only_ledger = tuple(sorted(ledger_ids - page_ids))
    only_page = tuple(sorted(page_ids - ledger_ids))
    return ReconcileReport(
        only_in_ledger=only_ledger,
        only_on_page=only_page,
        matched=matched,
        task=task,
        instance=instance,
    )


def stage_item(
    config_dir: Path,
    task: str,
    instance: str,
    kind: ItemKind,
    item: str,
    *,
    sha256: str | None = None,
    low_confidence: bool | None = None,
    model_version: int | None = None,
) -> ApplyResult:
    """Stage ``item`` unless it is already verified (then a no-op)."""
    status = instance_status(config_dir, task, instance)
    existing = _find(status, kind, item)
    if existing is not None and existing.state is ItemState.VERIFIED:
        return ApplyResult(item=existing, noop=True, status=status)
    record = record_item(
        config_dir,
        task,
        instance,
        kind,
        item,
        ItemState.STAGED,
        low_confidence=low_confidence,
        model_version=model_version,
        sha256=sha256 if kind is ItemKind.RECEIPT else None,
    )
    return ApplyResult(
        item=record,
        noop=False,
        status=instance_status(config_dir, task, instance),
    )


def verify_item(
    config_dir: Path,
    task: str,
    instance: str,
    kind: ItemKind,
    item: str,
    *,
    present_on_page: bool,
    sha256: str | None = None,
    model_version: int | None = None,
) -> ApplyResult:
    """Mark ``item`` verified when the page shows it; otherwise failed. Verified is a no-op."""
    status = instance_status(config_dir, task, instance)
    existing = _find(status, kind, item)
    if existing is not None and existing.state is ItemState.VERIFIED:
        return ApplyResult(item=existing, noop=True, status=status)
    if existing is None:
        raise LedgerError(
            f"Expected {kind.value} {item!r} to be staged or filled before verify, got none"
        )
    digest = sha256 if kind is ItemKind.RECEIPT else existing.sha256
    # Staged items may be verified in one call: filled first, then verified or failed.
    if existing.state is ItemState.STAGED and present_on_page:
        record_item(
            config_dir,
            task,
            instance,
            kind,
            item,
            ItemState.FILLED,
            model_version=model_version,
            sha256=digest,
        )
    state = ItemState.VERIFIED if present_on_page else ItemState.FAILED
    record = record_item(
        config_dir,
        task,
        instance,
        kind,
        item,
        state,
        model_version=model_version,
        sha256=digest,
    )
    return ApplyResult(
        item=record,
        noop=False,
        status=instance_status(config_dir, task, instance),
    )


def _default_run_step(task: str) -> StepRunner:
    from blumkin.browser import actions
    from blumkin.config import load_browser_setup

    config = load_browser_setup()

    def run(step: FlowStep, values: Mapping[str, str]) -> None:
        action = step.action.strip().lower()
        if action in {"click", "press"}:
            if not step.control:
                raise LedgerError(f"Flow step {action!r} needs a control")
            actions.click_task(config, task, step.control, role="button")
            return
        if action == "fill":
            if not step.control:
                raise LedgerError("Flow step 'fill' needs a control")
            key = step.control
            if key not in values:
                raise LedgerError(f"Expected a value for control {key!r}, got none")
            actions.fill_task(config, task, key, values[key])
            return
        if action == "goto":
            url = values.get("url") or values.get("page_url")
            if not url:
                raise LedgerError("Flow step 'goto' needs a url value")
            actions.goto_task(config, task, url)
            return
        if action == "upload":
            if not step.control:
                raise LedgerError("Flow step 'upload' needs a control")
            path = values.get("path") or values.get("file")
            if not path:
                raise LedgerError("Flow step 'upload' needs a path value")
            actions.upload_task(config, task, step.control, Path(path))
            return
        if action in {"expect", "check", "landmark"}:
            return
        raise LedgerError(f"Unsupported flow action {step.action!r}")

    return run


def _find(status: LedgerStatus, kind: ItemKind, item: str) -> ItemRecord | None:
    for record in status.items:
        if record.kind is kind and record.item == item:
            return record
    return None


def _require_flow(model: SiteModel, name: str) -> Flow:
    for flow in model.flows:
        if flow.name == name:
            return flow
    raise LedgerError(f"Expected a recorded flow named {name!r} for task {model.task!r}, got none")


def _status_payload(status: LedgerStatus) -> dict[str, Any]:
    return {
        "instance": status.instance,
        "phase": status.phase.value,
        "summary": status.summary(),
        "task": status.task,
    }


__all__ = [
    "ApplyResult",
    "ReconcileReport",
    "StepRunner",
    "apply_item",
    "handoff_from_ledger",
    "reconcile",
    "stage_item",
    "verify_item",
]
