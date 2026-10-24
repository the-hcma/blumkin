"""Launch the task browser on its profile with DevTools on loopback only (issue #465).

The browser is started detached (its own session, no inherited streams) so it outlives
blumkin and is never killed on exit; the user signs in by hand in its window. DevTools
binds `127.0.0.1` on an OS-allocated port, and the port Chrome picked is read back from
its `DevToolsActivePort` file.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from blumkin.browser.discovery import ResolvedBrowser
from blumkin.browser.profiles import (
    DEBUG_HOST,
    BrowserProfile,
    BrowserSession,
    ProfileBusyError,
    profile_lock,
    read_session,
    remove_session,
    require_blumkin_profile,
    running_browser_pid,
    write_session,
)


class BrowserLaunchError(RuntimeError):
    """The browser did not open a DevTools port."""


DEFAULT_LAUNCH_TIMEOUT_S = 30.0


@dataclass(frozen=True, slots=True)
class LaunchResult:
    already_running: bool
    session: BrowserSession


PORT_FILENAME = "DevToolsActivePort"


def build_launch_args(
    browser: Path, profile_dir: Path, extra_args: Sequence[str] = ()
) -> list[str]:
    """Command line: dedicated profile, DevTools on loopback, OS-allocated port.

    The extra arguments come first and the fixed ones last, because Chrome keeps the last
    occurrence of a repeated switch: a caller cannot override the profile or the DevTools
    switches. `--remote-debugging-address` is advisory (headful Chrome ignores it and binds
    loopback by default), so the loopback guarantee is Chrome's default plus the loopback-only
    endpoint check at attach time.

    A bare `--` (or a blank argument) is refused: Chromium treats `--` as the end of the
    switches, so everything after it, including the fixed switches, would become a plain
    argument and be ignored.
    """
    for argument in extra_args:
        if argument.strip() in ("", "--"):
            raise ValueError(
                f"Expected browser switches or a start URL, got {argument!r}; "
                "a bare '--' would end switch parsing and drop the profile switch"
            )
    return [
        str(browser),
        *extra_args,
        f"--user-data-dir={profile_dir}",
        "--remote-debugging-port=0",
        f"--remote-debugging-address={DEBUG_HOST}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def launch(
    profile: BrowserProfile,
    browser: ResolvedBrowser,
    *,
    extra_args: Sequence[str] = (),
    is_browser: Callable[[int, Path], bool] | None = None,
    kill_group: Callable[[int, int], None] | None = None,
    now: Callable[[], float] = time.monotonic,
    pid_alive: Callable[[int], bool] | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
    timeout_s: float = DEFAULT_LAUNCH_TIMEOUT_S,
) -> LaunchResult:
    """Start the browser on the profile, or return the session already running."""
    require_blumkin_profile(profile.path)
    stop = kill_group or os.killpg
    try:
        with profile_lock(profile):
            existing = read_session(profile.path, pid_alive=pid_alive, strict=True)
            if existing is not None:
                return LaunchResult(already_running=True, session=existing)
            if running_browser_pid(profile.path, is_browser=is_browser, strict=True) is not None:
                raise BrowserLaunchError(
                    f"Expected the browser for task {profile.task!r} to be closed, got a running "
                    "browser with no session record; close it and launch again"
                )
            remove_session(profile.path)
            with contextlib.suppress(FileNotFoundError):
                (profile.path / PORT_FILENAME).unlink()

            proc = popen(
                build_launch_args(browser.path, profile.path, extra_args),
                close_fds=True,
                start_new_session=True,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )
            try:
                port = _wait_for_port(profile.path, proc, now=now, sleep=sleep, timeout_s=timeout_s)
                session = BrowserSession(
                    browser=browser.kind.value,
                    pid=int(proc.pid),
                    port=port,
                    started_at=_utc_now(),
                )
                write_session(profile.path, session)
            except BaseException:
                _stop_browser(proc, stop)
                raise
            return LaunchResult(already_running=False, session=session)
    except ProfileBusyError as exc:
        raise BrowserLaunchError(
            f"Expected no other launch for task {profile.task!r} in progress, "
            "got another launch still running"
        ) from exc


def read_devtools_port(profile_dir: Path) -> int | None:
    """Return the DevTools port Chrome wrote, None when it has not written one yet."""
    try:
        first = (profile_dir / PORT_FILENAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    # Chrome writes the port, then the browser path on a second line: a file with only the
    # first line may be half written (a truncated "40123" would read as the wrong port).
    if len(first) < 2:
        return None
    try:
        port = int(first[0].strip())
    except ValueError as exc:
        raise BrowserLaunchError(
            f"Expected a TCP port on the first line of {PORT_FILENAME}, got {first[0]!r}"
        ) from exc
    if not 1 <= port <= 65535:
        raise BrowserLaunchError(f"Expected a TCP port from 1 to 65535, got {port}")
    return port


_STOP_WAIT_S = 5.0


def _stop_browser(proc: Any, stop: Callable[[int, int], None]) -> None:
    """Stop a browser we started: its whole process group (it has its own session), then reap.

    SIGTERM first; if it has not exited after `_STOP_WAIT_S`, SIGKILL. A browser that has
    already exited is left alone: its pid may since belong to an unrelated process group.
    """
    if proc.poll() is not None:
        return
    with contextlib.suppress(OSError):
        proc.terminate()
    with contextlib.suppress(OSError):
        stop(int(proc.pid), signal.SIGTERM)
    try:
        proc.wait(timeout=_STOP_WAIT_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            stop(int(proc.pid), signal.SIGKILL)
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            proc.wait(timeout=_STOP_WAIT_S)
    except OSError:
        pass


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wait_for_port(
    profile_dir: Path,
    proc: Any,
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    timeout_s: float,
) -> int:
    deadline = now() + timeout_s
    last_error: BrowserLaunchError | None = None
    while True:
        try:
            port = read_devtools_port(profile_dir)
        except BrowserLaunchError as exc:
            # Chrome may still be writing the file: keep reading until the deadline.
            last_error, port = exc, None
        if port is not None:
            return port
        code = proc.poll()
        if code is not None:
            raise BrowserLaunchError(
                f"Expected the browser to open a DevTools port, got exit code {code} before one"
            )
        if now() >= deadline:
            if last_error is not None:
                raise last_error
            raise BrowserLaunchError(
                f"Expected the browser to open a DevTools port within {timeout_s} seconds, got none"
            )
        sleep(0.1)
