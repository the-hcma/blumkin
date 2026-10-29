"""The project describes itself as Microsoft 365 and Google Workspace, work or personal (#533)."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from click.testing import CliRunner

from blumkin.cli import main

ROOT = Path(__file__).resolve().parent.parent
OLD_WORDING = re.compile(r"personal (microsoft 365|m365)", re.IGNORECASE)
SELF_DESCRIBING = (
    ".agents/skills/blumkin/SKILL.md",
    "AGENTS.md",
    "PLAN.md",
    "README.md",
    "SECURITY.md",
    "docs/agent-integration.md",
    "integrations/pi/package.json",
    "pyproject.toml",
    "src/blumkin/cli.py",
)


def _pyproject() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_package_description_covers_both_providers_and_drops_personal() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    description = project["description"]
    assert "Microsoft 365" in description and "Google Workspace" in description
    assert "Personal" not in description


def test_the_package_keywords_name_both_providers() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    keywords = set(project["keywords"])
    assert {"google-workspace", "microsoft-365"} <= keywords


def test_no_file_describes_blumkin_as_personal_microsoft_365() -> None:
    offenders = [
        relative
        for relative in SELF_DESCRIBING
        if OLD_WORDING.search((ROOT / relative).read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_skill_description_names_both_providers() -> None:
    text = (ROOT / ".agents/skills/blumkin/SKILL.md").read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    assert "Microsoft 365" in front and "Google" in front


def test_the_pi_package_description_names_both_providers() -> None:
    description = json.loads((ROOT / "integrations/pi/package.json").read_text("utf-8"))[
        "description"
    ]
    assert "Microsoft 365" in description and "Google" in description


def test_the_cli_help_does_not_call_blumkin_personal_microsoft_only() -> None:
    output = CliRunner().invoke(main, ["--help"]).output
    assert "Google Workspace" in output
    assert not OLD_WORDING.search(output)
