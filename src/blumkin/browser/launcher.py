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
    process_is_browser_for,
    profile_lock,
    read_session,
    remove_session,
    require_blumkin_profile,
    running_browser_pid,
    write_session,
)


class BrowserCloseError(RuntimeError):
    """The browser did not stop when asked."""


class BrowserLaunchError(RuntimeError):
    """The browser did not open a DevTools port."""


DEFAULT_CLOSE_TIMEOUT_S = 15.0
DEFAULT_LAUNCH_TIMEOUT_S = 30.0


@dataclass(frozen=True, slots=True)
class CloseResult:
    forced: bool
    was_running: bool


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


def close_browser(
    profile: BrowserProfile,
    *,
    force: bool = False,
    is_browser: Callable[[int, Path], bool] | None = None,
    now: Callable[[], float] = time.monotonic,
    send_signal: Callable[[int, int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout_s: float = DEFAULT_CLOSE_TIMEOUT_S,
) -> CloseResult:
    """Stop the task's browser, leaving its profile (cookies, saved sign-in) in place.

    The browser is asked to quit with SIGTERM, which lets it write its profile out, and given
    `timeout_s` to exit. It is signalled only if its process is confirmed, by its command line,
    to be a browser running on this very profile, so a stale session whose pid has since been
    reused is never touched. A browser that does not exit is reported; only `force` kills it.
    Nothing running is not an error: the answer is `was_running=False`.

    "Cannot tell" is never "not running": when `ps` does not answer, a live process is treated
    as still being the browser, so a close neither reports success for a browser that is still
    up nor throws its session record away.
    """
    require_blumkin_profile(profile.path)
    sender = send_signal or _signal_process

    def identity(pid: int) -> str:
        """`browser`, `other` (confirmed not the browser, or gone) or `unknown` (`ps` silent)."""
        if is_browser is not None:
            return "browser" if is_browser(pid, profile.path) else "other"
        if process_is_browser_for(pid, profile.path, assume_on_unknown=False):
            return "browser"
        if process_is_browser_for(pid, profile.path, assume_on_unknown=True):
            return "unknown"
        return "other"

    def not_gone(pid: int) -> bool:
        return identity(pid) != "other"

    def signal_pid(pid: int, sig: int) -> None:
        try:
            sender(pid, sig)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise BrowserCloseError(
                f"Expected permission to signal the browser for task {profile.task!r}, got {exc}"
            ) from exc

    with profile_lock(profile):
        session = read_session(profile.path, pid_alive=lambda pid: not_gone(pid), strict=True)
        pid = (
            session.pid
            if session is not None
            else running_browser_pid(
                profile.path, is_browser=lambda pid, directory: not_gone(pid), strict=True
            )
        )
        if pid is None:
            remove_session(profile.path)
            return CloseResult(forced=False, was_running=False)
        if identity(pid) == "unknown":
            raise BrowserCloseError(
                f"Expected to confirm that process {pid} is the browser for task "
                f"{profile.task!r}, but `ps` did not answer, so it could not confirm; "
                "nothing was signalled. Try again, or close the browser window by hand"
            )
        signal_pid(pid, signal.SIGTERM)
        deadline = now() + timeout_s
        while not_gone(pid) and now() < deadline:
            sleep(_CLOSE_POLL_S)
        forced = False
        if not_gone(pid):
            if not force:
                raise BrowserCloseError(
                    f"Expected the browser for task {profile.task!r} to close within "
                    f"{timeout_s:g} seconds, got it still running; close its window, or retry "
                    "with --force to kill it (its profile may need to recover)"
                )
            # Look again right before the kill: it may have left while we were waiting, and the
            # pid must still be the browser (or unconfirmable, which counts as still running).
            if not_gone(pid):
                forced = True
                signal_pid(pid, signal.SIGKILL)
                deadline = now() + _FORCE_WAIT_S
                while not_gone(pid) and now() < deadline:
                    sleep(_CLOSE_POLL_S)
                if not_gone(pid):
                    raise BrowserCloseError(
                        f"Expected the browser for task {profile.task!r} to stop after being "
                        "killed, got it still running"
                    )
                # A killed Chrome leaves its single-instance files behind; a graceful one
                # removes them itself.
                for name in _CHROME_SINGLETON_FILES:
                    with contextlib.suppress(OSError):
                        (profile.path / name).unlink()
        remove_session(profile.path)
        with contextlib.suppress(FileNotFoundError):
            (profile.path / PORT_FILENAME).unlink()
        return CloseResult(forced=forced, was_running=True)


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


_CHROME_SINGLETON_FILES = ("SingletonCookie", "SingletonLock", "SingletonSocket")
_CLOSE_POLL_S = 0.2
_FORCE_WAIT_S = 5.0
_STOP_WAIT_S = 5.0


def _signal_process(pid: int, sig: int) -> None:
    """Signal the browser: its whole group for a kill (it leads its own session), else the pid.

    A graceful SIGTERM goes to the main process alone, which closes its children itself. The
    group is signalled for SIGKILL only when the pid leads it, so a browser started some other
    way, inside the user's shell group, never takes the shell down with it. A target that is
    this process, its parent, or the head of its process group or session is refused outright:
    a wrapper such as `bash -c 'chrome --user-data-dir=...'` carries the profile on its command
    line too, and signalling that would take blumkin's own caller down.
    """
    if pid in (os.getpid(), os.getppid(), os.getpgrp(), os.getsid(0)):
        raise BrowserCloseError(
            f"Expected a browser to signal, got process {pid}, which is part of this command's "
            "own process tree; refusing to signal it"
        )
    if sig == signal.SIGKILL:
        with contextlib.suppress(OSError):
            if os.getpgid(pid) == pid:
                os.killpg(pid, sig)
                return
    os.kill(pid, sig)


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
