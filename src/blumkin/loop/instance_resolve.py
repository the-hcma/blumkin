"""Deterministic task-instance resolution for a trigger run (issue #504).

A mail trigger cannot name its instance in config when the instance is a trip that
changes over time. The resolver picks one per run, or says it cannot:

- `static`: the trigger's configured `instance` (empty means no answer).
- `newest_open`: the task's only OPEN (not submitted, not discarded) instance. Several
  open instances are ambiguous, so the run needs the user rather than a guess about
  which trip a receipt belongs to.
- `subject_trip`: the OPEN instance whose name appears as a token in the mail subject
  (after removing the trigger's `subject_tag`). A token is a run of lowercase letters,
  digits, `-` or `_` (subjects are lowercased first), so `[expense] trip-lisbon taxi`
  names `trip-lisbon`. Exactly one distinct open instance name must appear.

Anything else is a `needs_user` outcome. The subject is untrusted data: it is only
compared with existing instance names, never executed, and never echoed into a reason.
Reasons carry counts, not names or subject text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from blumkin.browser.ledger import LedgerError, Phase, list_instances
from blumkin.browser.profiles import is_valid_task_name
from blumkin.loop.config import LoopSetup
from blumkin.loop.types import InstanceResolver, TriggerSpec

UNRESOLVED_INSTANCE = "unresolved"


@dataclass(frozen=True, slots=True)
class InstanceResolution:
    """The instance a run works on, or the reason none could be chosen."""

    instance: str | None
    reason: str

    @property
    def resolved(self) -> bool:
        return self.instance is not None


def resolve_instance(
    setup: LoopSetup,
    trigger: TriggerSpec,
    *,
    subject: str | None = None,
) -> InstanceResolution:
    """Pick the instance for one run of `trigger`; see the module docstring for the rules."""
    resolver = trigger.instance_resolver
    if resolver is InstanceResolver.STATIC:
        if trigger.instance:
            return InstanceResolution(instance=trigger.instance, reason="")
        return InstanceResolution(instance=None, reason="trigger has no configured instance")
    try:
        open_names = _open_instances(setup, trigger.task)
    except LedgerError:
        return InstanceResolution(instance=None, reason=f"{trigger.task} ledger is not readable")
    if resolver is InstanceResolver.NEWEST_OPEN:
        return _only_open(trigger.task, open_names)
    return _from_subject(trigger, open_names, subject)


_TOKEN_SPLIT = re.compile(r"[^a-z0-9_-]+")


def _from_subject(
    trigger: TriggerSpec,
    open_names: list[str],
    subject: str | None,
) -> InstanceResolution:
    if not subject or not subject.strip():
        return InstanceResolution(instance=None, reason="no mail subject to name a trip")
    if not open_names:
        return InstanceResolution(instance=None, reason=f"no open {trigger.task} instance")
    text = subject.lower()
    tag = (trigger.subject_tag or "").strip().lower()
    if tag:
        text = text.replace(tag, " ")
    tokens = {token for token in _TOKEN_SPLIT.split(text) if token}
    named = sorted(tokens.intersection(open_names))
    if len(named) == 1:
        return InstanceResolution(instance=named[0], reason="")
    if not named:
        return InstanceResolution(
            instance=None, reason=f"subject names none of {len(open_names)} open instance(s)"
        )
    return InstanceResolution(
        instance=None, reason=f"subject names {len(named)} open instances, expected one"
    )


def _only_open(task: str, open_names: list[str]) -> InstanceResolution:
    if len(open_names) == 1:
        return InstanceResolution(instance=open_names[0], reason="")
    if not open_names:
        return InstanceResolution(instance=None, reason=f"no open {task} instance")
    return InstanceResolution(
        instance=None,
        reason=f"{len(open_names)} open {task} instances, expected one",
    )


def _open_instances(setup: LoopSetup, task: str) -> list[str]:
    """Names of the task's OPEN instances, sorted; a task with no ledger has none."""
    if not is_valid_task_name(task):
        return []
    return [name for name, phase in list_instances(setup.config_dir, task) if phase is Phase.OPEN]
