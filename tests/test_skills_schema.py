"""Freeze the ``skills list --json`` contract published in docs/agent-integration.md.

Agents parse this output, so a drift here breaks sessions on machines we do not
control. Adding a field is fine; renaming or removing one is a version bump.
"""

from __future__ import annotations

import json
import re

from click.testing import CliRunner

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

_ARG_OPTIONAL_KEYS = {"multiple", "note", "values"}
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
_ID_RE = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*")
_SKILL_KEYS = {"args", "cli", "id", "mutates", "notifies_others", "scopes", "summary"}


def test_arg_objects_match_the_documented_shape() -> None:
    for skill in skills_catalog()["skills"]:
        for arg in skill["args"]:
            keys = set(arg)
            missing = _ARG_REQUIRED_KEYS - keys
            assert not missing, f"{skill['id']} {arg.get('name')}: missing {missing}"
            unknown = keys - _ARG_REQUIRED_KEYS - _ARG_OPTIONAL_KEYS
            assert not unknown, f"{skill['id']} {arg.get('name')}: undocumented keys {unknown}"
            # Options are "--flag"; positionals (skills.describe) carry a bare name.
            assert isinstance(arg["name"], str) and arg["name"]
            assert isinstance(arg["required"], bool)
            assert arg["type"] in _ARG_TYPES, f"{skill['id']}: undocumented type {arg['type']}"


def test_cli_emits_the_documented_envelope() -> None:
    """The docs quote CLI output, not the in-process catalog."""
    result = CliRunner().invoke(main, ["skills", "list", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    assert set(payload) == _ENVELOPE_KEYS
    assert payload["cli"] == "blumkin"
    assert payload["version"] == 1
    assert payload["skills"]


def test_describe_returns_a_bare_skill_object() -> None:
    result = CliRunner().invoke(main, ["skills", "describe", "calendar.today", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert set(json.loads(result.output)) == _SKILL_KEYS


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
        assert set(skill) == _SKILL_KEYS, skill.get("id")
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
