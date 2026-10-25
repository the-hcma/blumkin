"""`blumkin browser explore`: observe, click, show, save and discard (#486)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.browser.profiles import create_profile
from blumkin.cli import main
from blumkin.exit_codes import EXIT_OTHER, EXIT_SUCCESS, EXIT_USAGE

_SNAPSHOT = """\
- heading "Expenses" [level=1]
- link "Help":
  - /url: /help
- button "Add line"
- button "Submit report"
- textbox "Amount"
- heading "Report 20261024123"
"""


class _Driver:
    def __init__(self) -> None:
        self.url = "https://erp.example.com/expenses"
        self.clicks: list[tuple[str, str]] = []
        self.closed = 0
        self.lands_on: str | None = None

    def aria_snapshot(self) -> str:
        return _SNAPSHOT

    def click_vetted(self, role: str, name: str) -> None:
        self.clicks.append((role, name))
        if self.lands_on is not None:
            self.url = self.lands_on

    def close(self) -> None:
        self.closed += 1


class _Confirmer:
    answers: list[bool] = []
    asked: list[str] = []
    unavailable = False

    def confirm(self, prompt: str) -> bool:
        type(self).asked.append(prompt)
        if not type(self).answers:
            type(self).unavailable = True
            return False
        return type(self).answers.pop(0)


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch) -> _Driver:
    fake = _Driver()
    monkeypatch.setattr("blumkin.browser.explore_commands.attach_exploring", lambda profile: fake)
    _Confirmer.answers = []
    _Confirmer.asked = []
    _Confirmer.unavailable = False
    monkeypatch.setattr("blumkin.browser.explore_commands.TerminalConfirmer", _Confirmer)
    return fake


@pytest.fixture(autouse=True)
def _config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config))
    _policy(config, 'allowed_hosts = ["erp.example.com"]')
    create_profile(config, "expense-report")
    return config


def _policy(config: Path, body: str) -> None:
    (config / "config.toml").write_text(
        f'[browser.tasks.expense-report]\n{body}\n\n[profiles.default]\nclient_id = "abc-123"\n'
    )


def _run(*args: str) -> Any:
    return CliRunner().invoke(main, ["--json", *args])


def _json(result: Any) -> dict[str, Any]:
    return json.loads(result.output)


def _draft_path(config: Path) -> Path:
    return config / "browser-models" / "expense-report" / "draft.json"


def test_observe_records_a_page_in_the_draft_and_reports_what_it_kept(
    driver: _Driver, _config_dir: Path
) -> None:
    result = _run("browser", "explore", "observe", "expense-report", "expense-list")
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _json(result)
    assert payload["page"] == "expense-list"
    assert payload["url_template"] == "/expenses"
    assert payload["skipped"] == 1
    assert payload["controls"] >= 3
    assert driver.closed == 1
    assert _draft_path(_config_dir).is_file()


def test_observe_refuses_a_task_without_allowed_hosts(driver: _Driver, _config_dir: Path) -> None:
    _policy(_config_dir, 'allowed_controls = ["Add line"]')
    result = _run("browser", "explore", "observe", "expense-report", "expense-list")
    assert result.exit_code == EXIT_OTHER
    payload = _json(result)
    assert payload["error"] == "explore_refused"
    assert "allowed_hosts" in payload["message"]
    assert not _draft_path(_config_dir).exists()


def test_observe_refuses_a_page_off_the_allowed_hosts(driver: _Driver, _config_dir: Path) -> None:
    driver.url = "https://evil.example.net/x"
    result = _run("browser", "explore", "observe", "expense-report", "expense-list")
    assert result.exit_code == EXIT_OTHER
    assert _json(result)["error"] == "explore_refused"
    assert not _draft_path(_config_dir).exists()


def test_observing_twice_updates_the_page_and_a_second_page_is_added(driver: _Driver) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    driver.url = "https://erp.example.com/expenses/42/line"
    _run("browser", "explore", "observe", "expense-report", "expense-form")
    shown = _json(_run("browser", "explore", "show", "expense-report"))
    assert [p["page_id"] for p in shown["pages"]] == ["expense-list", "expense-form"]
    assert shown["pages"][1]["url_template"] == "/expenses/{id}/line"


def test_a_bad_page_id_is_a_usage_error(driver: _Driver) -> None:
    result = _run("browser", "explore", "observe", "expense-report", "Page One")
    assert result.exit_code == EXIT_USAGE
    assert _json(result)["error"] == "usage_error"


def test_a_read_only_click_follows_a_link_without_asking(driver: _Driver) -> None:
    result = _run("browser", "explore", "click", "expense-report", "Help", "--role", "link")
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _json(result)
    assert payload["clicked"] is True
    assert payload["decision"] == "click"
    assert driver.clicks == [("link", "Help")]
    assert _Confirmer.asked == []


def test_a_state_changing_click_is_clicked_only_after_a_yes(driver: _Driver) -> None:
    _Confirmer.answers = [True]
    result = _run("browser", "explore", "click", "expense-report", "Add line")
    payload = _json(result)
    assert payload["clicked"] is True
    assert payload["decision"] == "ask"
    assert driver.clicks == [("button", "Add line")]
    assert "Add line" in _Confirmer.asked[0]


def test_a_no_answer_means_no_click(driver: _Driver) -> None:
    _Confirmer.answers = [False]
    result = _run("browser", "explore", "click", "expense-report", "Add line")
    payload = _json(result)
    assert result.exit_code == EXIT_SUCCESS
    assert payload["clicked"] is False
    assert driver.clicks == []


def test_without_a_terminal_the_control_is_not_clicked_and_the_user_gets_the_command(
    driver: _Driver,
) -> None:
    result = _run("browser", "explore", "click", "expense-report", "Add line")
    payload = _json(result)
    assert payload["clicked"] is False
    assert payload["terminal_unavailable"] is True
    assert "blumkin browser explore click expense-report" in payload["hint"]
    assert driver.clicks == []


def test_a_finalizing_control_is_never_clicked_or_asked_about(driver: _Driver) -> None:
    _Confirmer.answers = [True]
    result = _run("browser", "explore", "click", "expense-report", "Submit report")
    payload = _json(result)
    assert payload["clicked"] is False
    assert payload["decision"] == "record_only"
    assert driver.clicks == []
    assert _Confirmer.asked == []


def test_a_click_that_lands_off_the_allowed_hosts_is_reported_as_refused(driver: _Driver) -> None:
    driver.lands_on = "https://evil.example.net/x"
    result = _run("browser", "explore", "click", "expense-report", "Help", "--role", "link")
    assert result.exit_code == EXIT_OTHER
    payload = _json(result)
    assert payload["error"] == "explore_refused"
    assert "evil.example.net" in payload["message"]


def test_a_click_records_a_step_when_a_page_is_named(driver: _Driver) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    result = _run(
        "browser",
        "explore",
        "click",
        "expense-report",
        "Help",
        "--role",
        "link",
        "--page",
        "expense-list",
    )
    assert _json(result)["recorded_step"] is True
    shown = _json(_run("browser", "explore", "show", "expense-report"))
    assert shown["steps"] == 1


def test_save_needs_yes_then_stores_a_version_and_clears_the_draft(
    driver: _Driver, _config_dir: Path
) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    refused = _run("browser", "explore", "save", "expense-report")
    assert refused.exit_code == EXIT_USAGE
    assert "--yes" in _json(refused)["message"]
    saved = _run("browser", "explore", "save", "expense-report", "--yes")
    assert saved.exit_code == EXIT_SUCCESS, saved.output
    assert _json(saved)["version"] == 1
    assert not _draft_path(_config_dir).exists()
    assert (_config_dir / "browser-models" / "expense-report" / "model.json").is_file()


def test_save_without_a_draft_is_not_found(driver: _Driver) -> None:
    result = _run("browser", "explore", "save", "expense-report", "--yes")
    assert result.exit_code != EXIT_SUCCESS
    assert _json(result)["error"] == "not_found"


def test_discard_removes_the_draft(driver: _Driver, _config_dir: Path) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    assert _json(_run("browser", "explore", "discard", "expense-report"))["discarded"] is True
    assert _json(_run("browser", "explore", "discard", "expense-report"))["discarded"] is False
    assert not _draft_path(_config_dir).exists()


def test_show_without_a_draft_says_so(driver: _Driver) -> None:
    payload = _json(_run("browser", "explore", "show", "expense-report"))
    assert payload["pages"] == []


def test_an_unknown_task_is_not_found_and_a_bad_name_is_a_usage_error(driver: _Driver) -> None:
    assert _json(_run("browser", "explore", "show", "time-sheet"))["pages"] == []
    result = _run("browser", "explore", "observe", "time-sheet", "expense-list")
    assert result.exit_code != EXIT_SUCCESS
    assert _json(result)["error"] in {"not_found", "explore_refused"}
    bad = _run("browser", "explore", "observe", "Bad_Name", "expense-list")
    assert bad.exit_code == EXIT_USAGE


def test_the_human_output_names_the_controls_and_never_a_value(driver: _Driver) -> None:
    result = CliRunner().invoke(
        main, ["browser", "explore", "observe", "expense-report", "expense-list"]
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "expense-list" in result.output
    assert os.linesep in result.output
