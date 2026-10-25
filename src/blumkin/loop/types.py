"""Shared types for the loop/cron runner (issue #501)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class RunOutcome(StrEnum):
    """Final state of one trigger run."""

    DONE = "done"
    FAILED = "failed"
    NEEDS_USER = "needs_user"


class TriggerKind(StrEnum):
    """How a trigger is started (mail and schedule land in later issues)."""

    CRON = "cron"
    LOOP = "loop"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One journaled run: ids and outcome only, never bodies or amounts."""

    at: str
    instance: str
    outcome: RunOutcome
    reason: str
    run_id: str
    task: str
    trigger: str
    trigger_kind: TriggerKind

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "instance": self.instance,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "run_id": self.run_id,
            "task": self.task,
            "trigger": self.trigger,
            "trigger_kind": self.trigger_kind.value,
        }


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    """A named trigger from config.toml (`[loop.triggers.*]` or `[cron.*]`)."""

    instance: str
    kind: TriggerKind
    name: str
    task: str
    # Cron-only fields; unused by the runner core until issue #502.
    schedule: str | None = None
    timezone: str | None = None
    working_days_only: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instance": self.instance,
            "kind": self.kind.value,
            "name": self.name,
            "task": self.task,
        }
        if self.kind is TriggerKind.CRON:
            payload["schedule"] = self.schedule
            payload["timezone"] = self.timezone
            payload["working_days_only"] = self.working_days_only
        return payload
