"""Per-task browser profiles: names, modes, markers, sessions (issue #465)."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from blumkin.browser.profiles import (
    BrowserProfileError,
    BrowserSession,
    NotBlumkinProfileError,
    ProfileInUseError,
    create_profile,
    delete_profile,
    list_profiles,
    load_profile,
    process_is_browser_for,
    read_session,
    require_blumkin_profile,
    write_session,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize(
    "name",
    ["", "Expense", "a b", "../x", "a/b", "-lead", ".hidden", "x" * 65, "tilde~", "UP"],
)
def test_create_profile_rejects_names_that_could_escape_or_confuse(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(BrowserProfileError) as excinfo:
        create_profile(tmp_path, name)
    assert "Expected" in str(excinfo.value)
    assert not (tmp_path / "browser-profiles").exists() or not any(
        (tmp_path / "browser-profiles").iterdir()
    )


def test_create_profile_makes_a_private_directory_with_a_marker(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    assert profile.task == "expense-report"
    assert profile.path == tmp_path / "browser-profiles" / "expense-report"
    assert _mode(profile.path) == 0o700
    assert _mode(profile.path.parent) == 0o700
    assert require_blumkin_profile(profile.path) == profile


def test_create_profile_is_idempotent_for_a_profile_blumkin_made(tmp_path: Path) -> None:
    first = create_profile(tmp_path, "time-sheet")
    marker_before = (first.path / ".blumkin-profile.json").read_text(encoding="utf-8")
    second = create_profile(tmp_path, "time-sheet")
    assert second == first
    assert (first.path / ".blumkin-profile.json").read_text(encoding="utf-8") == marker_before


def test_create_profile_refuses_a_directory_blumkin_did_not_create(tmp_path: Path) -> None:
    foreign = tmp_path / "browser-profiles" / "expense-report"
    foreign.mkdir(parents=True)
    (foreign / "Cookies").write_text("not ours", encoding="utf-8")
    with pytest.raises(NotBlumkinProfileError):
        create_profile(tmp_path, "expense-report")
    assert (foreign / "Cookies").read_text(encoding="utf-8") == "not ours"


def test_create_profile_refuses_a_symlinked_profile_directory(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    root = tmp_path / "browser-profiles"
    root.mkdir(mode=0o700)
    (root / "expense-report").symlink_to(target)
    with pytest.raises(BrowserProfileError):
        create_profile(tmp_path, "expense-report")


def test_list_profiles_returns_only_marked_profiles_sorted(tmp_path: Path) -> None:
    create_profile(tmp_path, "time-sheet")
    create_profile(tmp_path, "expense-report")
    (tmp_path / "browser-profiles" / "stray").mkdir()
    assert [p.task for p in list_profiles(tmp_path)] == ["expense-report", "time-sheet"]


def test_list_profiles_is_empty_before_anything_was_created(tmp_path: Path) -> None:
    assert list_profiles(tmp_path) == []


def test_load_profile_returns_none_for_an_unknown_task(tmp_path: Path) -> None:
    assert load_profile(tmp_path, "nope") is None


def test_require_blumkin_profile_refuses_an_unmarked_directory(tmp_path: Path) -> None:
    with pytest.raises(NotBlumkinProfileError) as excinfo:
        require_blumkin_profile(tmp_path)
    assert str(tmp_path) in str(excinfo.value)


def test_session_round_trips_with_a_private_file(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    session = BrowserSession(
        browser="chrome", pid=os.getpid(), port=40123, started_at="2026-01-01T00:00:00Z"
    )
    write_session(profile.path, session)
    assert _mode(profile.path / ".blumkin-session.json") == 0o600
    assert read_session(profile.path, pid_alive=lambda pid: True) == session
    assert session.endpoint == "http://127.0.0.1:40123"


def test_read_session_ignores_a_session_whose_process_is_gone(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    write_session(
        profile.path,
        BrowserSession(browser="chrome", pid=2**22 + 12345, port=1, started_at="x"),
    )
    assert read_session(profile.path, pid_alive=lambda pid: False) is None


def test_read_session_ignores_a_corrupt_file(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    (profile.path / ".blumkin-session.json").write_text("{not json", encoding="utf-8")
    assert read_session(profile.path) is None


def test_delete_profile_removes_a_marked_profile(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    (profile.path / "Default").mkdir()
    delete_profile(tmp_path, "expense-report")
    assert not profile.path.exists()


def test_delete_profile_refuses_while_the_browser_is_running(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    write_session(
        profile.path,
        BrowserSession(browser="chrome", pid=os.getpid(), port=40123, started_at="x"),
    )
    with pytest.raises(ProfileInUseError):
        delete_profile(tmp_path, "expense-report", pid_alive=lambda pid: True)
    assert profile.path.exists()


def test_delete_profile_reports_an_unknown_task(tmp_path: Path) -> None:
    with pytest.raises(BrowserProfileError) as excinfo:
        delete_profile(tmp_path, "nope")
    assert "nope" in str(excinfo.value)


class _Ps:
    """A fake `subprocess.run` for `ps -ww -p PID -o command=`."""

    def __init__(
        self, stdout: str = "", returncode: int = 0, error: Exception | None = None
    ) -> None:
        self.args: list[str] = []
        self._result = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")
        self._error = error

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.args = args
        if self._error is not None:
            raise self._error
        return self._result


def test_a_process_whose_command_line_names_the_profile_is_the_browser(tmp_path: Path) -> None:
    ps = _Ps(stdout=f"/opt/chrome --user-data-dir={tmp_path} --remote-debugging-port=0\n")
    assert process_is_browser_for(os.getpid(), tmp_path, run=ps) is True
    assert ps.args[:2] == ["ps", "-ww"]
    assert "-o" in ps.args


def test_a_recycled_pid_running_something_else_is_not_the_browser(tmp_path: Path) -> None:
    ps = _Ps(stdout="/usr/sbin/cron -f\n")
    assert process_is_browser_for(os.getpid(), tmp_path, run=ps) is False


def test_a_pid_ps_does_not_know_is_not_the_browser(tmp_path: Path) -> None:
    assert process_is_browser_for(os.getpid(), tmp_path, run=_Ps(stdout="", returncode=1)) is False


def test_a_dead_pid_is_not_the_browser_and_ps_is_not_even_asked(tmp_path: Path) -> None:
    ps = _Ps(stdout=f"{tmp_path}")
    assert process_is_browser_for(2**22 + 4321, tmp_path, run=ps) is False
    assert ps.args == []


@pytest.mark.parametrize("error", [FileNotFoundError("ps"), subprocess.TimeoutExpired("ps", 5.0)])
def test_when_ps_cannot_answer_a_live_pid_is_assumed_to_be_the_browser(
    tmp_path: Path, error: Exception
) -> None:
    assert process_is_browser_for(os.getpid(), tmp_path, run=_Ps(error=error)) is True


def test_a_real_process_started_with_the_profile_path_is_recognised(tmp_path: Path) -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", f"--user-data-dir={tmp_path}"]
    )
    try:
        assert process_is_browser_for(child.pid, tmp_path) is True
        assert process_is_browser_for(child.pid, tmp_path / "other") is False
    finally:
        child.kill()
        child.wait(timeout=10)
