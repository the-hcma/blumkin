"""Every --json stdout payload carries a top-level `ok` boolean (issue #99)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from dataclasses import replace

import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.config import load_config
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_SUCCESS, EXIT_USAGE
from blumkin.output import emit_json


def _enable_wo1162425(monkeypatch) -> None:
    monkeypatch.setattr(
        "blumkin.cli.load_config",
        lambda *, profile=None: replace(load_config(profile=profile), wo1162425_scopes=True),
    )


def _emit(payload: object) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        emit_json(payload)
    return json.loads(buf.getvalue())


def test_emit_json_injects_ok_true_for_a_bare_dict() -> None:
    assert _emit({"events": []}) == {"ok": True, "events": []}


def test_emit_json_keeps_an_explicit_ok() -> None:
    assert _emit({"ok": False, "problems": ["x"]})["ok"] is False
    assert _emit({"ok": True, "count": 2})["ok"] is True


def test_emit_json_leaves_non_dicts_alone() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        emit_json([1, 2, 3])
    assert json.loads(buf.getvalue()) == [1, 2, 3]


def _stdout_json(args: list[str]) -> tuple[int, dict]:
    result = CliRunner().invoke(main, args)
    return result.exit_code, json.loads(result.stdout)


def test_normal_read_reports_ok_true(monkeypatch) -> None:
    async def _events(**_kwargs):
        return {"date": "2026-09-01", "events": [], "timezone": "UTC"}

    monkeypatch.setattr("blumkin.providers.microsoft.calendar_today", _events)
    code, payload = _stdout_json(["calendar", "today", "--tz", "UTC", "--json"])
    assert code == EXIT_SUCCESS
    assert payload["ok"] is True


def test_people_resolve_ambiguous_reports_ok_false(monkeypatch) -> None:
    _enable_wo1162425(monkeypatch)

    async def _ambiguous(**_kwargs):
        return {"ambiguous": True, "candidates": [{"email": "a@x"}, {"email": "b@x"}]}

    monkeypatch.setattr("blumkin.providers.microsoft.people_resolve", _ambiguous)
    code, payload = _stdout_json(["people", "resolve", "--name", "Sam", "--json"])
    assert code == EXIT_USAGE
    assert payload["ok"] is False
    assert payload["ambiguous"] is True


def test_chat_last_no_match_reports_ok_false(monkeypatch) -> None:
    async def _no_match(**_kwargs):
        return {"chat": None, "items": [], "partial": False, "query": "nobody", "skipped": 0}

    monkeypatch.setattr("blumkin.providers.microsoft.chat_last", _no_match)
    code, payload = _stdout_json(["chat", "last", "--with", "nobody", "--json"])
    assert code == EXIT_NOT_FOUND
    assert payload["ok"] is False
    assert payload["chat"] is None


@pytest.mark.parametrize(
    "args",
    [
        ["skills", "list", "--json"],
        ["skills", "describe", "calendar.today", "--json"],
    ],
)
def test_discovery_commands_also_carry_ok(args: list[str]) -> None:
    result = CliRunner().invoke(main, args)
    assert result.exit_code == EXIT_SUCCESS
    assert json.loads(result.stdout)["ok"] is True
