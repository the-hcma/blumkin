"""The expense-report and time-sheet skills name only real commands and never submit (#471)."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import click
import pytest

from blumkin.cli import main

_ROOT = Path(__file__).resolve().parents[1]
_SKILLS = ("expense-report", "time-sheet")
_FORBIDDEN_CLICKS = re.compile(r"\b(submit|send|file|confirm|pay|finish|approve)\b", re.IGNORECASE)


def _skill_text(name: str) -> str:
    return (_ROOT / ".agents" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def _command_lines(text: str) -> list[str]:
    """`blumkin ...` command lines from fenced code blocks and inline code spans."""
    lines: list[str] = []
    for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.DOTALL):
        lines.extend(
            line.strip() for line in block.splitlines() if line.strip().startswith("blumkin ")
        )
    lines.extend(re.findall(r"`(blumkin [^`]+)`", text))
    return lines


def _resolves(line: str) -> bool:
    """True when the words after `blumkin` walk down real click commands."""
    command: click.Command = main
    words = shlex.split(line.replace("\\\n", " "))[1:]
    seen_any = False
    for word in words:
        if word.startswith("-") or word.startswith("<") or word.startswith("$"):
            break
        if not isinstance(command, click.Group):
            break
        nxt = command.commands.get(word)
        if nxt is None:
            return seen_any and not isinstance(command, click.Group)
        command, seen_any = nxt, True
    return seen_any


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
    missing = [line for line in lines if not _resolves(line)]
    assert missing == []


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
