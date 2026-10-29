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


def test_the_dev_skill_warns_that_a_launcher_on_path_shadows_the_installed_release() -> None:
    """A bare `blumkin` can silently be a checkout's launcher, not the release (#564)."""
    text = " ".join((_ROOT / ".agents/skills/blumkin-dev/SKILL.md").read_text().split())
    assert "If `scripts/dev` is on `PATH`" in text
    assert "a bare `blumkin` runs that launcher" in text
    assert "`+dev` marker" in text


def test_the_dev_skill_says_only_provider_bound_commands_need_a_config() -> None:
    text = " ".join((_ROOT / ".agents/skills/blumkin-dev/SKILL.md").read_text().split())
    assert "`profiles list`" in text
    assert "Only provider-bound commands, and the ones that create state" in text
    assert "under the config directory, need a config" in text
    assert "`browser`" in text and "`loop`" in text and "`receipts`" in text
    assert "`completion`" in text and "`mcp install`" in text


def test_the_onboarding_guide_no_longer_says_the_launcher_refuses_mcp_install() -> None:
    text = " ".join((_ROOT / "docs/agent-onboarding.md").read_text().split())
    assert "the launcher refuses `mcp install`" not in text
    assert "`mcp install` runs without a config" in text


def test_the_onboarding_troubleshooting_table_has_no_rows_for_fixed_problems() -> None:
    """These rows described bugs fixed in #564 and would now send an agent the wrong way."""
    text = " ".join((_ROOT / "docs/agent-onboarding.md").read_text().split())
    assert "attachment ids change on every fetch" not in text
    assert "`mail thread` fails with a 400 on Microsoft" not in text
    assert "`missing_scope` with a 403" not in text
    assert '`graph_error` with a 403 "has not been used in project"' in text
    # The prose elsewhere in the guide must agree with the table.
    assert "fails with `missing_scope` and a 403" not in text
    assert "fails with `graph_error` and a 403" in text


def test_the_onboarding_guide_tells_a_missing_config_from_an_empty_one() -> None:
    """`profiles list` runs without a config now, so `count: 0` alone cannot mean state B (#564)."""
    text = (_ROOT / "docs/agent-onboarding.md").read_text()
    assert 'The launcher exits 2 with "no config.toml in' not in text
    assert "launcher would otherwise stop at its guided-setup prompt" not in text
    rows = [line for line in text.splitlines() if line.startswith("| `") and "count" in line]
    no_config = next(line for line in rows if "no `config.toml`" in line.split("|")[1])
    assert "A. No config at all" in no_config
    empty = next(line for line in rows if "`config.toml` exists" in line.split("|")[1])
    assert "B. Config exists, no profiles" in empty


def test_the_readme_separates_draft_attachment_ids_from_listed_part_ids() -> None:
    text = " ".join((_ROOT / "README.md").read_text().split())
    assert "`attachments[].id` is `null`" in text
    assert (
        "`mail attachments` and `mail get` list Gmail attachments by their stable MIME part id"
        in text
    )
    # The guarantee holds only when Gmail supplies a part id.
    assert "when Gmail supplies a MIME part id" in text


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


def test_claude_code_can_discover_every_repo_skill() -> None:
    """Claude Code reads `.claude/skills/`; the skills live in `.agents/skills/` (#565)."""
    source = _ROOT / ".agents" / "skills"
    shims = _ROOT / ".claude" / "skills"
    names = sorted(path.name for path in source.iterdir() if (path / "SKILL.md").is_file())
    assert names, "no skills found"
    for name in names:
        shim = shims / name
        assert shim.is_symlink(), name
        assert shim.resolve() == (source / name).resolve(), name
        assert (shim / "SKILL.md").is_file(), name


def test_the_readme_explains_the_send_as_address_and_the_send_draft_ids() -> None:
    text = " ".join((_ROOT / "README.md").read_text().split())
    assert "default send-as address" in text
    assert (
        "`mail send-draft` returns `sent` (the draft id), `message_id` and `conversation_id`"
        in text
    )
