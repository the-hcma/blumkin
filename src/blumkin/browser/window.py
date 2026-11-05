"""Bring a task's browser window to the front (issue #656).

Every task profile runs as its own Edge or Chrome process, all under one application name, so
activating the application by name raises whichever instance the system picks (often the user's
regular mail window). The right window is found by the process id blumkin recorded when it
launched the browser, and raised through System Events. macOS only: elsewhere the command says
so instead of guessing.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

#: Seconds the System Events script may take (it can wait on an accessibility prompt).
RAISE_TIMEOUT_S = 10.0
#: How long to wait before trying again when the process is not listed yet.
RETRY_AFTER_S = 1.0
#: The longest window title kept.
_MAX_TITLE_CHARS = 200

# `{pid}` is replaced by an integer only. The script un-hides the process, un-minimizes all its
# windows and then raises the one that was in front (found again by its title, since raising
# reorders the windows), so that window ends up in front, and returns its title. A process
# with no window is an error: nothing was raised.
_RAISE_SCRIPT = """
tell application "System Events"
    set proc to first process whose unix id is {pid}
    set visible of proc to true
    set frontmost of proc to true
    if (count of windows of proc) is 0 then error "no windows"
    set shown to ""
    try
        set shown to name of window 1 of proc
    end try
    repeat with candidate in windows of proc
        try
            set value of attribute "AXMinimized" of candidate to false
        end try
    end repeat
    try
        if shown is "" then
            perform action "AXRaise" of window 1 of proc
        else
            perform action "AXRaise" of (first window of proc whose name is shown)
        end if
    end try
    return shown
end tell
"""

_TITLE_SCRIPT = """
tell application "System Events"
    set proc to first process whose unix id is {pid}
    if (count of windows of proc) is 0 then return ""
    return name of window 1 of proc
end tell
"""


class WindowRaiseError(RuntimeError):
    """The window could not be raised (not macOS, no permission, no such window)."""

    def __init__(self, message: str, *, not_listed: bool = False) -> None:
        super().__init__(message)
        #: System Events does not list that process (yet, or any more).
        self.not_listed = not_listed


@dataclass(frozen=True, slots=True)
class RaisedWindow:
    """Which window was raised: the process and its (page-supplied, so untrusted) title."""

    pid: int
    title: str


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def raise_window(
    pid: int,
    *,
    platform: str | None = None,
    run: Runner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RaisedWindow:
    """Un-hide, un-minimize and raise every window of process `pid`; report the front one's title.

    A browser that was only just started may not be listed by System Events yet: that one
    failure is tried again after a second before it is reported.
    """
    _require_supported(platform)
    try:
        title = _osascript(_RAISE_SCRIPT, pid, run)
    except WindowRaiseError as exc:
        if not exc.not_listed:
            raise
        sleep(RETRY_AFTER_S)
        title = _osascript(_RAISE_SCRIPT, pid, run)
    return RaisedWindow(pid=pid, title=_clean(title))


def window_title(pid: int, *, platform: str | None = None, run: Runner | None = None) -> str | None:
    """The title of the process's first window, or None when it cannot be read (best effort)."""
    try:
        _require_supported(platform)
        return _clean(_osascript(_TITLE_SCRIPT, pid, run))
    except Exception:  # noqa: BLE001 - best effort: a title is never worth a traceback
        return None


def _clean(text: str) -> str:
    """One short printable line: window titles are page text, so no control characters."""
    kept = "".join(char if char.isprintable() else " " for char in text)
    return " ".join(kept.split())[:_MAX_TITLE_CHARS]


def _osascript(script: str, pid: int, run: Runner | None) -> str:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 2:
        raise WindowRaiseError(f"Expected a process id of 2 or more, got {pid!r}")
    runner = run or subprocess.run
    try:
        result = runner(
            ["osascript", "-e", script.format(pid=pid)],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=RAISE_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise WindowRaiseError("Expected osascript on PATH, got none") from exc
    except subprocess.TimeoutExpired as exc:
        raise WindowRaiseError(
            f"Expected the window to be raised within {RAISE_TIMEOUT_S:g} seconds, got no answer "
            "(a permission prompt may be waiting)"
        ) from exc
    except OSError as exc:
        raise WindowRaiseError(f"Expected osascript to run, got {type(exc).__name__}") from exc
    if result.returncode != 0:
        detail = _clean(str(result.stderr))[:120] or "no detail"
        if "no windows" in detail:
            raise WindowRaiseError(
                f"Expected the browser process {pid} to have a window, got none ({detail}); "
                "open one in the browser",
                not_listed=True,
            )
        if "-1728" in detail or "Can't get" in detail or "Can\u2019t get" in detail:
            raise WindowRaiseError(
                f"Expected System Events to list the browser process {pid}, got none ({detail}); "
                "the browser may have exited, or its window is not up yet",
                not_listed=True,
            )
        raise WindowRaiseError(
            "Expected System Events to raise the browser window, got a failure "
            f"({detail}); allow your terminal under System Settings > Privacy & Security > "
            "Accessibility"
        )
    return str(result.stdout)


def _require_supported(platform: str | None) -> None:
    if (platform or sys.platform) != "darwin":
        raise WindowRaiseError(
            "Expected macOS to raise a browser window, got another system; raise it by hand"
        )
