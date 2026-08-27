"""Freeze the ``skills list --json`` contract published in docs/agent-integration.md.

Agents parse this output, so a drift here breaks sessions on machines we do not
control. Adding a field is fine; renaming or removing one is a version bump.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

import blumkin
from blumkin.cli import _raise_chat_attachment_error, _require_wo1162425_scopes, main
from blumkin.exit_codes import (
    EXIT_AUTH,
    EXIT_MISSING_SCOPE,
    EXIT_NOT_FOUND,
    EXIT_OTHER,
    EXIT_SUCCESS,
    EXIT_USAGE,
)
from blumkin.skills import skills_catalog
from blumkin.skills.chat import ChatAttachmentScopeError


def test_arg_objects_match_the_documented_shape() -> None:
    for skill in skills_catalog()["skills"]:
        for arg in skill["args"]:
            missing = _ARG_REQUIRED_KEYS - set(arg)
            assert not missing, f"{skill['id']} {arg.get('name')}: missing {missing}"
            # Options are "--flag"; positionals (skills.describe) carry a bare name.
            assert isinstance(arg["name"], str) and arg["name"]
            assert isinstance(arg["required"], bool)
            assert arg["type"] in _ARG_TYPES, f"{skill['id']}: undocumented type {arg['type']}"


def test_arg_signatures_are_pinned_for_documented_skills() -> None:
    """Shape checks alone would let a reorder or a renamed positional through."""
    by_id = {skill["id"]: skill for skill in skills_catalog()["skills"]}
    for skill_id, expected in _ARG_SIGNATURES.items():
        actual = [(a["name"], a["required"], a["type"]) for a in by_id[skill_id]["args"]]
        assert actual == expected, skill_id


def test_argument_errors_exit_usage_without_an_envelope() -> None:
    """Documented caveat: the parser rejects these before any envelope is emitted."""
    result = CliRunner().invoke(main, ["mail", "list", "--top", "notanint", "--json"])
    assert result.exit_code == EXIT_USAGE
    assert result.stderr.lstrip().startswith("Usage:")


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


def test_cli_invocation_follows_the_skill_id() -> None:
    """The documented rule: the command is the id, split on dots, after "blumkin".

    Pinning the relationship rather than 30 literal argv lists means a renamed
    subcommand fails even for skills added later.
    """
    for skill in skills_catalog()["skills"]:
        assert skill["cli"] == ["blumkin", *skill["id"].split(".")], skill["id"]


def test_config_opt_ins_do_not_share_an_exit_code(tmp_path, monkeypatch) -> None:
    """The docs distinguish them because the CLI does; keep that honest.

    wo1162425_scopes is refused up front as a usage error, while files_scopes
    surfaces as a missing scope from the download path. Point at an empty config
    dir and clear the env override, which wins over it, so the assertion holds
    regardless of the operator's own opt-ins.
    """
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_WO1162425_SCOPES", raising=False)
    scope_error = ChatAttachmentScopeError("needs Files.Read")
    with pytest.raises(SystemExit) as files_off:
        _raise_chat_attachment_error(scope_error, as_json=True)
    assert files_off.value.code == EXIT_MISSING_SCOPE

    with pytest.raises(SystemExit) as addon_off:
        _require_wo1162425_scopes(as_json=True)
    assert addon_off.value.code == EXIT_USAGE


def test_describe_returns_a_bare_skill_object() -> None:
    result = CliRunner().invoke(main, ["skills", "describe", "calendar.today", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert _SKILL_KEYS <= set(json.loads(result.stdout))


def test_diagnostic_commands_report_failure_on_stdout(tmp_path, monkeypatch) -> None:
    """The guide tells agents to fall back to stdout, so pin the commands that need it.

    doctor and chat last exit non-zero with their payload on stdout and an empty
    stderr, which is the opposite of the envelope contract everything else follows.
    """
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    runner = CliRunner()

    doctor = runner.invoke(main, ["doctor", "--json"])
    assert doctor.exit_code == EXIT_AUTH
    assert doctor.stderr == ""
    problems = json.loads(doctor.stdout)
    assert problems["ok"] is False
    # Exit 3 here is a missing client_id, which "run blumkin auth login" would not fix.
    assert any("client_id" in problem for problem in problems["problems"])

    async def _no_match(**_kwargs: object) -> dict[str, object]:
        return {"chat": None, "items": [], "partial": True, "query": "nobody", "skipped": 4}

    monkeypatch.setattr("blumkin.cli.chat_last", _no_match)
    chat = runner.invoke(main, ["chat", "last", "--with", "nobody", "--json"])
    assert chat.exit_code == EXIT_NOT_FOUND
    assert chat.stderr == ""
    assert json.loads(chat.stdout)["chat"] is None


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
    """Agents pass these strings verbatim, so a removed value breaks real commands."""
    published = {
        (skill["id"], arg["name"]): arg.get("values")
        for skill in skills_catalog()["skills"]
        for arg in skill["args"]
        if arg["type"] == "enum"
    }
    for key, values in published.items():
        assert values, f"{key}: enum without values"
        assert all(isinstance(v, str) for v in values)
    assert published == _ENUM_VALUES


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


def test_every_skill_is_classified_for_reaching_other_people() -> None:
    """Guard both directions, since either one silently defeats the safety rule.

    A skill losing the flag escapes the rule; a new skill that does reach people but
    ships with the flag unset never enters it. Requiring an explicit entry here means
    neither can happen without someone deciding. ``mutates`` rides along because
    agents use the pair to judge whether an action is safe to run unattended.
    """
    actual = {s["id"]: (s["mutates"], s["notifies_others"]) for s in skills_catalog()["skills"]}
    unclassified = set(actual) - set(_CONSENT)
    assert not unclassified, (
        f"classify these in _CONSENT — does the skill change anything or reach "
        f"anyone else? {unclassified}"
    )
    dropped = set(_CONSENT) - set(actual)
    assert not dropped, f"released ids removed or renamed: {dropped}"
    assert actual == _CONSENT


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
        # A value-taking --yes would let any string satisfy consent.
        assert consent[0]["type"] == "flag", f"{skill['id']}: --yes is not a flag"


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
    assert _CONSENT.keys() <= set(ids), f"missing released ids: {_CONSENT.keys() - set(ids)}"


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
        ("--from", False, "string"),
        ("--subject", False, "string"),
        ("--search", False, "string"),
        ("--since", False, "datetime"),
        ("--until", False, "datetime"),
        ("--unread", False, "flag"),
        ("--top", False, "int"),
        ("--tz", False, "iana_tz"),
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


_ENUM_VALUES = {
    ("mail.draft", "--body-type"): ["text", "html"],
    ("mail.forward", "--body-type"): ["html", "text"],
    ("mail.get", "--body-type"): ["html", "text"],
    ("mail.reply", "--body-type"): ["html", "text"],
    ("mail.list", "--orderby"): ["created", "received", "sent"],
    ("mail.update-draft", "--body-type"): ["text", "html"],
}


_ENVELOPE_KEYS = {"cli", "skills", "version"}


_ERROR_KEYS = {"error", "message", "ok"}


_ERROR_VALUES = {"auth_required", "graph_error", "missing_scope", "not_found", "usage_error"}


_ID_RE = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*")


# Every skill mapped to its consent metadata, (mutates, notifies_others). Exhaustive on
# purpose: a new skill fails the suite until someone records the decision here, so the
# fields agents use to decide whether an action is safe cannot be set by default or by
# accident. (A review gate, not a contract limit — v1 still permits adding skills.)
_CONSENT = {
    "auth.login": (True, False),
    "auth.logout": (True, False),
    "auth.status": (False, False),
    "calendar.accept": (True, True),
    "calendar.cancel": (True, True),
    "calendar.create": (True, True),
    "calendar.freebusy": (False, False),
    "calendar.today": (False, False),
    "calendar.view": (False, False),
    "chat.attachments": (False, False),
    "chat.attachments.download": (False, False),
    "chat.delete": (True, True),
    "chat.edit": (True, True),
    "chat.find": (False, False),
    "chat.last": (False, False),
    "chat.send": (True, True),
    "doctor": (False, False),
    "mail.attachments": (False, False),
    "mail.attachments.download": (False, False),
    "mail.delete-draft": (True, False),
    "mail.draft": (True, False),
    "mail.folders": (False, False),
    "mail.forward": (True, False),
    "mail.get": (False, False),
    "mail.inbox": (False, False),
    "mail.list": (False, False),
    "mail.reply": (True, False),
    "mail.send-draft": (True, True),
    "mail.update-draft": (True, False),
    "meeting.get": (False, False),
    "meeting.transcription": (True, False),
    "skills.describe": (False, False),
    "skills.list": (False, False),
}


_SKILL_KEYS = {"args", "cli", "id", "mutates", "notifies_others", "scopes", "summary"}


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
