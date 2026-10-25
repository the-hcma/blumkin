"""`blumkin browser ledger`: begin, record, status, list, submitted and discard (#488)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_OTHER, EXIT_SUCCESS, EXIT_USAGE

_SHA = "ab12cd34" * 8


@pytest.fixture(autouse=True)
def _config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config))
    (config / "config.toml").write_text('[profiles.default]\nclient_id = "abc-123"\n')
    return config


def _run(*args: str) -> Any:
    return CliRunner().invoke(main, ["--json", *args])


def _text(*args: str) -> Any:
    return CliRunner().invoke(main, list(args))


def _json(result: Any) -> dict[str, Any]:
    return json.loads(result.output)


def _begin(*extra: str) -> None:
    result = _run("browser", "ledger", "begin", "timesheet", "2026-p21", *extra)
    assert result.exit_code == EXIT_SUCCESS, result.output


def _file(day: str) -> None:
    for state in ("staged", "filled", "verified"):
        result = _run(
            "browser", "ledger", "record", "timesheet", "2026-p21", "day", day, state,
            "--model-version", "1",
        )  # fmt: skip
        assert result.exit_code == EXIT_SUCCESS, result.output


def test_beginning_and_checking_an_instance_reads_like_a_sentence() -> None:
    result = _text("browser", "ledger", "begin", "timesheet", "2026-p21", "--expect", "day=10")
    assert result.exit_code == EXIT_SUCCESS
    assert "0 of 10 days filed, not submitted" in result.output
    _file("2026-10-19")
    status = _text("browser", "ledger", "status", "timesheet", "2026-p21")
    assert "1 of 10 days filed, not submitted" in status.output


def test_the_status_payload_lists_counts_and_items() -> None:
    _begin("--expect", "day=10")
    _file("2026-10-19")
    result = _run(
        "browser", "ledger", "record", "timesheet", "2026-p21", "day", "2026-10-20", "staged"
    )
    assert _json(result)["state"] == "staged"
    payload = _json(_run("browser", "ledger", "status", "timesheet", "2026-p21"))
    assert payload["ok"] is True
    assert payload["phase"] == "open"
    assert payload["expected"] == {"day": 10}
    assert payload["counts"]["day"] == {"failed": 0, "filled": 0, "staged": 1, "verified": 1}
    assert [i["item"] for i in payload["items"]] == ["2026-10-19", "2026-10-20"]
    assert payload["items"][0]["model_version"] == 1
    assert payload["summary"] == "1 of 10 days filed, 1 in progress, not submitted"


def test_a_receipt_is_recorded_with_its_hash() -> None:
    _run("browser", "ledger", "begin", "expense-report", "trip-lisbon")
    result = _run(
        "browser", "ledger", "record", "expense-report", "trip-lisbon", "receipt", "r1", "staged",
        "--sha256", _SHA,
    )  # fmt: skip
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _json(result)["sha256"] == _SHA


def test_a_state_change_that_is_not_allowed_is_a_usage_error() -> None:
    _begin()
    result = _run("browser", "ledger", "record", "timesheet", "2026-p21", "day", "d1", "verified")
    assert result.exit_code == EXIT_USAGE
    assert _json(result)["error"] == "usage_error"


def test_instances_are_listed() -> None:
    _begin()
    assert _json(_run("browser", "ledger", "list", "timesheet"))["instances"] == [
        {"instance": "2026-p21", "phase": "open"}
    ]
    assert "2026-p21: open" in _text("browser", "ledger", "list", "timesheet").output
    assert "no instances" in _text("browser", "ledger", "list", "other-task").output


def test_marking_submitted_needs_yes_and_then_closes_the_instance() -> None:
    _begin()
    refused = _run("browser", "ledger", "submitted", "timesheet", "2026-p21")
    assert refused.exit_code == EXIT_USAGE
    done = _run("browser", "ledger", "submitted", "timesheet", "2026-p21", "--yes")
    assert done.exit_code == EXIT_SUCCESS
    assert _json(done)["phase"] == "submitted"
    late = _run("browser", "ledger", "record", "timesheet", "2026-p21", "day", "d1", "staged")
    assert late.exit_code == EXIT_USAGE


def test_discarding_needs_yes_and_keeps_the_journal(_config_dir: Path) -> None:
    _begin()
    assert _run("browser", "ledger", "discard", "timesheet", "2026-p21").exit_code == EXIT_USAGE
    done = _run("browser", "ledger", "discard", "timesheet", "2026-p21", "--yes")
    assert _json(done)["phase"] == "discarded"
    assert (_config_dir / "browser-tasks" / "timesheet" / "2026-p21" / "journal.jsonl").is_file()


def test_an_unknown_instance_is_not_found() -> None:
    result = _run("browser", "ledger", "status", "timesheet", "2026-p21")
    assert result.exit_code == EXIT_NOT_FOUND
    assert _json(result)["error"] == "not_found"


@pytest.mark.parametrize("bad", ["day", "day=0", "day=x", "week=2", "day=1001", "=3"])
def test_a_bad_expectation_is_a_usage_error(bad: str) -> None:
    result = _run("browser", "ledger", "begin", "timesheet", "2026-p21", "--expect", bad)
    assert result.exit_code == EXIT_USAGE


def test_a_damaged_journal_is_reported_not_guessed_at(_config_dir: Path) -> None:
    _begin()
    _file("2026-10-19")
    journal = _config_dir / "browser-tasks" / "timesheet" / "2026-p21" / "journal.jsonl"
    lines = journal.read_bytes().splitlines(keepends=True)
    lines[1] = b"garbage\n"
    journal.write_bytes(b"".join(lines))
    result = _run("browser", "ledger", "status", "timesheet", "2026-p21")
    assert result.exit_code == EXIT_OTHER
    assert _json(result)["error"] == "ledger_invalid"
