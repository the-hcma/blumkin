"""`blumkin browser` commands: profiles, launch and status (issue #468)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.browser.discovery import BrowserKind, BrowserNotFoundError, ResolvedBrowser
from blumkin.browser.launcher import (
    BrowserCloseError,
    BrowserLaunchError,
    CloseResult,
    LaunchResult,
)
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
def _one_open_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here talks to a real DevTools port: every browser reports one open page."""
    monkeypatch.setattr(
        "blumkin.browser.cdp._fetch_targets", lambda endpoint, timeout_s: [{"type": "page"}]
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


def test_launch_reports_a_running_browser_even_when_no_browser_is_installed(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "expense-report")
    write_session(_config_dir / "browser-profiles" / "expense-report", _SESSION)

    def missing(preferred: str | None) -> ResolvedBrowser:
        raise BrowserNotFoundError("Expected Chrome to be installed, found none")

    monkeypatch.setattr("blumkin.browser.commands.resolve_browser", missing)
    result = _run("browser", "launch", "expense-report")
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _json(result)
    assert payload["already_running"] is True
    assert payload["endpoint"] == _SESSION.endpoint
    assert payload["fallback_from"] is None


@pytest.mark.parametrize(
    "args",
    [
        ("browser", "launch", "Expense_Report"),
        ("browser", "status", "Expense_Report"),
        ("browser", "profile", "delete", "--yes", "Expense_Report"),
        ("browser", "launch", "../escape"),
    ],
    ids=["launch", "status", "delete", "launch-traversal"],
)
def test_an_invalid_task_name_is_a_usage_error_not_a_missing_profile(args: tuple[str, ...]) -> None:
    result = _run(*args)
    assert result.exit_code == EXIT_USAGE, result.output
    payload = _json(result)
    assert payload["error"] == "usage_error"
    assert "Create it first" not in json.dumps(payload)


def test_profile_list_with_an_unreadable_directory_reports_an_error_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable(config_dir: Path) -> list[Any]:
        raise PermissionError(
            "Expected a readable browser-profiles directory, got permission denied"
        )

    monkeypatch.setattr("blumkin.browser.commands.list_profiles", unreadable)
    result = _run("browser", "profile", "list")
    assert result.exit_code != EXIT_SUCCESS
    assert isinstance(result.exception, SystemExit)
    assert "error" in _json(result)


def test_profile_list_says_which_task_browser_is_running(_config_dir: Path) -> None:
    create_profile(_config_dir, "expense-report")
    create_profile(_config_dir, "time-sheet")
    write_session(_config_dir / "browser-profiles" / "expense-report", _SESSION)
    result = CliRunner().invoke(main, ["browser", "profile", "list"])
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert f"expense-report: running ({_SESSION.endpoint})" in result.output
    assert "time-sheet: not running" in result.output


def test_launch_with_an_invalid_browser_default_is_a_usage_error(_config_dir: Path) -> None:
    create_profile(_config_dir, "expense-report")
    (_config_dir / "config.toml").write_text(
        '[browser]\ndefault = "netscape"\n\n[profiles.default]\nclient_id = "abc-123"\n'
    )
    result = _run("browser", "launch", "expense-report")
    assert result.exit_code == EXIT_USAGE, result.output
    assert _json(result)["error"] == "usage_error"
    assert "browser.default" in _json(result)["message"]


# --------------------------------------------------------------------------- close (issue #574)


class _Closer:
    """Stands in for `close_browser`: records how it was called and returns or raises."""

    def __init__(self, result: CloseResult | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, profile: Any, **kwargs: Any) -> CloseResult:
        self.calls.append({"task": profile.task, **kwargs})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _closer(monkeypatch: pytest.MonkeyPatch, result: CloseResult | Exception) -> _Closer:
    closer = _Closer(result)
    monkeypatch.setattr("blumkin.browser.commands.close_browser", closer)
    return closer


def test_close_stops_the_task_browser_and_reports_it(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "youtube-transcript")
    closer = _closer(monkeypatch, CloseResult(forced=False, was_running=True))
    result = _run("browser", "close", "youtube-transcript")
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _json(result) == {
        "forced": False,
        "ok": True,
        "task": "youtube-transcript",
        "was_running": True,
    }
    assert closer.calls[0]["task"] == "youtube-transcript"
    assert closer.calls[0]["force"] is False


def test_close_human_output_says_the_profile_is_kept(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "youtube-transcript")
    _closer(monkeypatch, CloseResult(forced=False, was_running=True))
    out = CliRunner().invoke(main, ["browser", "close", "youtube-transcript"]).output
    assert "youtube-transcript: browser closed" in out and "profile kept" in out


def test_close_with_nothing_running_is_a_calm_no_op(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "youtube-transcript")
    _closer(monkeypatch, CloseResult(forced=False, was_running=False))
    result = _run("browser", "close", "youtube-transcript")
    assert result.exit_code == EXIT_SUCCESS
    assert _json(result)["was_running"] is False
    out = CliRunner().invoke(main, ["browser", "close", "youtube-transcript"]).output
    assert "no browser was running" in out


def test_close_passes_force_and_the_wait_through(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "youtube-transcript")
    closer = _closer(monkeypatch, CloseResult(forced=True, was_running=True))
    result = _run("browser", "close", "youtube-transcript", "--force", "--wait", "40")
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _json(result)["forced"] is True
    assert closer.calls[0]["force"] is True
    assert closer.calls[0]["timeout_s"] == 40.0


@pytest.mark.parametrize("bad", ["-1", "nan", "99999"])
def test_close_rejects_a_wait_that_is_not_a_sensible_number_of_seconds(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    create_profile(_config_dir, "youtube-transcript")
    closer = _closer(monkeypatch, CloseResult(forced=False, was_running=True))
    result = _run("browser", "close", "youtube-transcript", "--wait", bad)
    assert result.exit_code == EXIT_USAGE, result.output
    assert closer.calls == []


def test_close_of_an_unknown_task_is_not_found_and_says_how_to_create_it() -> None:
    result = _run("browser", "close", "nope")
    assert result.exit_code == EXIT_NOT_FOUND
    assert _json(result)["error"] == "not_found"
    assert "browser profile create" in _json(result)["hint"]


def test_close_rejects_a_bad_task_name_as_a_usage_error() -> None:
    result = _run("browser", "close", "../x")
    assert result.exit_code == EXIT_USAGE
    assert _json(result)["error"] == "usage_error"


def test_a_browser_that_will_not_stop_is_reported_with_the_force_hint(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_profile(_config_dir, "youtube-transcript")
    _closer(
        monkeypatch,
        BrowserCloseError("Expected the browser for task 'youtube-transcript' to close, got it"),
    )
    result = _run("browser", "close", "youtube-transcript")
    assert result.exit_code == EXIT_OTHER
    payload = _json(result)
    assert payload["error"] == "browser_close_failed"
    assert "--force" in payload["hint"]


def test_profile_delete_refusal_points_at_browser_close(_config_dir: Path) -> None:
    profile = create_profile(_config_dir, "expense-report")
    write_session(profile.path, _SESSION)
    result = _run("browser", "profile", "delete", "expense-report", "--yes")
    assert "browser close expense-report" in (_json(result).get("hint") or "")


def test_profile_delete_refused_for_a_launch_in_progress_does_not_say_to_close_a_browser(
    _config_dir: Path,
) -> None:
    from blumkin.browser.profiles import profile_lock

    profile = create_profile(_config_dir, "expense-report")
    with profile_lock(profile):
        result = _run("browser", "profile", "delete", "expense-report", "--yes")
    assert result.exit_code == EXIT_OTHER
    payload = _json(result)
    assert payload["error"] == "profile_in_use"
    assert "browser close" not in (payload.get("hint") or "")


def test_status_reports_the_open_page_count_and_says_nothing_when_there_is_one(
    _config_dir: Path,
) -> None:
    write_session(create_profile(_config_dir, "expense-report").path, _SESSION)

    entry = _json(_run("browser", "status", "expense-report"))["profiles"][0]

    assert entry["pages"] == 1
    assert entry["note"] is None


def test_status_says_so_when_the_browser_is_running_with_no_open_page(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("blumkin.browser.cdp._fetch_targets", lambda endpoint, timeout_s: [])
    write_session(create_profile(_config_dir, "expense-report").path, _SESSION)

    entry = _json(_run("browser", "status", "expense-report"))["profiles"][0]
    text = CliRunner().invoke(main, ["browser", "status", "expense-report"]).output

    assert entry["pages"] == 0
    assert "no open page" in entry["note"]
    assert "no open page" in text


def test_only_pages_count_as_open_pages(_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "blumkin.browser.cdp._fetch_targets",
        lambda endpoint, timeout_s: [
            {"type": "service_worker"},
            {"type": "page"},
            {"type": "iframe"},
        ],
    )
    write_session(create_profile(_config_dir, "expense-report").path, _SESSION)

    assert _json(_run("browser", "status", "expense-report"))["profiles"][0]["pages"] == 1


def test_status_leaves_the_page_count_unknown_when_devtools_does_not_answer(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(endpoint: str, timeout_s: float) -> list[dict[str, Any]]:
        raise OSError("connection refused")

    monkeypatch.setattr("blumkin.browser.cdp._fetch_targets", down)
    write_session(create_profile(_config_dir, "expense-report").path, _SESSION)

    entry = _json(_run("browser", "status", "expense-report"))["profiles"][0]

    assert entry["pages"] is None
    assert entry["note"] is None
    assert entry["running"] is True


def test_endpoint_prints_the_devtools_endpoint_of_the_running_browser(
    _config_dir: Path,
) -> None:
    write_session(create_profile(_config_dir, "expense-report").path, _SESSION)

    payload = _json(_run("browser", "endpoint", "expense-report"))
    text = CliRunner().invoke(main, ["browser", "endpoint", "expense-report"]).output

    assert payload["endpoint"] == "http://127.0.0.1:40123"
    assert payload["port"] == 40123
    assert text.strip() == "http://127.0.0.1:40123"


def test_endpoint_without_a_running_browser_says_to_launch_it(_config_dir: Path) -> None:
    create_profile(_config_dir, "expense-report")

    result = _run("browser", "endpoint", "expense-report")

    assert result.exit_code == EXIT_OTHER
    assert _json(result)["error"] == "browser_not_running"
    assert "browser launch expense-report" in _json(result)["hint"]


def test_endpoint_for_an_unknown_task_is_not_found() -> None:
    assert _run("browser", "endpoint", "nope").exit_code == EXIT_NOT_FOUND
