"""Per-task browser profiles: names, modes, markers, sessions (issue #465)."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from blumkin.browser.profiles import (
    BrowserProfileError,
    BrowserSession,
    NotBlumkinProfileError,
    ProfileBusyError,
    ProfileInUseError,
    create_profile,
    delete_profile,
    list_profiles,
    load_profile,
    process_is_browser_for,
    profile_lock,
    read_session,
    require_blumkin_profile,
    running_browser_pid,
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


def test_a_profile_whose_path_is_a_prefix_of_another_is_not_matched(tmp_path: Path) -> None:
    profile = tmp_path / "a"
    other = f"{tmp_path}/ab"
    ps = _Ps(stdout=f"/opt/chrome --user-data-dir={other} --remote-debugging-port=0\n")
    assert process_is_browser_for(os.getpid(), profile, run=ps) is False


def test_the_profile_path_must_be_the_user_data_dir_argument_not_just_mentioned(
    tmp_path: Path,
) -> None:
    ps = _Ps(stdout=f"tail -f {tmp_path}/Default/log\n")
    assert process_is_browser_for(os.getpid(), tmp_path, run=ps) is False


def test_a_profile_path_with_spaces_matches_up_to_the_next_flag(tmp_path: Path) -> None:
    spaced = tmp_path / "with space"
    ps = _Ps(stdout=f"/opt/chrome --user-data-dir={spaced} --remote-debugging-port=0\n")
    assert process_is_browser_for(os.getpid(), spaced, run=ps) is True
    ps_end = _Ps(stdout=f"/opt/chrome --user-data-dir={spaced}\n")
    assert process_is_browser_for(os.getpid(), spaced, run=ps_end) is True


@pytest.mark.parametrize(
    ("pid", "port"), [(0, 40123), (1, 40123), (-5, 40123), (4242, 0), (4242, 70000)]
)
def test_read_session_ignores_a_session_with_an_impossible_pid_or_port(
    tmp_path: Path, pid: int, port: int
) -> None:
    profile = create_profile(tmp_path, "expense-report")
    write_session(
        profile.path, BrowserSession(browser="chrome", pid=pid, port=port, started_at="x")
    )
    assert read_session(profile.path, pid_alive=lambda p: True) is None


def test_a_symlinked_marker_does_not_make_a_directory_a_blumkin_profile(tmp_path: Path) -> None:
    real = tmp_path / "real-marker.json"
    real.write_text("{}", encoding="utf-8")
    fake = tmp_path / "browser-profiles" / "expense-report"
    fake.mkdir(parents=True, mode=0o700)
    (fake / ".blumkin-profile.json").symlink_to(real)
    with pytest.raises(NotBlumkinProfileError):
        require_blumkin_profile(fake)
    assert load_profile(tmp_path, "expense-report") is None
    assert list_profiles(tmp_path) == []


def test_create_profile_leaves_no_staging_directory_behind(tmp_path: Path) -> None:
    create_profile(tmp_path, "expense-report")
    names = sorted(p.name for p in (tmp_path / "browser-profiles").iterdir())
    assert names == ["expense-report"]


def test_a_leftover_staging_directory_is_never_listed_as_a_profile(tmp_path: Path) -> None:
    create_profile(tmp_path, "time-sheet")
    stale = tmp_path / "browser-profiles" / ".expense-report.creating-abc"
    stale.mkdir(mode=0o700)
    (stale / ".blumkin-profile.json").write_text("{}", encoding="utf-8")
    assert [p.task for p in list_profiles(tmp_path)] == ["time-sheet"]


def test_write_session_does_not_follow_or_reuse_a_stale_temp_file(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    (profile.path / ".blumkin-session.json.tmp").symlink_to(victim)
    session = BrowserSession(browser="chrome", pid=4242, port=40123, started_at="x")
    write_session(profile.path, session)
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert read_session(profile.path, pid_alive=lambda p: True) == session
    assert _mode(profile.path / ".blumkin-session.json") == 0o600


@pytest.mark.parametrize("pid", ["1e30", "Infinity", 10**30, 2**40, -1, 0])
def test_read_session_survives_a_corrupt_or_overflowing_pid(tmp_path: Path, pid: object) -> None:
    profile = create_profile(tmp_path, "expense-report")
    raw = {"browser": "chrome", "pid": pid, "port": 40123, "started_at": "x"}
    (profile.path / ".blumkin-session.json").write_text(json.dumps(raw), encoding="utf-8")
    assert read_session(profile.path, pid_alive=lambda p: True) is None
    delete_profile(tmp_path, "expense-report", is_browser=lambda p, d: False)
    assert not profile.path.exists()


def test_profile_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    with profile_lock(profile):
        with pytest.raises(ProfileBusyError):
            with profile_lock(profile):
                pass
    with profile_lock(profile):
        pass


def test_delete_profile_refuses_while_a_launch_holds_the_lock(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    fd = os.open(profile.path / ".blumkin-launch.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(ProfileInUseError) as excinfo:
            delete_profile(tmp_path, "expense-report")
    finally:
        os.close(fd)
    assert "launch" in str(excinfo.value)
    assert profile.path.exists()


def test_running_browser_pid_reads_the_singleton_lock(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    assert running_browser_pid(profile.path) is None
    (profile.path / "SingletonLock").symlink_to("some-host-4242")
    assert running_browser_pid(profile.path, is_browser=lambda pid, d: pid == 4242) == 4242
    assert running_browser_pid(profile.path, is_browser=lambda pid, d: False) is None


@pytest.mark.parametrize("target", ["host-notanumber", "nohyphen", "host-1", "host-0"])
def test_running_browser_pid_ignores_a_malformed_singleton_lock(
    tmp_path: Path, target: str
) -> None:
    profile = create_profile(tmp_path, "expense-report")
    (profile.path / "SingletonLock").symlink_to(target)
    assert running_browser_pid(profile.path, is_browser=lambda pid, d: True) is None


def test_delete_profile_refuses_while_a_browser_holds_the_profile_without_a_session(
    tmp_path: Path,
) -> None:
    profile = create_profile(tmp_path, "expense-report")
    (profile.path / "SingletonLock").symlink_to("host-4242")
    with pytest.raises(ProfileInUseError):
        delete_profile(tmp_path, "expense-report", is_browser=lambda pid, d: True)
    assert profile.path.exists()


def test_create_profile_removes_an_old_staging_directory_but_not_a_recent_one(
    tmp_path: Path,
) -> None:
    root = tmp_path / "browser-profiles"
    root.mkdir(mode=0o700)
    old = root / ".expense-report.creating-old"
    recent = root / ".expense-report.creating-new"
    other = root / ".time-sheet.creating-old"
    for directory in (old, recent, other):
        directory.mkdir()
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    os.utime(other, (two_hours_ago, two_hours_ago))
    create_profile(tmp_path, "expense-report")
    assert not old.exists()
    assert recent.exists()
    assert other.exists()


def test_when_ps_cannot_answer_reuse_is_strict_but_deletion_stays_conservative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = create_profile(tmp_path, "expense-report")
    session = BrowserSession(browser="chrome", pid=os.getpid(), port=40123, started_at="x")
    write_session(profile.path, session)

    def no_ps(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("ps")

    monkeypatch.setattr("blumkin.browser.profiles.subprocess.run", no_ps)
    assert read_session(profile.path) == session
    assert read_session(profile.path, strict=True) is None
    with pytest.raises(ProfileInUseError):
        delete_profile(tmp_path, "expense-report")
    assert process_is_browser_for(os.getpid(), profile.path, assume_on_unknown=False) is False


def test_strict_running_browser_pid_does_not_trust_an_unverifiable_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = create_profile(tmp_path, "expense-report")
    (profile.path / "SingletonLock").symlink_to(f"host-{os.getpid()}")

    def no_ps(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("ps")

    monkeypatch.setattr("blumkin.browser.profiles.subprocess.run", no_ps)
    assert running_browser_pid(profile.path) == os.getpid()
    assert running_browser_pid(profile.path, strict=True) is None
