"""The handoff a browser task ends with: the form is filled in, never submitted (issue #470).

A finished task reports what it filled in, what still needs the user's decision, and the
numbered steps for the final click. `submitted` is not a field: the type can only say
`False`, so no task can report that it submitted, and `parse_handoff` rejects any payload
that claims otherwise.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from blumkin.output import sanitize_terminal


class HandoffError(ValueError):
    """A handoff is malformed or claims something a browser task can never do."""


@dataclass(frozen=True, slots=True)
class HandoffResult:
    instructions: tuple[str, ...]
    open_decisions: tuple[str, ...]
    summary: tuple[str, ...]
    task: str

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError(
            "Expected HandoffResult to stay as it is, got a subclass; "
            "a handoff must never be able to claim a submission"
        )

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise HandoffError("Expected a task name, got a blank one")
        if not self.instructions:
            raise HandoffError("Expected at least one final-step instruction, got none")
        for instruction in self.instructions:
            if not instruction.strip():
                raise HandoffError("Expected a non-blank final-step instruction, got a blank one")

    @property
    def needs_user_action(self) -> bool:
        return True

    @property
    def submitted(self) -> bool:
        return False

    def to_payload(self) -> dict[str, object]:
        return {
            "instructions": list(self.instructions),
            "needs_user_action": self.needs_user_action,
            "open_decisions": list(self.open_decisions),
            "submitted": self.submitted,
            "summary": list(self.summary),
            "task": self.task,
        }


def format_handoff_human(result: HandoffResult) -> str:
    """Render the handoff for a terminal; page-derived text is stripped of control chars."""
    lines = [f"{_one_line(result.task)}: filled in, NOT submitted. Your turn."]
    if result.summary:
        lines += ["", "Filled in:", *(f"  - {_one_line(item)}" for item in result.summary)]
    if result.open_decisions:
        lines += [
            "",
            "Still needs your decision:",
            *(f"  - {_one_line(item)}" for item in result.open_decisions),
        ]
    lines += [
        "",
        "To finish, do this yourself:",
        *(f"  {n}. {_one_line(step)}" for n, step in enumerate(result.instructions, 1)),
    ]
    return "\n".join(lines)


def parse_handoff(payload: Mapping[str, object]) -> HandoffResult:
    """Rebuild a handoff from its payload, refusing any that claims it was submitted."""
    if payload.get("submitted") is not False:
        raise HandoffError(
            f"Expected submitted false, got {payload.get('submitted')!r}; "
            "a browser task never submits"
        )
    if payload.get("needs_user_action") is not True:
        raise HandoffError(
            f"Expected needs_user_action true, got {payload.get('needs_user_action')!r}"
        )
    task = payload.get("task")
    if not isinstance(task, str) or not task:
        raise HandoffError(f"Expected a task name string in 'task', got {task!r}")
    return HandoffResult(
        instructions=_string_tuple(payload, "instructions"),
        open_decisions=_string_tuple(payload, "open_decisions"),
        summary=_string_tuple(payload, "summary"),
        task=task,
    )


_BIDI_CONTROLS = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def _one_line(text: str) -> str:
    """Make page-derived text safe to print as one line of a numbered list.

    Carriage returns and newlines would let it forge extra steps or overwrite the status
    line, and bidirectional controls would reorder what the user reads, so whitespace
    collapses to single spaces and those controls are removed before control characters
    are stripped.
    """
    return " ".join(sanitize_terminal(_BIDI_CONTROLS.sub("", text)).split())


def _string_tuple(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    if key not in payload:
        raise HandoffError(f"Expected the field {key!r}, got none")
    value = payload[key]
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise HandoffError(f"Expected {key!r} to be a list of strings, got {value!r}")
    return tuple(value)
