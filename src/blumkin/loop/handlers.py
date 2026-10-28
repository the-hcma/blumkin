"""Task handlers the runner invokes for a trigger (issue #501 / #502).

A trigger names a task; the task body lives here (or in later modules), not on
the trigger. Unattended runs must never prompt: anything that would ask returns
`needs_user` instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from blumkin.loop.types import AGENT_TASK, RunContext, RunOutcome, TriggerSpec


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """What a task handler returns after one attempt."""

    outcome: RunOutcome
    reason: str


class TaskHandler(Protocol):
    """A task the runner can invoke without a terminal."""

    def __call__(self, trigger: TriggerSpec, *, context: RunContext | None = None) -> HandlerResult:
        """Run once. `trigger.instance` is already resolved (issue #504); `context` carries
        the mail message, when there is one, and the config directory."""
        ...


HandlerMap = Mapping[str, TaskHandler]


def default_handlers() -> dict[str, TaskHandler]:
    """Built-in handlers. Browser ledger fill for time-sheet lands with #488/#489."""
    from blumkin.loop.agent_task import agent_task
    from blumkin.loop.expense_handler import expense_report

    return {
        "expense-report": expense_report,
        AGENT_TASK: agent_task,
        "probe": _probe,
        "time-sheet": _time_sheet,
    }


def register_handler(
    registry: dict[str, TaskHandler],
    task: str,
    handler: TaskHandler,
) -> None:
    """Add or replace a handler in a mutable registry (tests and later tasks)."""
    registry[task] = handler


def resolve_handler(task: str, handlers: HandlerMap | None = None) -> TaskHandler | None:
    """The handler for `task`, or None when none is registered."""
    registry = handlers if handlers is not None else default_handlers()
    return registry.get(task)


def _probe(trigger: TriggerSpec, *, context: RunContext | None = None) -> HandlerResult:
    """A no-op task used to exercise the runner without a browser."""
    return HandlerResult(
        outcome=RunOutcome.DONE,
        reason=f"probe ok for instance {trigger.instance}",
    )


def _time_sheet(trigger: TriggerSpec, *, context: RunContext | None = None) -> HandlerResult:
    """End-of-day time sheet: hours must be declared; never guess (issue #502).

    Filing into the browser form and ledger idempotence depend on #488/#489; until
    those land, a declared-hours run records readiness as `done` so the schedule
    catch-up advances, and a missing hours value becomes `needs_user`.
    """
    if trigger.default_hours is None:
        return HandlerResult(
            outcome=RunOutcome.NEEDS_USER,
            reason="no declared hours for the scheduled time-sheet entry",
        )
    return HandlerResult(
        outcome=RunOutcome.DONE,
        reason="declared hours ready for instance; browser ledger fill awaits #488",
    )
