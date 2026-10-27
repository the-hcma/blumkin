"""Shared types for the loop/cron runner (issue #501)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class InstanceResolver(StrEnum):
    """How a trigger picks the task instance a run works on (issue #504)."""

    NEWEST_OPEN = "newest_open"
    STATIC = "static"
    SUBJECT_TRIP = "subject_trip"


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
class RunContext:
    """Per-run facts a handler may need beyond the trigger (issue #504).

    Feeders (the mail poller) fill `message_id`, `profile` and `subject`; the runner
    fills `config_dir` and the resolved `instance`. `subject` is untrusted data: it is
    matched against instance names and never executed or echoed into journals.
    """

    config_dir: Path | None = None
    # False when the mail feeder could not finish saving attachments for this message.
    intake_ok: bool = True
    instance: str = ""
    message_id: str | None = None
    profile: str | None = None
    subject: str | None = None


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
    # Loop/mail-only: sender allowlist and optional filters (issue #503).
    allowlist: tuple[str, ...] = ()
    attachment_types: tuple[str, ...] = ()
    # How the run's instance is chosen; cron triggers are always `static`. For the other
    # resolvers `instance` is "" in config and the runner resolves it per run (issue #504).
    instance_resolver: InstanceResolver = InstanceResolver.STATIC
    profiles: tuple[str, ...] = ()
    subject_tag: str | None = None
    # Cron-only: declared hours for the scheduled time-sheet entry (issue #502).
    default_hours: float | None = None
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
        if self.kind is TriggerKind.LOOP:
            payload["allowlist"] = list(self.allowlist)
            payload["attachment_types"] = list(self.attachment_types)
            payload["instance_resolver"] = self.instance_resolver.value
            payload["profiles"] = list(self.profiles)
            payload["subject_tag"] = self.subject_tag
        if self.kind is TriggerKind.CRON:
            payload["default_hours"] = self.default_hours
            payload["schedule"] = self.schedule
            payload["timezone"] = self.timezone
            payload["working_days_only"] = self.working_days_only
        return payload
