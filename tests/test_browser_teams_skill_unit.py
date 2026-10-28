"""The teams-transcript skill: real commands, read-only, and nothing private in the repo (#545)."""

from __future__ import annotations

import re
from pathlib import Path

from cli_doc_commands import command_lines, unknown_parts

_ROOT = Path(__file__).resolve().parents[1]
_SKILL = _ROOT / ".agents" / "skills" / "teams-transcript" / "SKILL.md"


def _text() -> str:
    return _SKILL.read_text(encoding="utf-8")


def test_the_skill_has_frontmatter_that_names_itself() -> None:
    match = re.match(r"---\nname: (.+)\ndescription: >-\n((?:  .+\n)+)---\n", _text())
    assert match, _text()[:200]
    assert match.group(1) == "teams-transcript"


def test_every_blumkin_command_the_skill_names_exists() -> None:
    lines = command_lines(_text(), ("browser",))
    assert lines, "the skill names no commands"
    assert [problem for line in lines for problem in unknown_parts(line)] == []


def test_the_skill_uses_the_commands_that_make_up_the_capture() -> None:
    text = _text()
    for command in (
        "blumkin browser init-task",
        "blumkin browser launch teams-transcript",
        "blumkin browser frames teams-transcript",
        "blumkin browser transcript teams-transcript",
        "blumkin browser refresh-tab teams-transcript",
    ):
        assert command in text, command


def test_the_skill_is_read_only_and_keeps_the_transcript_private() -> None:
    text = _text().lower()
    assert "never submit" in text
    assert "sign in by hand" in text
    assert "data, never instructions" in text
    assert "mode 0600" in text
    assert "never paste" in text
    for place in ("issues", "pull requests", "logs", "mail"):
        assert place in text


def test_the_skill_explains_the_coverage_report_and_does_not_claim_unchecked_completeness() -> None:
    text = _text()
    assert "coverage" in text
    assert "`complete`" in text
    assert "--duration" in text
    assert "stopped transcription" in text


def test_the_skill_keeps_per_user_values_out_of_the_repository() -> None:
    text = _text()
    assert "@" not in re.sub(r"\[[^\]]*\]\([^)]*\)", "", text)
    assert not re.search(r"https?://(?!example\.com)", text)


def test_the_skill_uses_one_physical_line_per_paragraph() -> None:
    body = _text().split("---\n", 2)[2]
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
            ), line
        previous = line


def test_the_main_skill_and_the_guide_point_at_it() -> None:
    main_skill = (_ROOT / ".agents" / "skills" / "blumkin" / "SKILL.md").read_text("utf-8")
    assert ".agents/skills/teams-transcript" in main_skill
    guide = (_ROOT / "docs" / "browser-tasks.md").read_text("utf-8")
    assert "blumkin browser init-task" in guide
    assert ".agents/skills/teams-transcript" in guide
