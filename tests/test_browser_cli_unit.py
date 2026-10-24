"""`blumkin browser` commands: profiles, launch and status (issue #468)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.browser.discovery import BrowserKind, BrowserNotFoundError, ResolvedBrowser
from blumkin.browser.launcher import BrowserLaunchError, LaunchResult
from blumkin.browser.profiles import BrowserSession, create_profile, write_session
from blumkin.cli import main
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_OTHER, EXIT_SUCCESS, EXIT_USAGE

_CHROME = ResolvedBrowser(kind=BrowserKind.CHROME, path=Path("/opt/chrome"), fallback_from=None)
_SESSION = BrowserSession(
    browser="chrome", pid=os.getpid(), port=40123, started_at="2026-01-01T00:00:00Z"
)


@pytest.fixture(autouse=True)
def _live_pid_is_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sessions in these tests use this process's pid, whose command line is not a browser's."""
    monkeypatch.setattr(
        "blumkin.browser.profiles.process_is_browser_for",
        lambda pid, profile_dir, **kwargs: pid == os.getpid(),
    )


@pytest.fixture(autouse=True)
def _config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.default]\nclient_id = "abc-123"\n')
    return tmp_path


def _run(*args: str) -> Any:
    return CliRunner().invoke(main, ["--json", *args])


def _json(result: Any) -> dict[str, Any]:
    return json.loads(result.output)


def test_profile_create_makes_a_private_profile_and_reports_it(_config_dir: Path) -> None:
    result = _run("browser", "profile", "create", "expense-report")
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _json(result)
    assert payload["ok"] is True
    assert payload["task"] == "expense-report"
    assert payload["created"] is True
    assert payload["path"] == str(_config_dir / "browser-profiles" / "expense-report")


def test_profile_create_is_idempotent_and_says_so(_config_dir: Path) -> None:
    _run("browser", "profile", "create", "expense-report")
    payload = _json(_run("browser", "profile", "create", "expense-report"))
    assert payload["created"] is False


def test_profile_create_rejects_a_bad_name_as_a_usage_error() -> None:
    result = _run("browser", "profile", "create", "../x")
    assert result.exit_code == EXIT_USAGE
    payload = _json(result)
    assert payload["error"] == "usage_error"
    assert "Expected a task name" in payload["message"]


def test_profile_list_shows_each_profile_and_whether_its_browser_runs(_config_dir: Path) -> None:
    create_profile(_config_dir, "time-sheet")
    running = create_profile(_config_dir, "expense-report")
    write_session(running.path, _SESSION)
    payload = _json(_run("browser", "profile", "list"))
    assert [(p["task"], p["running"]) for p in payload["profiles"]] == [
        ("expense-report", True),
        ("time-sheet", False),
    ]


def test_profile_list_human_output_names_every_task(_config_dir: Path) -> None:
    create_profile(_config_dir, "expense-report")
    result = CliRunner().invoke(main, ["browser", "profile", "list"])
    assert result.exit_code == EXIT_SUCCESS
    assert "expense-report" in result.output


def test_profile_delete_needs_yes_and_says_what_it_will_lose(_config_dir: Path) -> None:
    profile = create_profile(_config_dir, "expense-report")
    result = _run("browser", "profile", "delete", "expense-report")
    assert result.exit_code == EXIT_USAGE
    assert "--yes" in _json(result)["message"]
    assert "sign-in" in _json(result)["message"]
    assert profile.path.exists()


def test_profile_delete_with_yes_removes_the_profile(_config_dir: Path) -> None:
    profile = create_profile(_config_dir, "expense-report")
    result = _run("browser", "profile", "delete", "expense-report", "--yes")
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _json(result)["deleted"] is True
    assert not profile.path.exists()


def test_profile_delete_of_an_unknown_task_is_not_found() -> None:
    result = _run("browser", "profile", "delete", "nope", "--yes")
    assert result.exit_code == EXIT_NOT_FOUND
    assert _json(result)["error"] == "not_found"


def test_profile_delete_refuses_while_the_browser_runs(_config_dir: Path) -> None:
    profile = create_profile(_config_dir, "expense-report")
    write_session(profile.path, _SESSION)
    result = _run("browser", "profile", "delete", "expense-report", "--yes")
    assert result.exit_code == EXIT_OTHER
    assert _json(result)["error"] == "profile_in_use"
    assert profile.path.exists()


def test_launch_needs_an_existing_profile_and_says_how_to_create_it() -> None:
    result = _run("browser", "launch", "expense-report")
    assert result.exit_code == EXIT_NOT_FOUND
    payload = _json(result)
    assert payload["error"] == "not_found"
    assert "blumkin browser profile create expense-report" in payload["hint"]


def test_launch_uses_the_configured_browser_and_reports_the_endpoint(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "expense-report")
    (_config_dir / "config.toml").write_text(
        '[browser]\ndefault = "edge"\n\n[profiles.default]\nclient_id = "abc-123"\n'
    )
    seen: dict[str, Any] = {}

    def fake_resolve(preferred: str | None) -> ResolvedBrowser:
        seen["preferred"] = preferred
        return _CHROME

    monkeypatch.setattr("blumkin.browser.commands.resolve_browser", fake_resolve)
    monkeypatch.setattr(
        "blumkin.browser.commands.launch",
        lambda profile, browser: LaunchResult(already_running=False, session=_SESSION),
    )

    result = _run("browser", "launch", "expense-report")

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert seen["preferred"] == "edge"
    payload = _json(result)
    assert payload["ok"] is True
    assert payload["browser"] == "chrome"
    assert payload["endpoint"] == "http://127.0.0.1:40123"
    assert payload["pid"] == os.getpid()
    assert payload["already_running"] is False
    assert "never sees your credentials" in payload["sign_in"]


def test_launch_human_output_tells_the_user_to_sign_in_by_hand(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "expense-report")
    monkeypatch.setattr("blumkin.browser.commands.resolve_browser", lambda preferred: _CHROME)
    monkeypatch.setattr(
        "blumkin.browser.commands.launch",
        lambda profile, browser: LaunchResult(already_running=False, session=_SESSION),
    )
    result = CliRunner().invoke(main, ["browser", "launch", "expense-report"])
    assert result.exit_code == EXIT_SUCCESS
    assert "Sign in by hand" in result.output
    assert "never sees your credentials" in result.output


def test_launch_reports_a_fallback_to_chrome(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "expense-report")
    fallback = ResolvedBrowser(
        kind=BrowserKind.CHROME, path=Path("/opt/chrome"), fallback_from=BrowserKind.EDGE
    )
    monkeypatch.setattr("blumkin.browser.commands.resolve_browser", lambda preferred: fallback)
    monkeypatch.setattr(
        "blumkin.browser.commands.launch",
        lambda profile, browser: LaunchResult(already_running=False, session=_SESSION),
    )
    payload = _json(_run("browser", "launch", "expense-report"))
    assert payload["fallback_from"] == "edge"


def test_launch_of_an_already_running_browser_says_so(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "expense-report")
    monkeypatch.setattr("blumkin.browser.commands.resolve_browser", lambda preferred: _CHROME)
    monkeypatch.setattr(
        "blumkin.browser.commands.launch",
        lambda profile, browser: LaunchResult(already_running=True, session=_SESSION),
    )
    assert _json(_run("browser", "launch", "expense-report"))["already_running"] is True


def test_launch_without_an_installed_browser_is_not_found(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "expense-report")

    def missing(preferred: str | None) -> ResolvedBrowser:
        raise BrowserNotFoundError("Expected Chrome to be installed, found none")

    monkeypatch.setattr("blumkin.browser.commands.resolve_browser", missing)
    result = _run("browser", "launch", "expense-report")
    assert result.exit_code == EXIT_NOT_FOUND
    assert _json(result)["error"] == "browser_not_found"


def test_launch_failure_is_reported_as_a_launch_error(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "expense-report")
    monkeypatch.setattr("blumkin.browser.commands.resolve_browser", lambda preferred: _CHROME)

    def broken(profile: Any, browser: Any) -> LaunchResult:
        raise BrowserLaunchError("Expected the browser to open a DevTools port, got none")

    monkeypatch.setattr("blumkin.browser.commands.launch", broken)
    result = _run("browser", "launch", "expense-report")
    assert result.exit_code == EXIT_OTHER
    assert _json(result)["error"] == "browser_launch_failed"


def test_status_lists_every_profile_with_its_endpoint_when_running(_config_dir: Path) -> None:
    running = create_profile(_config_dir, "expense-report")
    write_session(running.path, _SESSION)
    create_profile(_config_dir, "time-sheet")
    payload = _json(_run("browser", "status"))
    by_task = {p["task"]: p for p in payload["profiles"]}
    assert by_task["expense-report"]["running"] is True
    assert by_task["expense-report"]["endpoint"] == "http://127.0.0.1:40123"
    assert by_task["time-sheet"]["running"] is False
    assert by_task["time-sheet"]["endpoint"] is None


def test_status_for_one_task_and_an_unknown_one(_config_dir: Path) -> None:
    create_profile(_config_dir, "expense-report")
    one = _json(_run("browser", "status", "expense-report"))
    assert [p["task"] for p in one["profiles"]] == ["expense-report"]
    missing = _run("browser", "status", "nope")
    assert missing.exit_code == EXIT_NOT_FOUND


_EDGE_SESSION = BrowserSession(
    browser="edge", pid=os.getpid(), port=40124, started_at="2026-01-01T00:00:00Z"
)


def _fake_launch(
    monkeypatch: pytest.MonkeyPatch, *, result: LaunchResult, resolved: ResolvedBrowser
) -> None:
    monkeypatch.setattr("blumkin.browser.commands.resolve_browser", lambda preferred: resolved)
    monkeypatch.setattr("blumkin.browser.commands.launch", lambda profile, browser: result)


def test_launch_reports_the_browser_that_is_actually_running_not_the_configured_one(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "expense-report")
    _fake_launch(
        monkeypatch,
        result=LaunchResult(already_running=True, session=_EDGE_SESSION),
        resolved=_CHROME,
    )
    payload = _json(_run("browser", "launch", "expense-report"))
    assert payload["browser"] == "edge"
    assert payload["endpoint"] == "http://127.0.0.1:40124"
    assert payload["fallback_from"] is None


def test_a_fallback_is_reported_only_for_a_browser_that_was_just_started(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "expense-report")
    fallback = ResolvedBrowser(
        kind=BrowserKind.CHROME, path=Path("/opt/chrome"), fallback_from=BrowserKind.EDGE
    )
    _fake_launch(
        monkeypatch,
        result=LaunchResult(already_running=True, session=_SESSION),
        resolved=fallback,
    )
    assert _json(_run("browser", "launch", "expense-report"))["fallback_from"] is None


def test_launch_needs_no_account_profile_in_the_config(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "expense-report")
    (_config_dir / "config.toml").unlink()
    _fake_launch(
        monkeypatch,
        result=LaunchResult(already_running=False, session=_SESSION),
        resolved=_CHROME,
    )
    result = _run("browser", "launch", "expense-report")
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _json(result)["ok"] is True


def test_launch_does_not_ask_which_account_profile_to_use(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "expense-report")
    (_config_dir / "config.toml").write_text(
        '[browser]\ndefault = "edge"\n\n[profiles.one]\nclient_id = "a"\n\n'
        '[profiles.two]\nclient_id = "b"\n'
    )
    seen: dict[str, Any] = {}

    def fake_resolve(preferred: str | None) -> ResolvedBrowser:
        seen["preferred"] = preferred
        return _CHROME

    monkeypatch.setattr("blumkin.browser.commands.resolve_browser", fake_resolve)
    monkeypatch.setattr(
        "blumkin.browser.commands.launch",
        lambda profile, browser: LaunchResult(already_running=False, session=_SESSION),
    )
    result = _run("browser", "launch", "expense-report")
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert seen["preferred"] == "edge"


def test_every_profile_and_status_command_works_with_no_config_file(_config_dir: Path) -> None:
    (_config_dir / "config.toml").unlink()
    create_profile(_config_dir, "expense-report")
    assert _run("browser", "profile", "list").exit_code == EXIT_SUCCESS
    assert _run("browser", "status").exit_code == EXIT_SUCCESS
    assert _run("browser", "profile", "create", "time-sheet").exit_code == EXIT_SUCCESS


def test_a_broken_config_file_is_a_usage_error_not_a_traceback(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "expense-report")
    (_config_dir / "config.toml").write_text("[browser\ndefault = ")
    result = _run("browser", "launch", "expense-report")
    assert result.exit_code == EXIT_USAGE
    assert _json(result)["error"] == "usage_error"
    assert "config.toml" in _json(result)["message"]
