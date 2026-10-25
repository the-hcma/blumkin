"""The docs only name real `blumkin browser` and `blumkin receipts` commands and flags (#472)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from cli_doc_commands import command_lines, unknown_parts

from blumkin.cli import main

_ROOT = Path(__file__).resolve().parents[1]
_GROUPS = ("browser", "receipts")
_README_GROUPS = frozenset(
    {"auth", "calendar", "mail", "chat", "browser", "docs", "meeting", "people", "receipts", "mcp"}
)
_ALLOWED_EXAMPLE_HOSTS = frozenset({"example.com", "erp.example.com", "playwright.dev"})
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
    assert _README_GROUPS <= set(rows)
    assert set(rows) <= set(main.commands)


def test_the_guide_and_security_page_state_the_no_submit_design_and_point_to_the_limits() -> None:
    guide = " ".join(_text("docs/browser-tasks.md").split())
    assert "has no submit command" in guide
    assert "the Limits section" in guide
    assert "never submits" not in guide
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
    hosts = {
        host.split(":")[0].rstrip(".,;)") for host in re.findall(r"https?://([^\s/)`'\"]+)", guide)
    }
    assert hosts <= _ALLOWED_EXAMPLE_HOSTS, hosts - _ALLOWED_EXAMPLE_HOSTS
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


def _flat(relative: str) -> str:
    return " ".join(_text(relative).split())


def test_the_guide_states_the_limits_of_the_guard_plainly() -> None:
    guide = _flat("docs/browser-tasks.md")
    assert "submitted by a GET request" in guide
    assert "A declared control that is not on the deny list is clicked" in guide
    assert "detective, not preventive" in guide
    assert "`goto` is not limited" in guide
    assert "no POST through" not in guide


def test_the_guide_describes_the_deny_rules_as_the_code_applies_them() -> None:
    guide = _flat("docs/browser-tasks.md")
    assert "an inline handler whose text mentions submit" in guide
    assert "any word in its name that starts with a finalizing word" in guide
    assert "even when it is listed in `allowed_controls`" in guide
    assert "Shift+Tab" in guide
    assert "every modifier combination" not in guide


def test_the_guide_lists_every_config_rule_the_loader_enforces() -> None:
    guide = _flat("docs/browser-tasks.md")
    assert "never `/`, the whole home directory or a path that climbs out of it" in guide
    assert "needs a dot after the star" in guide
    assert "public suffixes such as `co.uk` are not checked" in guide
    assert "`~user` paths are refused" in guide
    assert "a hard link is refused" in guide


def test_the_guide_narrows_what_is_checked_before_the_browser_is_touched() -> None:
    guide = _flat("docs/browser-tasks.md")
    assert "checks the file and the folder before the browser is touched" in guide
    assert "checks the page host once attached" in guide


def test_the_guide_documents_click_roles_and_the_ambiguous_errors() -> None:
    guide = _flat("docs/browser-tasks.md")
    assert "`--role link`" in guide
    assert "the task's browser is running or a launch for it is in progress" in guide
    assert "a file or folder the command named is missing" in guide


def test_the_security_page_and_agent_guide_do_not_overclaim() -> None:
    security = _flat("docs/SECURITY-AT-A-GLANCE.md")
    assert "A refused action never reaches the page" not in security
    assert "a GET request" in security
    assert "inline handlers whose text mentions submit" in security
    agent = _flat("docs/agent-integration.md")
    assert "nothing is sent; stop and hand over" not in agent
    assert "stop and hand over" in agent


def test_the_url_check_would_catch_a_host_that_only_starts_with_an_allowed_one() -> None:
    sample = "see https://example.com.evil.test/x and https://erp.example.com/ok"
    hosts = {
        host.split(":")[0].rstrip(".,;)") for host in re.findall(r"https?://([^\s/)`'\"]+)", sample)
    }
    assert hosts - _ALLOWED_EXAMPLE_HOSTS == {"example.com.evil.test"}


@pytest.mark.parametrize(
    "line",
    [
        "blumkin browser --not-a-flag",
        "blumkin --bogus browser status",
        "blumkin browser profile --nope list",
        "blumkin browser status --nope",
    ],
)
def test_the_command_checker_rejects_an_unknown_option_at_any_level(line: str) -> None:
    assert unknown_parts(line) != []


@pytest.mark.parametrize(
    "line",
    [
        "blumkin browser status --json",
        "blumkin --profile work browser status",
        "blumkin --json browser profile list",
        "blumkin browser handoff expense-report --filled 'one line' --step 'review' --step 'press'",
        "blumkin receipts find --dir ~/receipts --since 2026-10-01 --limit 20 --json",
    ],
)
def test_the_command_checker_accepts_real_options_and_their_values(line: str) -> None:
    assert unknown_parts(line) == []


def test_the_agent_guide_describes_the_interface_instead_of_claiming_impossibility() -> None:
    agent = " ".join(_text("docs/agent-integration.md").split())
    assert "A task has no submit command" in agent
    assert "A task **never submits**" not in agent


def test_the_security_page_limits_the_upload_and_receipt_guarantees_to_the_policy() -> None:
    security = " ".join(_text("docs/SECURITY-AT-A-GLANCE.md").split())
    assert "cannot make an agent upload a key or a token" not in security
    assert "outside the declared folders" in security
    assert "does not inspect file names or contents" in security
    assert "go only to declared sites" not in security
    assert "only as far as the upload check" in security


def test_the_operator_notes_list_every_browser_policy_key() -> None:
    notes = " ".join(_text("docs/operator-config.md").split())
    for key in ("allowed_controls", "allowed_hosts", "extra_deny_names", "upload_dirs"):
        assert f"`{key}`" in notes, key


def test_the_decision_record_does_not_title_the_design_as_a_guarantee() -> None:
    decisions = _text("docs/DECISIONS.md")
    assert "guarded and never submitting" not in decisions
