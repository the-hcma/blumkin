"""Freeze the ``skills list --json`` contract published in docs/agent-integration.md.

Agents parse this output, so a drift here breaks sessions on machines we do not
control. Adding a field is fine; renaming or removing one is a version bump.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from click.testing import CliRunner

import blumkin
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
# Full (name, required, type) sequences for the skills the guide makes claims about:
# a positional name, command-line rather than sorted order, and published enum values.
_ARG_SIGNATURES = {
    "calendar.create": [
        ("--subject", True, "string"),
        ("--with", True, "email"),
        ("--start", True, "datetime"),
        ("--duration", False, "duration"),
        ("--teams", False, "flag"),
        ("--tz", False, "iana_tz"),
        ("--yes", True, "flag"),
    ],
    "mail.list": [
        ("--folder", False, "string"),
        ("--orderby", False, "enum"),
        ("--top", False, "int"),
    ],
    "skills.describe": [("skill-id", True, "string")],
}
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
# Every skill, classified by whether it reaches another person. This is exhaustive on
# purpose: a new skill fails the suite until someone records the decision here, so the
# most safety-critical field in the contract cannot be set by default or by accident.
# (An internal review gate, not a contract limit — v1 still permits adding skills.)
_NOTIFIES_OTHERS = {
    "auth.login": False,
    "auth.logout": False,
    "auth.status": False,
    "calendar.accept": True,
    "calendar.cancel": True,
    "calendar.create": True,
    "calendar.freebusy": False,
    "calendar.today": False,
    "calendar.view": False,
    "chat.attachments": False,
    "chat.attachments.download": False,
    "chat.delete": True,
    "chat.edit": True,
    "chat.find": False,
    "chat.last": False,
    "chat.send": True,
    "doctor": False,
    "mail.attachments": False,
    "mail.attachments.download": False,
    "mail.delete-draft": False,
    "mail.draft": False,
    "mail.folders": False,
    "mail.inbox": False,
    "mail.list": False,
    "mail.send-draft": True,
    "mail.update-draft": False,
    "meeting.get": False,
    "meeting.transcription": False,
    "skills.describe": False,
    "skills.list": False,
}
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
    assert _emitted_error_values() == _ERROR_VALUES


def test_error_values_reach_the_cli_verbatim() -> None:
    """Scanning source proves what is written; only running it proves what is emitted."""
    runner = CliRunner()
    for argv, expected in (
        (["skills", "describe", "nope", "--json"], "not_found"),
        (["calendar", "today", "--tz", "Not/AZone", "--json"], "usage_error"),
    ):
        result = runner.invoke(main, argv)
        assert json.loads(result.stderr)["error"] == expected, argv


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


def test_arg_signatures_are_pinned_for_documented_skills() -> None:
    """Shape checks alone would let a reorder or a renamed positional through."""
    by_id = {skill["id"]: skill for skill in skills_catalog()["skills"]}
    for skill_id, expected in _ARG_SIGNATURES.items():
        actual = [(a["name"], a["required"], a["type"]) for a in by_id[skill_id]["args"]]
        assert actual == expected, skill_id


def test_every_skill_is_classified_for_reaching_other_people() -> None:
    """Guard both directions, since either one silently defeats the safety rule.

    A skill losing the flag escapes the rule; a new skill that does reach people but
    ships with the flag unset never enters it. Requiring an explicit entry here means
    neither can happen without someone deciding.
    """
    actual = {s["id"]: s["notifies_others"] for s in skills_catalog()["skills"]}
    unclassified = set(actual) - set(_NOTIFIES_OTHERS)
    assert not unclassified, (
        f"classify these in _NOTIFIES_OTHERS — does the skill reach anyone else? {unclassified}"
    )
    dropped = set(_NOTIFIES_OTHERS) - set(actual)
    assert not dropped, f"released ids removed or renamed: {dropped}"
    assert actual == _NOTIFIES_OTHERS


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
    # v1 permits new skills, but an existing id may not be renamed or removed.
    assert _NOTIFIES_OTHERS.keys() <= set(ids), (
        f"missing released ids: {_NOTIFIES_OTHERS.keys() - set(ids)}"
    )


def _emitted_error_values() -> set[str]:
    """Collect every ``error=`` literal passed to ``emit_error`` across the package.

    Walking the AST rather than matching source text means keyword order and call
    formatting cannot hide a value, and a non-literal argument fails loudly instead
    of dropping out of the scan.
    """
    values: set[str] = set()
    for path in sorted(Path(blumkin.__file__).parent.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "emit_error":
                continue
            keyword = next((kw for kw in node.keywords if kw.arg == "error"), None)
            where = f"{path.name}:{node.lineno}"
            assert keyword is not None, f"{where}: emit_error without an error= keyword"
            literal = keyword.value
            assert isinstance(literal, ast.Constant) and isinstance(literal.value, str), (
                f"{where}: error= is not a string literal, so the documented set cannot be verified"
            )
            values.add(literal.value)
    return values
