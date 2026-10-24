"""Launching the task browser on its profile (issue #465)."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest

from blumkin.browser.discovery import BrowserKind, ResolvedBrowser
from blumkin.browser.launcher import (
    BrowserLaunchError,
    build_launch_args,
    launch,
)
from blumkin.browser.profiles import (
    BrowserProfile,
    BrowserSession,
    NotBlumkinProfileError,
    create_profile,
    process_is_browser_for,
    read_session,
    write_session,
)

_BROWSER = ResolvedBrowser(
    kind=BrowserKind.CHROME, path=Path("/opt/chrome/chrome"), fallback_from=None
)


class _Killpg:
    """Records process-group signals; the fake processes use pytest's own pid."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, pgid: int, sig: int) -> None:
        self.calls.append((pgid, sig))


@pytest.fixture(autouse=True)
def _never_signal_a_real_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake processes carry pytest's pid, so an un-injected killpg would hit pytest."""

    def refuse(pgid: int, sig: int) -> None:
        raise AssertionError(f"os.killpg({pgid}, {sig}) must be injected in tests")

    monkeypatch.setattr("blumkin.browser.launcher.os.killpg", refuse)


class _Clock:
    """A fake monotonic clock whose `sleep` advances it (no real waiting)."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Proc:
    def __init__(self, pid: int, exit_code: int | None = None, wait_timeouts: int = 0) -> None:
        self.pid = pid
        self._exit_code = exit_code
        self._wait_timeouts = wait_timeouts
        self.terminated = False
        self.waited = False

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        if self._wait_timeouts:
            self._wait_timeouts -= 1
            raise subprocess.TimeoutExpired("browser", timeout or 0.0)
        return 0

    def poll(self) -> int | None:
        return self._exit_code

    def terminate(self) -> None:
        self.terminated = True


class _Popen:
    """Records the launch and, like Chrome, writes DevToolsActivePort into the profile."""

    def __init__(
        self,
        *,
        port_text: str | None = "40123\n/devtools/browser/abc\n",
        exit_code: int | None = None,
        wait_timeouts: int = 0,
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.proc = _Proc(os.getpid(), exit_code, wait_timeouts)
        self._port_text = port_text

    def __call__(self, args: list[str], **kwargs: Any) -> _Proc:
        self.calls.append((args, kwargs))
        if self._port_text is not None:
            data_dir = next(a for a in args if a.startswith("--user-data-dir="))
            Path(data_dir.split("=", 1)[1], "DevToolsActivePort").write_text(
                self._port_text, encoding="utf-8"
            )
        return self.proc


def test_launch_args_bind_loopback_on_an_os_allocated_port_with_the_task_profile() -> None:
    args = build_launch_args(Path("/opt/chrome/chrome"), Path("/data/p"))
    assert args[0] == "/opt/chrome/chrome"
    assert "--user-data-dir=/data/p" in args
    assert "--remote-debugging-port=0" in args
    assert "--remote-debugging-address=127.0.0.1" in args
    assert not any("0.0.0.0" in a for a in args)


def test_launch_starts_the_browser_detached_and_records_the_endpoint(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    popen = _Popen()
    clock = _Clock()

    result = launch(profile, _BROWSER, popen=popen, now=clock.monotonic, sleep=clock.sleep)

    assert result.already_running is False
    assert result.session.port == 40123
    assert result.session.endpoint == "http://127.0.0.1:40123"
    assert result.session.browser == "chrome"
    args, kwargs = popen.calls[0]
    assert f"--user-data-dir={profile.path}" in args
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert read_session(profile.path, pid_alive=lambda pid: True) == result.session
    assert popen.proc.terminated is False


def test_launch_is_idempotent_when_the_browser_is_already_running(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    existing = BrowserSession(
        browser="chrome", pid=os.getpid(), port=41000, started_at="2026-01-01T00:00:00Z"
    )
    write_session(profile.path, existing)
    popen = _Popen()

    result = launch(profile, _BROWSER, popen=popen, pid_alive=lambda pid: True)

    assert result.already_running is True
    assert result.session == existing
    assert popen.calls == []


def test_launch_replaces_a_stale_session_and_a_stale_port_file(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    write_session(
        profile.path,
        BrowserSession(browser="chrome", pid=2**22 + 999, port=1, started_at="x"),
    )
    (profile.path / "DevToolsActivePort").write_text("1\n/old\n", encoding="utf-8")
    popen = _Popen(port_text="40555\n/devtools/browser/new\n")
    clock = _Clock()

    result = launch(
        profile,
        _BROWSER,
        popen=popen,
        now=clock.monotonic,
        sleep=clock.sleep,
        pid_alive=lambda pid: False,
    )

    assert result.already_running is False
    assert result.session.port == 40555


def test_launch_times_out_and_stops_the_browser_it_started(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    popen = _Popen(port_text=None)
    clock = _Clock()
    killpg = _Killpg()

    with pytest.raises(BrowserLaunchError) as excinfo:
        launch(
            profile,
            _BROWSER,
            kill_group=killpg,
            popen=popen,
            now=clock.monotonic,
            sleep=clock.sleep,
            timeout_s=2.0,
        )

    assert "2.0" in str(excinfo.value)
    assert popen.proc.terminated is True
    assert popen.proc.waited is True
    assert killpg.calls == [(popen.proc.pid, signal.SIGTERM)]
    assert read_session(profile.path) is None


def test_launch_reports_a_browser_that_exits_before_opening_a_port(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    popen = _Popen(port_text=None, exit_code=3)
    clock = _Clock()

    with pytest.raises(BrowserLaunchError) as excinfo:
        launch(profile, _BROWSER, popen=popen, now=clock.monotonic, sleep=clock.sleep)

    assert "exit code 3" in str(excinfo.value)
    assert clock.sleeps == []


@pytest.mark.parametrize("port_text", ["not-a-port\n/x\n", "0\n/x\n", "70000\n/x\n", ""])
def test_launch_rejects_a_malformed_port_file(tmp_path: Path, port_text: str) -> None:
    profile = create_profile(tmp_path, "expense-report")
    popen = _Popen(port_text=port_text)
    clock = _Clock()
    killpg = _Killpg()

    with pytest.raises(BrowserLaunchError):
        launch(
            profile,
            _BROWSER,
            kill_group=killpg,
            popen=popen,
            now=clock.monotonic,
            sleep=clock.sleep,
            timeout_s=1.0,
        )

    assert popen.proc.terminated is True
    assert killpg.calls == [(popen.proc.pid, signal.SIGTERM)]
    assert read_session(profile.path, pid_alive=lambda pid: True) is None


def test_launch_refuses_a_profile_blumkin_did_not_create(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    popen = _Popen()

    with pytest.raises(NotBlumkinProfileError):
        launch(BrowserProfile(task="foreign", path=foreign), _BROWSER, popen=popen)

    assert popen.calls == []


def test_a_second_launch_for_the_same_task_fails_fast_instead_of_racing(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    popen = _Popen()
    fd = os.open(profile.path / ".blumkin-launch.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(BrowserLaunchError) as excinfo:
            launch(profile, _BROWSER, popen=popen)
    finally:
        os.close(fd)
    assert "another launch" in str(excinfo.value)
    assert "expense-report" in str(excinfo.value)
    assert popen.calls == []
    assert not (profile.path / "DevToolsActivePort").exists()


def test_the_launch_lock_is_released_after_a_launch(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    clock = _Clock()
    launch(profile, _BROWSER, popen=_Popen(), now=clock.monotonic, sleep=clock.sleep)
    fd = os.open(profile.path / ".blumkin-launch.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_extra_arguments_cannot_override_the_profile_or_the_devtools_switches() -> None:
    args = build_launch_args(
        Path("/opt/chrome/chrome"),
        Path("/data/p"),
        (
            "--user-data-dir=/etc",
            "--remote-debugging-port=9222",
            "--remote-debugging-address=0.0.0.0",
            "--headless=new",
        ),
    )

    def last(prefix: str) -> str:
        return [a for a in args if a.startswith(prefix)][-1]

    assert last("--user-data-dir=") == "--user-data-dir=/data/p"
    assert last("--remote-debugging-port=") == "--remote-debugging-port=0"
    assert last("--remote-debugging-address=") == "--remote-debugging-address=127.0.0.1"
    assert "--headless=new" in args
    assert args[0] == "/opt/chrome/chrome"


def test_a_start_url_in_the_extra_arguments_does_not_hide_the_browser_from_the_pid_check() -> None:
    args = build_launch_args(
        Path("/opt/chrome/chrome"), Path("/data/p"), ("https://example.com/start",)
    )
    ps = subprocess.CompletedProcess([], 0, stdout=" ".join(args) + "\n", stderr="")
    assert process_is_browser_for(os.getpid(), Path("/data/p"), run=lambda *a, **k: ps)


def test_a_running_browser_with_no_session_record_is_not_launched_twice(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    (profile.path / "SingletonLock").symlink_to("host-4242")
    (profile.path / "DevToolsActivePort").write_text("40123\n/x\n", encoding="utf-8")
    popen = _Popen()
    with pytest.raises(BrowserLaunchError) as excinfo:
        launch(profile, _BROWSER, popen=popen, is_browser=lambda pid, directory: pid == 4242)
    assert "running browser" in str(excinfo.value)
    assert popen.calls == []
    assert (profile.path / "DevToolsActivePort").exists()


def test_a_stale_singleton_lock_does_not_block_a_launch(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    (profile.path / "SingletonLock").symlink_to("host-4242")
    clock = _Clock()
    result = launch(
        profile,
        _BROWSER,
        is_browser=lambda pid, directory: False,
        popen=_Popen(),
        now=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result.already_running is False


def test_a_hung_browser_is_killed_after_the_graceful_stop_times_out(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    popen = _Popen(port_text=None, wait_timeouts=1)
    clock = _Clock()
    killpg = _Killpg()
    with pytest.raises(BrowserLaunchError):
        launch(
            profile,
            _BROWSER,
            kill_group=killpg,
            popen=popen,
            now=clock.monotonic,
            sleep=clock.sleep,
            timeout_s=1.0,
        )
    pid = popen.proc.pid
    assert killpg.calls == [(pid, signal.SIGTERM), (pid, signal.SIGKILL)]


def test_a_browser_that_already_exited_is_not_signalled(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    popen = _Popen(port_text=None, exit_code=3)
    clock = _Clock()
    killpg = _Killpg()
    with pytest.raises(BrowserLaunchError):
        launch(
            profile,
            _BROWSER,
            kill_group=killpg,
            popen=popen,
            now=clock.monotonic,
            sleep=clock.sleep,
        )
    assert killpg.calls == []


def test_a_failure_to_record_the_session_stops_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = create_profile(tmp_path, "expense-report")
    popen = _Popen()
    clock = _Clock()
    killpg = _Killpg()

    def broken(profile_dir: Path, session: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("blumkin.browser.launcher.write_session", broken)
    with pytest.raises(OSError, match="disk full"):
        launch(
            profile,
            _BROWSER,
            kill_group=killpg,
            popen=popen,
            now=clock.monotonic,
            sleep=clock.sleep,
        )
    assert killpg.calls == [(popen.proc.pid, signal.SIGTERM)]


def test_an_interrupt_while_waiting_stops_the_browser_and_propagates(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    popen = _Popen(port_text=None)
    killpg = _Killpg()

    def interrupted(seconds: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        launch(
            profile,
            _BROWSER,
            kill_group=killpg,
            popen=popen,
            now=_Clock().monotonic,
            sleep=interrupted,
        )
    assert killpg.calls == [(popen.proc.pid, signal.SIGTERM)]


def test_a_port_file_still_being_written_is_read_again_not_fatal(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    popen = _Popen(port_text="40")
    clock = _Clock()
    killpg = _Killpg()
    real_sleep = clock.sleep

    def finish_writing(seconds: float) -> None:
        (profile.path / "DevToolsActivePort").write_text("40123\n/x\n", encoding="utf-8")
        real_sleep(seconds)

    result = launch(
        profile,
        _BROWSER,
        kill_group=killpg,
        popen=popen,
        now=clock.monotonic,
        sleep=finish_writing,
    )
    assert result.session.port == 40123
    assert killpg.calls == []


@pytest.mark.parametrize(
    "extra", [("--",), ("--headless=new", "--", "--user-data-dir=/etc"), ("",)]
)
def test_a_bare_double_dash_or_blank_extra_argument_is_refused(extra: tuple[str, ...]) -> None:
    with pytest.raises(ValueError) as excinfo:
        build_launch_args(Path("/opt/chrome/chrome"), Path("/data/p"), extra)
    assert "Expected" in str(excinfo.value)
    assert "--" in str(excinfo.value)


def test_launch_refuses_a_bare_double_dash_before_starting_anything(tmp_path: Path) -> None:
    profile = create_profile(tmp_path, "expense-report")
    popen = _Popen()
    with pytest.raises(ValueError):
        launch(profile, _BROWSER, extra_args=("--",), popen=popen)
    assert popen.calls == []
    assert not (profile.path / "DevToolsActivePort").exists()


def test_ordinary_switches_and_a_start_url_are_still_accepted() -> None:
    args = build_launch_args(
        Path("/opt/chrome/chrome"),
        Path("/data/p"),
        ("--headless=new", "--no-sandbox", "https://example.com/start"),
    )
    assert "--headless=new" in args
    assert "https://example.com/start" in args
