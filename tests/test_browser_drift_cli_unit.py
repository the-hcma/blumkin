"""`blumkin browser model`: check a page against the stored model, diff versions (#487)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.browser.explore import Inspection, ModelDraft, parse_aria_snapshot
from blumkin.browser.model import save_model
from blumkin.browser.profiles import create_profile
from blumkin.cli import main
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_OTHER, EXIT_SUCCESS, EXIT_USAGE

_RECORDED = """\
- heading "Expenses" [level=1]
- link "Help"
- button "Add line"
- textbox "Amount"
- button "Submit report"
"""


class _Driver:
    def __init__(self) -> None:
        self.url = "https://erp.example.com/expenses"
        self.snapshot = _RECORDED
        self.closed = 0
        self.clicks: list[tuple[str, str]] = []

    def aria_snapshot(self) -> str:
        return self.snapshot

    def click_vetted(self, role: str, name: str, expected: Inspection) -> None:
        self.clicks.append((role, name))

    def inspect(self, role: str, name: str) -> Inspection:
        return Inspection(refusal=None, href="")

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch) -> _Driver:
    fake = _Driver()
    monkeypatch.setattr(
        "blumkin.browser.model_commands.attach_exploring", lambda profile, **kwargs: fake
    )
    return fake


@pytest.fixture(autouse=True)
def _config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config))
    (config / "config.toml").write_text(
        '[browser.tasks.expense-report]\nallowed_hosts = ["erp.example.com"]\n\n'
        '[profiles.default]\nclient_id = "abc-123"\n'
    )
    create_profile(config, "expense-report")
    return config


def _store(config: Path, snapshot: str = _RECORDED) -> None:
    draft = ModelDraft(task="expense-report", hosts=("erp.example.com",))
    draft.add_page(
        "expense-list", "https://erp.example.com/expenses", parse_aria_snapshot(snapshot)
    )
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")
    save_model(config, model)


def _run(*args: str) -> Any:
    return CliRunner().invoke(main, ["--json", *args])


def _json(result: Any) -> dict[str, Any]:
    return json.loads(result.output)


def test_an_unchanged_page_is_compatible(driver: _Driver, _config_dir: Path) -> None:
    _store(_config_dir)
    result = _run("browser", "model", "check", "expense-report", "expense-list")
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _json(result)
    assert payload["grade"] == "compatible"
    assert payload["findings"] == []
    assert payload["page"] == "expense-list"
    assert payload["version"] == 1
    assert payload["url_matches"] is True
    assert driver.closed == 1
    assert driver.clicks == []


def test_a_degraded_page_exits_zero_and_lists_what_to_update(
    driver: _Driver, _config_dir: Path
) -> None:
    _store(_config_dir)
    driver.snapshot = _RECORDED.replace('textbox "Amount"', 'combobox "Amount"')
    result = _run("browser", "model", "check", "expense-report", "expense-list")
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _json(result)
    assert payload["grade"] == "degraded"
    assert payload["findings"][0]["kind"] == "fallback"
    assert payload["ok"] is True


def test_an_incompatible_page_exits_nonzero_and_says_why(
    driver: _Driver, _config_dir: Path
) -> None:
    _store(_config_dir)
    driver.snapshot = _RECORDED.replace('button "Add line"', 'button "Add row"')
    result = _run("browser", "model", "check", "expense-report", "expense-list")
    assert result.exit_code == EXIT_OTHER
    payload = _json(result)
    assert payload["grade"] == "incompatible"
    assert payload["ok"] is False
    assert payload["findings"][0]["kind"] == "missing"
    assert payload["findings"][0]["control"] == "expense-list.add-line"
    assert "re-explore" in payload["findings"][0]["note"]


def test_a_page_at_another_address_is_reported(driver: _Driver, _config_dir: Path) -> None:
    _store(_config_dir)
    driver.url = "https://erp.example.com/somewhere/else"
    payload = _json(_run("browser", "model", "check", "expense-report", "expense-list"))
    assert payload["url_matches"] is False
    assert payload["url_template"] == "/somewhere/else"


def test_checking_refuses_a_page_off_the_allowed_hosts(driver: _Driver, _config_dir: Path) -> None:
    _store(_config_dir)
    driver.url = "https://evil.example.net/x"
    result = _run("browser", "model", "check", "expense-report", "expense-list")
    assert result.exit_code == EXIT_OTHER
    assert _json(result)["error"] == "explore_refused"


def test_checking_without_a_model_is_not_found(driver: _Driver) -> None:
    result = _run("browser", "model", "check", "expense-report", "expense-list")
    assert result.exit_code == EXIT_NOT_FOUND
    assert _json(result)["error"] == "not_found"


def test_checking_a_page_the_model_does_not_have_is_a_usage_error(
    driver: _Driver, _config_dir: Path
) -> None:
    _store(_config_dir)
    result = _run("browser", "model", "check", "expense-report", "nowhere")
    assert result.exit_code == EXIT_USAGE
    assert "expense-list" in _json(result)["message"]


def test_a_named_version_is_checked_instead_of_the_current_one(
    driver: _Driver, _config_dir: Path
) -> None:
    _store(_config_dir)
    _store(_config_dir, _RECORDED.replace('button "Add line"', 'button "Add row"'))
    current = _run("browser", "model", "check", "expense-report", "expense-list")
    assert current.exit_code == EXIT_OTHER
    first = _run("browser", "model", "check", "expense-report", "expense-list", "--version", "1")
    assert first.exit_code == EXIT_SUCCESS
    assert _json(first)["version"] == 1


def test_versions_lists_every_saved_version_and_marks_the_current_one(_config_dir: Path) -> None:
    _store(_config_dir)
    _store(_config_dir)
    payload = _json(_run("browser", "model", "versions", "expense-report"))
    assert payload["versions"] == [1, 2]
    assert payload["current"] == 2


def test_versions_of_an_unexplored_task_is_empty(_config_dir: Path) -> None:
    payload = _json(_run("browser", "model", "versions", "expense-report"))
    assert payload["versions"] == []
    assert payload["current"] is None


def test_diff_compares_two_versions_and_defaults_the_second_to_the_current(
    _config_dir: Path,
) -> None:
    _store(_config_dir)
    _store(_config_dir, _RECORDED.replace('button "Add line"', 'button "Add row"'))
    payload = _json(_run("browser", "model", "diff", "expense-report", "1"))
    assert payload["empty"] is False
    assert payload["controls_added"] == {"expense-list": ["expense-list.add-row"]}
    assert payload["controls_removed"] == {"expense-list": ["expense-list.add-line"]}
    same = _json(_run("browser", "model", "diff", "expense-report", "1", "1"))
    assert same["empty"] is True


def test_diff_of_a_missing_version_is_not_found(_config_dir: Path) -> None:
    _store(_config_dir)
    result = _run("browser", "model", "diff", "expense-report", "1", "9")
    assert result.exit_code == EXIT_NOT_FOUND


def test_the_human_output_is_readable(driver: _Driver, _config_dir: Path) -> None:
    _store(_config_dir)
    driver.snapshot = _RECORDED.replace('button "Add line"', 'button "Add row"')
    result = CliRunner().invoke(
        main, ["browser", "model", "check", "expense-report", "expense-list"]
    )
    assert result.exit_code == EXIT_OTHER
    assert "incompatible" in result.output
    assert "expense-list.add-line" in result.output


def test_a_page_at_another_address_is_at_most_degraded_even_when_every_control_is_there(
    driver: _Driver, _config_dir: Path
) -> None:
    _store(_config_dir)
    driver.url = "https://erp.example.com/somewhere/else"
    payload = _json(_run("browser", "model", "check", "expense-report", "expense-list"))
    assert payload["grade"] == "degraded"
    assert any(
        f["kind"] == "looks_different" and "address" in f["note"] for f in payload["findings"]
    )
    assert payload["ok"] is True


_THIN = """\
- heading "Expenses" [level=1]
- link "Help"
"""


def test_a_thin_page_at_another_address_is_incompatible(driver: _Driver, _config_dir: Path) -> None:
    _store(_config_dir, _THIN)
    driver.snapshot = _THIN
    driver.url = "https://erp.example.com/somewhere/else"
    result = _run("browser", "model", "check", "expense-report", "expense-list")
    payload = _json(result)
    assert payload["grade"] == "incompatible"
    assert payload["ok"] is False
    assert result.exit_code == EXIT_OTHER
