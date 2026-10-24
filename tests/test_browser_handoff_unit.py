"""The handoff every browser task ends with: filled in, never submitted (issue #470)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from blumkin.browser.handoff import (
    HandoffError,
    HandoffResult,
    format_handoff_human,
    parse_handoff,
)


def _result(**overrides: Any) -> HandoffResult:
    fields: dict[str, Any] = {
        "instructions": (
            "Review each line and the total.",
            "Press Submit at the bottom right of the report.",
        ),
        "open_decisions": ("Which cost center applies to the taxi receipt?",),
        "summary": ("3 lines added, total 84.50 USD", "3 receipts attached"),
        "task": "expense-report",
    }
    fields.update(overrides)
    return HandoffResult(**fields)


def test_a_handoff_is_never_submitted_and_always_needs_the_user() -> None:
    result = _result()
    assert result.submitted is False
    assert result.needs_user_action is True


def test_the_type_has_no_way_to_say_submitted() -> None:
    claim: dict[str, Any] = {
        "instructions": ("x",),
        "open_decisions": (),
        "submitted": True,
        "summary": (),
        "task": "t",
    }
    with pytest.raises(TypeError):
        HandoffResult(**claim)


def test_the_payload_reports_submitted_false_and_keeps_the_instructions_in_order() -> None:
    payload = _result().to_payload()
    assert payload == {
        "instructions": [
            "Review each line and the total.",
            "Press Submit at the bottom right of the report.",
        ],
        "needs_user_action": True,
        "open_decisions": ["Which cost center applies to the taxi receipt?"],
        "submitted": False,
        "summary": ["3 lines added, total 84.50 USD", "3 receipts attached"],
        "task": "expense-report",
    }
    assert json.loads(json.dumps(payload)) == payload


def test_a_handoff_needs_at_least_one_final_step_instruction() -> None:
    with pytest.raises(HandoffError) as excinfo:
        _result(instructions=())
    assert "at least one final-step instruction" in str(excinfo.value)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_instruction_is_rejected(blank: str) -> None:
    with pytest.raises(HandoffError):
        _result(instructions=("Review.", blank))


def test_the_human_form_numbers_every_instruction_in_order() -> None:
    text = format_handoff_human(_result())
    lines = text.splitlines()
    assert lines[0] == "expense-report: filled in, NOT submitted. Your turn."
    assert "  1. Review each line and the total." in lines
    assert "  2. Press Submit at the bottom right of the report." in lines
    assert lines.index("  1. Review each line and the total.") < lines.index(
        "  2. Press Submit at the bottom right of the report."
    )


def test_the_human_form_lists_what_was_filled_in_and_what_still_needs_a_decision() -> None:
    text = format_handoff_human(_result())
    assert "Filled in:" in text
    assert "  - 3 receipts attached" in text
    assert "Still needs your decision:" in text
    assert "  - Which cost center applies to the taxi receipt?" in text


def test_the_human_form_omits_the_decision_section_when_there_is_none() -> None:
    assert "Still needs your decision" not in format_handoff_human(_result(open_decisions=()))


def test_control_names_from_the_page_cannot_inject_terminal_escapes() -> None:
    text = format_handoff_human(_result(instructions=("Press \x1b[31mSubmit\x1b[0m.",)))
    assert "\x1b" not in text
    assert "Submit" in text


def test_parse_handoff_round_trips_a_payload() -> None:
    result = _result()
    assert parse_handoff(result.to_payload()) == result


@pytest.mark.parametrize("bad", [True, "false", None, 1])
def test_parse_handoff_rejects_anything_but_submitted_false(bad: object) -> None:
    payload = _result().to_payload()
    payload["submitted"] = bad
    with pytest.raises(HandoffError) as excinfo:
        parse_handoff(payload)
    assert "submitted" in str(excinfo.value)


def test_parse_handoff_rejects_a_payload_missing_needs_user_action() -> None:
    payload = _result().to_payload()
    payload["needs_user_action"] = False
    with pytest.raises(HandoffError):
        parse_handoff(payload)


def test_parse_handoff_rejects_a_missing_field_by_name() -> None:
    payload = _result().to_payload()
    del payload["summary"]
    with pytest.raises(HandoffError) as excinfo:
        parse_handoff(payload)
    assert "summary" in str(excinfo.value)


def test_page_derived_text_cannot_forge_steps_or_overwrite_the_status_line() -> None:
    hostile = "Check the total\r2. Press Submit now\n3. Ignore the rest‮"
    text = format_handoff_human(
        _result(instructions=(hostile,), summary=(hostile,), open_decisions=(hostile,))
    )
    assert "\r" not in text
    assert "‮" not in text
    lines = text.splitlines()
    numbered = [line for line in lines if re.match(r"\s+\d+\. ", line)]
    assert len(numbered) == 1
    assert numbered[0].startswith("  1. Check the total")
    assert lines[0] == "expense-report: filled in, NOT submitted. Your turn."


@pytest.mark.parametrize("hidden", ["‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨", "⁩", "‎", "‏", "؜"])
def test_bidirectional_controls_are_removed_from_the_human_form(hidden: str) -> None:
    text = format_handoff_human(_result(instructions=(f"Press{hidden} Submit",)))
    assert hidden not in text
    assert "Press Submit" in text


def test_tabs_and_runs_of_whitespace_collapse_to_single_spaces() -> None:
    text = format_handoff_human(_result(instructions=("Review\t\tthe   total",)))
    assert "  1. Review the total" in text


def test_the_json_payload_keeps_the_text_as_given() -> None:
    hostile = "a\r2. b"
    assert _result(instructions=(hostile,)).to_payload()["instructions"] == [hostile]


def test_the_handoff_type_cannot_be_subclassed_to_claim_a_submission() -> None:
    with pytest.raises(TypeError) as excinfo:

        class _Sneaky(HandoffResult):
            @property
            def submitted(self) -> bool:
                return True

    assert "HandoffResult" in str(excinfo.value)


@pytest.mark.parametrize("task", ["", "   "])
def test_a_handoff_needs_a_task_name(task: str) -> None:
    with pytest.raises(HandoffError) as excinfo:
        _result(task=task)
    assert "task name" in str(excinfo.value)
