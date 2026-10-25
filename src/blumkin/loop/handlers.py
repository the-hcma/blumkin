"""Task handlers the runner invokes for a trigger (issue #501).

A trigger names a task; the task body lives here (or in later modules), not on
the trigger. Unattended runs must never prompt: anything that would ask returns
`needs_user` instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from blumkin.loop.types import RunOutcome, TriggerSpec


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """What a task handler returns after one attempt."""

    outcome: RunOutcome
    reason: str


class TaskHandler(Protocol):
    """A task the runner can invoke without a terminal."""

    def __call__(self, trigger: TriggerSpec) -> HandlerResult: ...


HandlerMap = Mapping[str, TaskHandler]


def default_handlers() -> dict[str, TaskHandler]:
    """Built-in handlers. Real browser tasks register in later issues."""
    return {"probe": _probe}


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


def _probe(trigger: TriggerSpec) -> HandlerResult:
    """A no-op task used to exercise the runner without a browser."""
    return HandlerResult(
        outcome=RunOutcome.DONE,
        reason=f"probe ok for instance {trigger.instance}",
    )
