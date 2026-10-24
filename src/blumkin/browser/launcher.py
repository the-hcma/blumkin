"""Launch the task browser on its profile with DevTools on loopback only (issue #465).

The browser is started detached (its own session, no inherited streams) so it outlives
blumkin and is never killed on exit; the user signs in by hand in its window. DevTools
binds `127.0.0.1` on an OS-allocated port, and the port Chrome picked is read back from
its `DevToolsActivePort` file.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from blumkin.browser.discovery import ResolvedBrowser
from blumkin.browser.profiles import (
    DEBUG_HOST,
    BrowserProfile,
    BrowserSession,
    read_session,
    remove_session,
    require_blumkin_profile,
    write_session,
)


class BrowserLaunchError(RuntimeError):
    """The browser did not open a DevTools port."""


DEFAULT_LAUNCH_TIMEOUT_S = 30.0


LOCK_FILENAME = ".blumkin-launch.lock"


@dataclass(frozen=True, slots=True)
class LaunchResult:
    already_running: bool
    session: BrowserSession


PORT_FILENAME = "DevToolsActivePort"


def build_launch_args(
    browser: Path, profile_dir: Path, extra_args: Sequence[str] = ()
) -> list[str]:
    """Command line: dedicated profile, DevTools on loopback, OS-allocated port."""
    return [
        str(browser),
        f"--user-data-dir={profile_dir}",
        "--remote-debugging-port=0",
        f"--remote-debugging-address={DEBUG_HOST}",
        "--no-first-run",
        "--no-default-browser-check",
        *extra_args,
    ]


def launch(
    profile: BrowserProfile,
    browser: ResolvedBrowser,
    *,
    extra_args: Sequence[str] = (),
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
    with _launch_lock(profile):
        existing = read_session(profile.path, pid_alive=pid_alive)
        if existing is not None:
            return LaunchResult(already_running=True, session=existing)
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
        port = _wait_for_port(
            profile.path, proc, now=now, sleep=sleep, stop=stop, timeout_s=timeout_s
        )
        session = BrowserSession(
            browser=browser.kind.value,
            pid=int(proc.pid),
            port=port,
            started_at=_utc_now(),
        )
        write_session(profile.path, session)
        return LaunchResult(already_running=False, session=session)


def read_devtools_port(profile_dir: Path) -> int | None:
    """Return the DevTools port Chrome wrote, None when it has not written one yet."""
    try:
        first = (profile_dir / PORT_FILENAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not first:
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


@contextlib.contextmanager
def _launch_lock(profile: BrowserProfile) -> Iterator[None]:
    """One launch per task at a time; a second caller fails fast instead of racing."""
    fd = os.open(profile.path / LOCK_FILENAME, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BrowserLaunchError(
                f"Expected no other launch for task {profile.task!r} in progress, "
                "got another launch still running"
            ) from exc
        yield
    finally:
        os.close(fd)


def _stop_browser(proc: Any, stop: Callable[[int, int], None]) -> None:
    """Stop a browser we started: its whole process group (it has its own session), then reap."""
    with contextlib.suppress(OSError):
        proc.terminate()
    with contextlib.suppress(OSError):
        stop(int(proc.pid), signal.SIGTERM)
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        proc.wait(timeout=_STOP_WAIT_S)


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wait_for_port(
    profile_dir: Path,
    proc: Any,
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    stop: Callable[[int, int], None],
    timeout_s: float,
) -> int:
    deadline = now() + timeout_s
    while True:
        try:
            port = read_devtools_port(profile_dir)
        except BrowserLaunchError:
            _stop_browser(proc, stop)
            raise
        if port is not None:
            return port
        code = proc.poll()
        if code is not None:
            raise BrowserLaunchError(
                f"Expected the browser to open a DevTools port, got exit code {code} before one"
            )
        if now() >= deadline:
            _stop_browser(proc, stop)
            raise BrowserLaunchError(
                f"Expected the browser to open a DevTools port within {timeout_s} seconds, got none"
            )
        sleep(0.1)
