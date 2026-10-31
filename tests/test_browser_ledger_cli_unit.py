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


# --------------------------------------------------------------------------- report, close (#592)


def _begin_trip() -> None:
    assert _run("browser", "ledger", "begin", "expense-report", "trip").exit_code == EXIT_SUCCESS


def test_ledger_report_records_the_report_and_status_shows_it() -> None:
    _begin_trip()

    result = _run(
        "browser", "ledger", "report", "expense-report", "trip",
        "--report-id", "100234", "--draft-saved", "--total-confirmed",
    )  # fmt: skip

    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _json(_run("browser", "ledger", "status", "expense-report", "trip"))
    assert payload["report"]["report_id"] == "100234"
    assert payload["report"]["draft_saved"] is True
    assert payload["submitted"] is False
    assert "total" not in json.dumps(payload["report"]).replace("total_confirmed", "")


def test_ledger_status_lists_what_is_on_the_server_and_what_is_only_local() -> None:
    _begin_trip()
    for state in ("staged", "filled", "verified"):
        _run("browser", "ledger", "record", "expense-report", "trip", "receipt", "a", state,
             "--sha256", _SHA)  # fmt: skip
    _run("browser", "ledger", "record", "expense-report", "trip", "receipt", "b", "staged",
         "--sha256", _SHA)  # fmt: skip

    payload = _json(_run("browser", "ledger", "status", "expense-report", "trip"))
    text = _text("browser", "ledger", "status", "expense-report", "trip").output

    assert payload["on_server"] == ["a"]
    assert payload["local_only"] == ["b"]
    assert "on the server (read back): 1; only local or not read back: 1" in text


def test_ledger_close_needs_the_users_yes_and_an_outcome() -> None:
    _begin_trip()

    no_yes = _run("browser", "ledger", "close", "expense-report", "trip", "--outcome", "done")
    no_outcome = _run("browser", "ledger", "close", "expense-report", "trip", "--yes")

    assert no_yes.exit_code == EXIT_USAGE
    assert no_outcome.exit_code == EXIT_USAGE


def test_ledger_close_pending_records_the_answer_and_shows_how_to_resume() -> None:
    _begin_trip()
    _run(
        "browser", "ledger", "report", "expense-report", "trip", "--report-id", "7", "--draft-saved"
    )

    result = _run(
        "browser", "ledger", "close", "expense-report", "trip", "--outcome", "pending", "--yes"
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _json(result)
    assert payload["session"]["outcome"] == "pending"
    assert "reopen the saved report 7" in payload["resume"]
    assert payload["phase"] == "open"
    text = _text("browser", "ledger", "status", "expense-report", "trip").output
    assert "pending: reopen the saved report 7" in text


def test_ledger_close_records_the_answer_before_closing_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _begin_trip()
    monkeypatch.setattr("blumkin.browser.ledger_commands.require_profile", lambda *a, **k: None)
    order: list[str] = []

    def fake_close(config_dir: Path, task: str, **kwargs: Any) -> dict[str, Any]:
        status = _json(_run("browser", "ledger", "status", task, "trip"))
        order.append(f"closed after session={status['session']['outcome']}")
        return {"forced": False, "ok": True, "task": task, "was_running": True}

    monkeypatch.setattr("blumkin.browser.ledger_commands.close_task_browser", fake_close)

    result = _run(
        "browser", "ledger", "close", "expense-report", "trip",
        "--outcome", "done", "--close-browser", "--yes",
    )  # fmt: skip

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert order == ["closed after session=done"]
    assert _json(result)["browser_closed"] is True


def test_a_failed_browser_close_leaves_the_recorded_answer_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blumkin.browser.launcher import BrowserCloseError

    _begin_trip()
    monkeypatch.setattr("blumkin.browser.ledger_commands.require_profile", lambda *a, **k: None)

    def broken(config_dir: Path, task: str, **kwargs: Any) -> dict[str, Any]:
        raise BrowserCloseError("Expected the browser to close, got a hang")

    monkeypatch.setattr("blumkin.browser.ledger_commands.close_task_browser", broken)

    result = _run(
        "browser", "ledger", "close", "expense-report", "trip",
        "--outcome", "pending", "--close-browser", "--yes",
    )  # fmt: skip

    assert result.exit_code == EXIT_OTHER
    assert _json(result)["error"] == "browser_close_failed"
    assert "already recorded in the ledger" in _json(result)["message"]
    status = _json(_run("browser", "ledger", "status", "expense-report", "trip"))
    assert status["session"]["outcome"] == "pending"


def test_closing_the_session_never_submits_the_report() -> None:
    _begin_trip()
    _run("browser", "ledger", "close", "expense-report", "trip", "--outcome", "done", "--yes")

    status = _json(_run("browser", "ledger", "status", "expense-report", "trip"))

    assert status["submitted"] is False
    assert status["phase"] == "open"


def test_the_handoff_says_what_is_on_the_server_and_what_is_local() -> None:
    _begin_trip()
    for state in ("staged", "filled", "verified"):
        _run(
            "browser", "ledger", "record", "expense-report", "trip", "receipt", "a", state,
            "--sha256", _SHA,
        )  # fmt: skip
    _run(
        "browser", "ledger", "report", "expense-report", "trip", "--report-id", "7", "--draft-saved"
    )

    payload = _json(
        _run("browser", "ledger", "handoff", "expense-report", "trip", "--step", "Review it.")
    )

    summary = " ".join(payload["summary"])
    assert "report 7 saved as a draft" in summary
    assert "on the server (read back from the page): 1 item(s)" in summary


def test_closing_with_no_browser_running_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _begin_trip()
    monkeypatch.setattr("blumkin.browser.ledger_commands.require_profile", lambda *a, **k: None)
    monkeypatch.setattr(
        "blumkin.browser.ledger_commands.close_task_browser",
        lambda config_dir, task, **kwargs: {"forced": False, "ok": True, "was_running": False},
    )

    text = _text(
        "browser", "ledger", "close", "expense-report", "trip",
        "--outcome", "done", "--close-browser", "--yes",
    )  # fmt: skip

    assert text.exit_code == EXIT_SUCCESS, text.output
    assert "no task browser was running" in text.output


def test_done_with_only_local_items_warns_that_it_verified_nothing() -> None:
    _begin_trip()
    _run("browser", "ledger", "record", "expense-report", "trip", "receipt", "a", "staged",
         "--sha256", _SHA)  # fmt: skip

    result = _run(
        "browser", "ledger", "close", "expense-report", "trip", "--outcome", "done", "--yes"
    )

    assert "done does not verify them" in _json(result)["warnings"][0]


def test_a_task_without_a_profile_fails_before_the_answer_is_recorded() -> None:
    _begin_trip()

    result = _run(
        "browser", "ledger", "close", "expense-report", "trip",
        "--outcome", "pending", "--close-browser", "--yes",
    )  # fmt: skip

    assert result.exit_code == EXIT_NOT_FOUND
    assert _json(_run("browser", "ledger", "status", "expense-report", "trip"))["session"] is None
