"""Execute one trigger run with budget, retries and journaling (issue #501)."""

from __future__ import annotations

import dataclasses
import time
import uuid
from dataclasses import dataclass
from typing import Any

from blumkin.loop.config import LoopSetup
from blumkin.loop.handlers import (
    HandlerMap,
    HandlerResult,
    TaskHandler,
    default_handlers,
    resolve_handler,
)
from blumkin.loop.instance_resolve import UNRESOLVED_INSTANCE, resolve_instance
from blumkin.loop.journal import append_run
from blumkin.loop.kill_switch import is_engaged
from blumkin.loop.locks import instance_lock
from blumkin.loop.notices import notify_run
from blumkin.loop.state import is_paused
from blumkin.loop.types import RunContext, RunOutcome, RunRecord, TriggerSpec


@dataclass(frozen=True, slots=True)
class RunResult:
    """Outcome of `run_trigger`, including whether a journal row was written."""

    record: RunRecord | None
    skipped: bool
    skip_reason: str

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
        }
        if self.record is not None:
            payload["run"] = self.record.as_dict()
        return payload


class LoopRunnerError(RuntimeError):
    """A run could not start for a structural reason (unknown trigger, etc.)."""


def handlers_or_default(handlers: HandlerMap | None) -> HandlerMap:
    return handlers if handlers is not None else default_handlers()


def run_trigger(
    setup: LoopSetup,
    name: str,
    *,
    context: RunContext | None = None,
    handlers: HandlerMap | None = None,
    honor_pause: bool = True,
    sleep: Any = time.sleep,
) -> RunResult:
    """Run one named trigger once: resolve the instance, serialize on it, budget, retry, journal.

    `context` carries what the feeder knew (mail message id, profile, subject). The
    instance is resolved before the lock (issue #504); when it cannot be, the run is
    journaled `needs_user` against the placeholder instance `unresolved`, without a lock.
    """
    trigger = setup.trigger(name)
    if is_engaged(setup.config_dir, setup.loop):
        return RunResult(record=None, skipped=True, skip_reason="kill_switch")
    if honor_pause and is_paused(setup.config_dir, name):
        return RunResult(record=None, skipped=True, skip_reason="paused")
    handler = resolve_handler(trigger.task, handlers)
    if handler is None:
        record = _journal(
            setup,
            trigger,
            instance=trigger.instance or UNRESOLVED_INSTANCE,
            outcome=RunOutcome.NEEDS_USER,
            reason=f"no handler registered for task {trigger.task!r}",
        )
        notify_run(record, trigger, fallback_to=setup.loop.notice_to)
        return RunResult(record=record, skipped=False, skip_reason="")
    resolution = resolve_instance(
        setup, trigger, subject=context.subject if context is not None else None
    )
    if resolution.instance is None:
        record = _journal(
            setup,
            trigger,
            instance=UNRESOLVED_INSTANCE,
            outcome=RunOutcome.NEEDS_USER,
            reason=f"instance not resolved: {resolution.reason}",
        )
        notify_run(record, trigger, fallback_to=setup.loop.notice_to)
        return RunResult(record=record, skipped=False, skip_reason="")
    instance = resolution.instance
    resolved = dataclasses.replace(trigger, instance=instance)
    run_context = dataclasses.replace(
        context if context is not None else RunContext(),
        config_dir=setup.config_dir,
        instance=instance,
    )
    record: RunRecord | None = None
    with instance_lock(setup.config_dir, trigger.task, instance):
        # Re-check after the lock: another caller may have paused or engaged the
        # kill switch while we waited for the same task instance.
        if is_engaged(setup.config_dir, setup.loop):
            return RunResult(record=None, skipped=True, skip_reason="kill_switch")
        if honor_pause and is_paused(setup.config_dir, name):
            return RunResult(record=None, skipped=True, skip_reason="paused")
        record = _execute(setup, resolved, handler, context=run_context, sleep=sleep)
    # Notices leave the instance lock so a hung Graph call cannot stall the next run.
    if record is not None:
        notify_run(record, trigger, fallback_to=setup.loop.notice_to)
    return RunResult(record=record, skipped=False, skip_reason="")


def _execute(
    setup: LoopSetup,
    trigger: TriggerSpec,
    handler: TaskHandler,
    *,
    context: RunContext,
    sleep: Any,
) -> RunRecord:
    run_id = uuid.uuid4().hex
    deadline = time.monotonic() + setup.loop.run_budget_seconds
    attempts = setup.loop.max_retries + 1
    last = HandlerResult(outcome=RunOutcome.FAILED, reason="no attempts")
    for attempt in range(attempts):
        if is_engaged(setup.config_dir, setup.loop):
            return _journal(
                setup,
                trigger,
                outcome=RunOutcome.FAILED,
                reason="kill_switch engaged during run",
                run_id=run_id,
            )
        if time.monotonic() >= deadline:
            return _journal(
                setup,
                trigger,
                outcome=RunOutcome.FAILED,
                reason="run budget exhausted",
                run_id=run_id,
            )
        try:
            last = handler(trigger, context=context)
        except Exception:  # noqa: BLE001 - bounded retry; journal the failure (no exc text)
            last = HandlerResult(outcome=RunOutcome.FAILED, reason="handler raised")
        if last.outcome is not RunOutcome.FAILED:
            return _journal(
                setup,
                trigger,
                outcome=last.outcome,
                reason=last.reason,
                run_id=run_id,
            )
        if attempt + 1 >= attempts:
            break
        backoff = setup.loop.retry_backoff_seconds
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sleep(min(backoff, remaining))
    return _journal(
        setup,
        trigger,
        outcome=RunOutcome.FAILED,
        reason=last.reason,
        run_id=run_id,
    )


def _journal(
    setup: LoopSetup,
    trigger: TriggerSpec,
    *,
    outcome: RunOutcome,
    reason: str,
    instance: str | None = None,
    run_id: str | None = None,
) -> RunRecord:
    return append_run(
        setup.config_dir,
        instance=instance if instance is not None else trigger.instance,
        outcome=outcome,
        reason=reason,
        run_id=run_id,
        task=trigger.task,
        trigger=trigger.name,
        trigger_kind=trigger.kind,
    )
