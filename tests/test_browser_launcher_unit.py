"""Launching the task browser on its profile (issue #465)."""

from __future__ import annotations

import os
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
    read_session,
    write_session,
)

_BROWSER = ResolvedBrowser(
    kind=BrowserKind.CHROME, path=Path("/opt/chrome/chrome"), fallback_from=None
)


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
    def __init__(self, pid: int, exit_code: int | None = None) -> None:
        self.pid = pid
        self._exit_code = exit_code
        self.terminated = False

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
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.proc = _Proc(os.getpid(), exit_code)
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

    with pytest.raises(BrowserLaunchError) as excinfo:
        launch(
            profile,
            _BROWSER,
            popen=popen,
            now=clock.monotonic,
            sleep=clock.sleep,
            timeout_s=2.0,
        )

    assert "2.0" in str(excinfo.value)
    assert popen.proc.terminated is True
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

    with pytest.raises(BrowserLaunchError):
        launch(
            profile,
            _BROWSER,
            popen=popen,
            now=clock.monotonic,
            sleep=clock.sleep,
            timeout_s=1.0,
        )

    assert popen.proc.terminated is True
    assert read_session(profile.path, pid_alive=lambda pid: True) is None


def test_launch_refuses_a_profile_blumkin_did_not_create(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    popen = _Popen()

    with pytest.raises(NotBlumkinProfileError):
        launch(BrowserProfile(task="foreign", path=foreign), _BROWSER, popen=popen)

    assert popen.calls == []
