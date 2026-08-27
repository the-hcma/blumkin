"""Freeze the ``skills list --json`` contract published in docs/agent-integration.md.

Agents parse this output, so a drift here breaks sessions on machines we do not
control. Adding a field is fine; renaming or removing one is a version bump.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from click.testing import CliRunner

from blumkin import cli
from blumkin.cli import main
from blumkin.exit_codes import (
    EXIT_AUTH,
    EXIT_MISSING_SCOPE,
    EXIT_NOT_FOUND,
    EXIT_OTHER,
    EXIT_SUCCESS,
    EXIT_USAGE,
)
from blumkin.skills import skills_catalog

_ARG_REQUIRED_KEYS = {"name", "required", "type"}
_ARG_TYPES = {
    "date",
    "datetime",
    "duration",
    "email",
    "enum",
    "flag",
    "iana_tz",
    "int",
    "path",
    "string",
}
_ENVELOPE_KEYS = {"cli", "skills", "version"}
_ERROR_KEYS = {"error", "message", "ok"}
_ERROR_VALUES = {"auth_required", "graph_error", "missing_scope", "not_found", "usage_error"}
_ID_RE = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*")
_SKILL_KEYS = {"args", "cli", "id", "mutates", "notifies_others", "scopes", "summary"}


def test_arg_objects_match_the_documented_shape() -> None:
    for skill in skills_catalog()["skills"]:
        for arg in skill["args"]:
            missing = _ARG_REQUIRED_KEYS - set(arg)
            assert not missing, f"{skill['id']} {arg.get('name')}: missing {missing}"
            # Options are "--flag"; positionals (skills.describe) carry a bare name.
            assert isinstance(arg["name"], str) and arg["name"]
            assert isinstance(arg["required"], bool)
            assert arg["type"] in _ARG_TYPES, f"{skill['id']}: undocumented type {arg['type']}"


def test_cli_emits_the_documented_envelope() -> None:
    """The docs quote CLI output, not the in-process catalog."""
    result = CliRunner().invoke(main, ["skills", "list", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.stdout)
    # Superset, not equality: the contract permits adding fields within version 1.
    assert _ENVELOPE_KEYS <= set(payload)
    assert payload["cli"] == "blumkin"
    assert payload["version"] == 1
    assert payload["skills"]


def test_describe_returns_a_bare_skill_object() -> None:
    result = CliRunner().invoke(main, ["skills", "describe", "calendar.today", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert _SKILL_KEYS <= set(json.loads(result.stdout))


def test_error_envelope_goes_to_stderr_with_the_documented_fields() -> None:
    """Agents that parse only stdout see nothing on failure, so pin the stream."""
    result = CliRunner().invoke(main, ["skills", "describe", "nope", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert _ERROR_KEYS <= set(payload)
    assert payload["ok"] is False
    assert payload["error"] == "not_found"
    assert isinstance(payload["message"], str) and payload["message"]


def test_argument_errors_exit_usage_without_an_envelope() -> None:
    """Documented caveat: the parser rejects these before any envelope is emitted."""
    result = CliRunner().invoke(main, ["mail", "list", "--top", "notanint", "--json"])
    assert result.exit_code == EXIT_USAGE
    assert result.stderr.lstrip().startswith("Usage:")


def test_error_values_are_the_documented_ones() -> None:
    """Note these are not the exit-code names: exit 1 is graph_error, exit 2 usage_error."""
    source = (Path(cli.__file__)).read_text(encoding="utf-8")
    emitted = set(re.findall(r'emit_error\(\s*error="([a-z_]+)"', source))
    assert emitted == _ERROR_VALUES


def test_documented_sample_matches_real_output() -> None:
    """The guide quotes real output, so keep it from drifting away from the catalog."""
    doc = Path(__file__).resolve().parents[1] / "docs" / "agent-integration.md"
    block = re.search(r"```json\n(.*?)```", doc.read_text(encoding="utf-8"), re.S)
    assert block, "sample envelope missing from docs/agent-integration.md"
    sample = json.loads(block.group(1))

    live = skills_catalog()
    assert sample["cli"] == live["cli"]
    assert sample["version"] == live["version"]
    by_id = {skill["id"]: skill for skill in live["skills"]}
    for shown in sample["skills"]:
        assert shown == by_id[shown["id"]], f"docs sample for {shown['id']} is stale"


def test_enum_args_publish_their_values() -> None:
    for skill in skills_catalog()["skills"]:
        for arg in skill["args"]:
            if arg["type"] == "enum":
                values = arg.get("values")
                assert values, f"{skill['id']} {arg['name']}: enum without values"
                assert all(isinstance(v, str) for v in values)


def test_exit_codes_are_stable() -> None:
    assert (EXIT_SUCCESS, EXIT_OTHER, EXIT_USAGE) == (0, 1, 2)
    assert (EXIT_AUTH, EXIT_MISSING_SCOPE, EXIT_NOT_FOUND) == (3, 4, 5)


def test_notifying_skills_require_explicit_consent() -> None:
    """Anything that reaches another person must be gated behind --yes."""
    for skill in skills_catalog()["skills"]:
        if not skill["notifies_others"]:
            continue
        assert skill["mutates"], f"{skill['id']}: notifies others without mutating"
        consent = [a for a in skill["args"] if a["name"] == "--yes"]
        assert consent, f"{skill['id']}: notifies others with no --yes"
        assert consent[0]["required"], f"{skill['id']}: --yes is optional"


def test_skill_objects_match_the_documented_shape() -> None:
    for skill in skills_catalog()["skills"]:
        assert _SKILL_KEYS <= set(skill), skill.get("id")
        assert _ID_RE.fullmatch(skill["id"]), skill["id"]
        assert skill["cli"][0] == "blumkin"
        assert isinstance(skill["summary"], str) and skill["summary"]
        assert isinstance(skill["mutates"], bool)
        assert isinstance(skill["notifies_others"], bool)
        assert all(isinstance(scope, str) for scope in skill["scopes"])


def test_skills_are_sorted_by_id() -> None:
    ids = [skill["id"] for skill in skills_catalog()["skills"]]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
