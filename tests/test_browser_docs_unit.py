"""The docs only name real `blumkin browser` and `blumkin receipts` commands and flags (#472)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from cli_doc_commands import command_lines, unknown_parts

from blumkin.cli import main

_ROOT = Path(__file__).resolve().parents[1]
_GROUPS = ("browser", "receipts")
_DOCS = [
    "README.md",
    "docs/agent-integration.md",
    "docs/browser-tasks.md",
    "docs/DECISIONS.md",
    "docs/operator-config.md",
    "docs/SECURITY-AT-A-GLANCE.md",
    ".agents/skills/blumkin/SKILL.md",
    ".agents/skills/expense-report/SKILL.md",
    ".agents/skills/time-sheet/SKILL.md",
]


def _text(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("relative", _DOCS)
def test_every_documented_browser_and_receipts_command_and_flag_exists(relative: str) -> None:
    problems: list[str] = []
    for line in command_lines(_text(relative), _GROUPS):
        problems.extend(unknown_parts(line))
    assert problems == []


def test_the_guide_documents_every_browser_and_receipts_command() -> None:
    guide = _text("docs/browser-tasks.md")
    for group in _GROUPS:
        commands = main.commands[group].commands  # type: ignore[attr-defined]
        for name, command in commands.items():
            if hasattr(command, "commands"):
                for sub in command.commands:  # type: ignore[attr-defined]
                    assert f"blumkin {group} {name} {sub}" in guide, f"{group} {name} {sub}"
            else:
                assert f"blumkin {group} {name}" in guide, f"{group} {name}"


def test_the_readme_table_lists_the_browser_and_receipts_groups() -> None:
    rows = re.findall(r"^\| `([a-z]+)`[^|]*\|", _text("README.md"), re.MULTILINE)
    assert "browser" in rows
    assert "receipts" in rows


def test_every_group_in_the_readme_table_is_a_real_command() -> None:
    rows = re.findall(r"^\| `([a-z]+)`[^|]*\|", _text("README.md"), re.MULTILINE)
    assert rows
    assert [row for row in rows if row not in main.commands] == []


def test_the_guide_and_security_page_state_the_never_submit_rule() -> None:
    assert "never submits" in _text("docs/browser-tasks.md")
    security = _text("docs/SECURITY-AT-A-GLANCE.md")
    assert "Browser-assisted tasks" in security
    assert "loopback" in security


def test_the_guide_error_table_covers_every_error_the_commands_emit() -> None:
    guide = _text("docs/browser-tasks.md")
    cli_source = _text("src/blumkin/cli.py")
    emitted = set(re.findall(r'slug, code = "([a-z_]+)"', cli_source))
    emitted -= {"usage_error"}
    for slug in emitted:
        assert f"`{slug}`" in guide, slug


def test_no_example_in_the_guide_uses_a_real_site_or_address() -> None:
    guide = _text("docs/browser-tasks.md")
    assert not re.search(r"https?://(?!example\.com|erp\.example\.com|playwright\.dev)", guide)
    assert "@" not in guide


def test_the_docs_state_the_shared_host_limit_and_the_safety_net() -> None:
    guide = _text("docs/browser-tasks.md")
    security = _text("docs/SECURITY-AT-A-GLANCE.md")
    assert "machine you alone use" in guide
    assert "other user accounts" in guide
    assert "swallows every non-GET page navigation" in guide
    assert "machine you alone use" in security
    assert "released when it detaches" in security


def test_the_docs_explain_upload_dirs_and_the_limit_of_the_safety_net() -> None:
    guide = _text("docs/browser-tasks.md")
    security = _text("docs/SECURITY-AT-A-GLANCE.md")
    assert "upload_dirs" in guide
    assert "never shorten it" in guide
    assert "protects only while a command is attached" in guide
    assert "Uploads are default-deny" in security
    assert "not a hard guarantee" in security


_POLICY_KEYS = ("allowed_controls", "allowed_hosts", "extra_deny_names", "upload_dirs")


def test_the_documented_task_policy_keys_are_exactly_the_ones_the_config_accepts() -> None:
    from blumkin.config import _TASK_POLICY_KEYS

    assert set(_POLICY_KEYS) == set(_TASK_POLICY_KEYS)
    guide = _text("docs/browser-tasks.md")
    for key in _POLICY_KEYS:
        assert f"`{key}`" in guide or f"{key} = " in guide, key
    for relative in ("docs/browser-tasks.md", ".agents/skills/expense-report/SKILL.md"):
        mentioned = set(re.findall(r"\b([a-z]+_(?:controls|hosts|dirs|names))\b", _text(relative)))
        assert mentioned <= set(_TASK_POLICY_KEYS), (relative, mentioned - set(_TASK_POLICY_KEYS))


def test_every_error_slug_the_skills_name_is_one_the_commands_emit() -> None:
    emitted = set(re.findall(r'slug, code = "([a-z_]+)"', _text("src/blumkin/cli.py")))
    emitted |= {"usage_error"}
    for relative in (
        ".agents/skills/expense-report/SKILL.md",
        ".agents/skills/time-sheet/SKILL.md",
    ):
        named = set(re.findall(r"- `([a-z_]+)`(?: and `([a-z_]+)`)?:", _text(relative)))
        slugs = {slug for pair in named for slug in pair if slug}
        assert slugs <= emitted, (relative, slugs - emitted)


def test_the_exit_codes_in_the_guide_match_the_ones_the_commands_use() -> None:
    cli_source = _text("src/blumkin/cli.py")
    codes = {
        slug: code
        for slug, code in re.findall(r'slug, code = "([a-z_]+)", (EXIT_[A-Z_]+)', cli_source)
    }
    expected = {"EXIT_OTHER": "1", "EXIT_NOT_FOUND": "5", "EXIT_USAGE": "2"}
    for slug, name in codes.items():
        row = re.search(rf"\| `{slug}` \| (\d) \|", _text("docs/browser-tasks.md"))
        assert row, slug
        assert row.group(1) == expected[name], (slug, row.group(1), name)
