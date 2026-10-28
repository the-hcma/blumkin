"""The dev launcher skill and docs stay consistent with the script (issue #524)."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SKILL = _ROOT / ".agents" / "skills" / "blumkin-dev" / "SKILL.md"
_MAIN_SKILL = _ROOT / ".agents" / "skills" / "blumkin" / "SKILL.md"


def _frontmatter(text: str) -> dict[str, str]:
    block = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert block is not None
    fields: dict[str, str] = {}
    key = ""
    for line in block.group(1).splitlines():
        if re.match(r"^[a-z-]+:", line):
            key, _, value = line.partition(":")
            fields[key] = value.strip().lstrip(">-").strip()
        elif key:
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return fields


def test_the_dev_skill_has_a_name_a_trigger_and_one_job() -> None:
    fields = _frontmatter(_SKILL.read_text())
    assert fields["name"] == "blumkin-dev"
    description = fields["description"].lower()
    assert "only" in description and "checkout" in description
    assert "unreleased" in description or "dev version" in description


def test_the_dev_skill_tells_the_agent_to_use_the_launcher_and_report_the_version() -> None:
    body = _SKILL.read_text()
    assert "scripts/dev/blumkin" in body
    assert "--version" in body and "+dev." in body
    assert "report" in body.lower()
    assert "uv run blumkin" in body  # named as the thing not to do
    for rule in ("no-secret-exposure", "no-third-party-side-effects"):
        assert rule in body


def test_the_main_skill_points_to_the_dev_skill_so_only_one_fires() -> None:
    body = _MAIN_SKILL.read_text()
    assert "blumkin-dev" in body


def test_the_launcher_is_documented_where_people_look() -> None:
    for name in ("README.md", "AGENTS.md", "docs/agent-integration.md"):
        text = (_ROOT / name).read_text()
        assert "scripts/dev/blumkin" in text, name


def test_agents_md_still_bans_uv_run_blumkin_except_for_the_launcher() -> None:
    text = (_ROOT / "AGENTS.md").read_text()
    assert "not `uv run blumkin`" in text
    assert "scripts/dev/blumkin" in text
