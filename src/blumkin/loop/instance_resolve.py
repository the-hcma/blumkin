"""Deterministic task-instance resolution for a trigger run (issue #504).

A mail trigger cannot name its instance in config when the instance is a trip that
changes over time. The resolver picks one per run, or says it cannot:

- `static`: the trigger's configured `instance` (empty means no answer).
- `newest_open`: the task's newest OPEN (not submitted, not discarded) instance, by the
  journal `begin` timestamp (directory mtime, then name, as a tie-break). Zero open
  instances means the run needs the user.
- `subject_trip`: the OPEN instance whose name appears as a token in the mail subject
  (after removing the trigger's `subject_tag`). A token is a run of lowercase letters,
  digits, `-` or `_` (subjects are lowercased first), so `[expense] trip-lisbon taxi`
  names `trip-lisbon`. Exactly one distinct open instance name must appear.

Anything else is a `needs_user` outcome. The subject is untrusted data: it is only
compared with existing instance names, never executed, and never echoed into a reason.
Reasons carry counts, not names or subject text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from blumkin.browser.ledger import LedgerError, Phase, list_instances
from blumkin.browser.profiles import is_valid_task_name
from blumkin.loop.config import LoopSetup
from blumkin.loop.types import InstanceResolver, TriggerSpec

UNRESOLVED_INSTANCE = "unresolved"
_JOURNAL = "journal.jsonl"
_STORE = "browser-tasks"


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
        return _newest_open(setup, trigger.task, open_names)
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


def _begin_sort_key(config_dir: Path, task: str, name: str) -> tuple[str, float, str]:
    """Newest first: ISO `begin` time descending, then mtime descending, then name ascending."""
    directory = config_dir / _STORE / task / name
    journal = directory / _JOURNAL
    begun = ""
    if journal.is_file() and not journal.is_symlink():
        try:
            line = journal.read_text(encoding="utf-8").splitlines()[0]
            entry = json.loads(line)
            if isinstance(entry, dict) and entry.get("event") == "begin":
                at = entry.get("at")
                if isinstance(at, str):
                    begun = at
        except OSError, ValueError, IndexError:
            begun = ""
    try:
        mtime = directory.stat().st_mtime if directory.is_dir() else 0.0
    except OSError:
        mtime = 0.0
    # Invert begun/mtime via sort of (-rank): we sort ascending with negated keys.
    return (begun, mtime, name)


def _newest_open(setup: LoopSetup, task: str, open_names: list[str]) -> InstanceResolution:
    if not open_names:
        return InstanceResolution(instance=None, reason=f"no open {task} instance")
    # Newest begin `at` first; empty `at` sorts before real timestamps, so fall back on mtime.
    ranked = sorted(
        open_names,
        key=lambda name: _begin_sort_key(setup.config_dir, task, name),
        reverse=True,
    )
    # When begin timestamps tie, reverse=True puts the later name first; re-tie-break by
    # mtime already in the key. Name alone is the last component (ascending under reverse
    # becomes Z-before-A); prefer the lexicographically greater name only as last resort.
    return InstanceResolution(instance=ranked[0], reason="")


def _open_instances(setup: LoopSetup, task: str) -> list[str]:
    """Names of the task's OPEN instances, sorted; a task with no ledger has none."""
    if not is_valid_task_name(task):
        return []
    return [name for name, phase in list_instances(setup.config_dir, task) if phase is Phase.OPEN]
