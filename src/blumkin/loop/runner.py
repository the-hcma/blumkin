"""Execute one trigger run with budget, retries and journaling (issue #501)."""

from __future__ import annotations

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
from blumkin.loop.journal import append_run
from blumkin.loop.kill_switch import is_engaged
from blumkin.loop.locks import instance_lock
from blumkin.loop.state import is_paused
from blumkin.loop.types import RunOutcome, RunRecord, TriggerSpec


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
    handlers: HandlerMap | None = None,
    honor_pause: bool = True,
    sleep: Any = time.sleep,
) -> RunResult:
    """Run one named trigger once: serialize on the instance, budget, retry, journal."""
    trigger = setup.trigger(name)
    if is_engaged(setup.config_dir, setup.loop):
        return RunResult(record=None, skipped=True, skip_reason="kill_switch")
    if honor_pause and is_paused(setup.config_dir, name):
        return RunResult(record=None, skipped=True, skip_reason="paused")
    handler = resolve_handler(trigger.task, handlers)
    with instance_lock(setup.config_dir, trigger.task, trigger.instance):
        # Re-check after the lock: another caller may have paused or engaged the
        # kill switch while we waited for the same task instance.
        if is_engaged(setup.config_dir, setup.loop):
            return RunResult(record=None, skipped=True, skip_reason="kill_switch")
        if honor_pause and is_paused(setup.config_dir, name):
            return RunResult(record=None, skipped=True, skip_reason="paused")
        if handler is None:
            record = append_run(
                setup.config_dir,
                instance=trigger.instance,
                outcome=RunOutcome.NEEDS_USER,
                reason=f"no handler registered for task {trigger.task!r}",
                task=trigger.task,
                trigger=trigger.name,
                trigger_kind=trigger.kind,
            )
            return RunResult(record=record, skipped=False, skip_reason="")
        record = _execute(setup, trigger, handler, sleep=sleep)
    return RunResult(record=record, skipped=False, skip_reason="")


def _execute(
    setup: LoopSetup,
    trigger: TriggerSpec,
    handler: TaskHandler,
    *,
    sleep: Any,
) -> RunRecord:
    run_id = uuid.uuid4().hex
    deadline = time.monotonic() + setup.loop.run_budget_seconds
    attempts = setup.loop.max_retries + 1
    last = HandlerResult(outcome=RunOutcome.FAILED, reason="no attempts")
    for attempt in range(attempts):
        if is_engaged(setup.config_dir, setup.loop):
            return append_run(
                setup.config_dir,
                instance=trigger.instance,
                outcome=RunOutcome.FAILED,
                reason="kill_switch engaged during run",
                run_id=run_id,
                task=trigger.task,
                trigger=trigger.name,
                trigger_kind=trigger.kind,
            )
        if time.monotonic() >= deadline:
            return append_run(
                setup.config_dir,
                instance=trigger.instance,
                outcome=RunOutcome.FAILED,
                reason="run budget exhausted",
                run_id=run_id,
                task=trigger.task,
                trigger=trigger.name,
                trigger_kind=trigger.kind,
            )
        try:
            last = handler(trigger)
        except Exception as exc:  # noqa: BLE001 - bounded retry; journal the failure
            last = HandlerResult(outcome=RunOutcome.FAILED, reason=f"handler raised: {exc}")
        if last.outcome is not RunOutcome.FAILED:
            return append_run(
                setup.config_dir,
                instance=trigger.instance,
                outcome=last.outcome,
                reason=last.reason,
                run_id=run_id,
                task=trigger.task,
                trigger=trigger.name,
                trigger_kind=trigger.kind,
            )
        if attempt + 1 >= attempts:
            break
        backoff = setup.loop.retry_backoff_seconds
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sleep(min(backoff, remaining))
    return append_run(
        setup.config_dir,
        instance=trigger.instance,
        outcome=RunOutcome.FAILED,
        reason=last.reason,
        run_id=run_id,
        task=trigger.task,
        trigger=trigger.name,
        trigger_kind=trigger.kind,
    )
