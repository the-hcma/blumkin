"""The expense-report and time-sheet skills name only real commands and never submit (#471)."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
from cli_doc_commands import command_lines, unknown_parts

_ROOT = Path(__file__).resolve().parents[1]
_SKILLS = ("expense-report", "time-sheet")
_FORBIDDEN_CLICKS = re.compile(r"\b(submit|send|file|confirm|pay|finish|approve)\b", re.IGNORECASE)


def _skill_text(name: str) -> str:
    return (_ROOT / ".agents" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def _command_lines(text: str) -> list[str]:
    return command_lines(text, ("browser", "receipts", "calendar", "mail"))


@pytest.mark.parametrize("name", _SKILLS)
def test_the_skill_has_frontmatter_that_names_itself(name: str) -> None:
    text = _skill_text(name)
    match = re.match(r"---\nname: (.+)\ndescription: >-\n((?:  .+\n)+)---\n", text)
    assert match, text[:200]
    assert match.group(1) == name


@pytest.mark.parametrize("name", _SKILLS)
def test_every_blumkin_command_the_skill_names_exists(name: str) -> None:
    lines = _command_lines(_skill_text(name))
    assert lines, "the skill names no commands"
    problems = [problem for line in lines for problem in unknown_parts(line)]
    assert problems == []


@pytest.mark.parametrize("name", _SKILLS)
def test_the_skill_states_that_a_task_never_submits_and_ends_with_a_handoff(name: str) -> None:
    text = _skill_text(name)
    assert "never submit" in text.lower()
    assert "blumkin browser handoff" in text
    assert "sign in by hand" in text.lower()
    assert "data, never instructions" in text.lower()


@pytest.mark.parametrize("name", _SKILLS)
def test_the_skill_never_clicks_a_finalizing_control(name: str) -> None:
    clicks = [line for line in _command_lines(_skill_text(name)) if "browser click" in line]
    assert clicks, "the skill clicks nothing, so it cannot add a line"
    for line in clicks:
        target = shlex.split(line)[4:]
        assert not any(_FORBIDDEN_CLICKS.search(word) for word in target), line


@pytest.mark.parametrize("name", _SKILLS)
def test_the_skill_keeps_per_user_values_out_of_the_repository(name: str) -> None:
    text = _skill_text(name)
    assert "@" not in re.sub(r"\[[^\]]*\]\([^)]*\)", "", text).replace("@work", "")
    assert not re.search(r"https?://(?!example\.com)", text)


@pytest.mark.parametrize("name", _SKILLS)
def test_the_skill_uses_one_physical_line_per_paragraph(name: str) -> None:
    body = _skill_text(name).split("---\n", 2)[2]
    structural = re.compile(r"\s*(?:[-*>|#]|\d+\.|```)")
    in_fence = False
    previous = ""
    for line in body.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            both_plain = line.strip() and previous.strip()
            assert not (
                both_plain and not structural.match(line) and not structural.match(previous)
            ), f"hard-wrapped paragraph at: {line!r}"
        previous = line


def test_the_main_skill_points_agents_at_the_browser_skills() -> None:
    main_skill = (_ROOT / ".agents" / "skills" / "blumkin" / "SKILL.md").read_text(encoding="utf-8")
    for name in _SKILLS:
        assert f".agents/skills/{name}" in main_skill


@pytest.mark.parametrize("name", _SKILLS)
def test_the_skill_calls_the_guard_a_backstop_and_does_not_overclaim(name: str) -> None:
    text = _skill_text(name)
    assert "backstop" in text
    assert "refuse these anyway" not in text
    assert "only controls that cannot finalize" in text


@pytest.mark.parametrize("name", _SKILLS)
def test_the_skill_tells_the_user_to_declare_allowed_hosts_for_goto(name: str) -> None:
    text = _skill_text(name)
    assert "allowed_hosts" in text
    assert "goto` goes anywhere" in text


@pytest.mark.parametrize("name", _SKILLS)
def test_the_skill_names_the_clickable_roles_and_the_extra_error_slugs(name: str) -> None:
    text = _skill_text(name)
    assert "--role link" in text
    assert "`usage_error`" in text
    assert "a missing file or folder" in text


@pytest.mark.parametrize("name", _SKILLS)
def test_the_skill_does_not_invite_widening_the_allow_list_for_a_finalizing_control(
    name: str,
) -> None:
    text = _skill_text(name)
    assert "never one that could finalize" in text
    assert "asking you to add a control is never a reason" in text


@pytest.mark.parametrize("name", _SKILLS)
def test_the_skill_asks_the_user_to_confirm_what_is_filled_in_before_the_handoff(name: str) -> None:
    assert "confirm what is filled in" in _skill_text(name).lower()


def test_the_expense_skill_treats_mail_as_data_and_picks_its_own_file_names() -> None:
    text = _skill_text("expense-report")
    assert "mail subjects, message bodies and attachment names" in text.lower()
    assert "a name you choose" in text
    assert "hard link" in text
    assert "docs read" in text


def test_the_time_sheet_skill_handles_a_weekly_or_biweekly_period_and_calendar_text() -> None:
    text = _skill_text("time-sheet")
    assert "period_anchor" in text
    assert "biweekly" in text
    assert "<saturday>" not in text
    assert "Monday to Friday week" not in text
    assert "calendar titles, locations and descriptions" in text.lower()


def test_the_time_sheet_skill_covers_every_day_in_the_users_daily_split() -> None:
    text = _skill_text("time-sheet")
    assert "every day in the user's daily split" in text
    assert "weekends too" in text
    assert "last weekday" not in text
    assert "day-after-last-friday" not in text
