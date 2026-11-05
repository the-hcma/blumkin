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
from collections.abc import Callable
from dataclasses import dataclass

#: Seconds the System Events script may take (it can wait on an accessibility prompt).
RAISE_TIMEOUT_S = 10.0
#: The longest window title kept.
_MAX_TITLE_CHARS = 200

# `{pid}` is replaced by an integer only. The script un-minimizes and raises every window of
# that one process and returns the title of the first, so the user can be told which it was.
_RAISE_SCRIPT = """
tell application "System Events"
    set target to first process whose unix id is {pid}
    set frontmost of target to true
    set shown to ""
    repeat with candidate in windows of target
        try
            set value of attribute "AXMinimized" of candidate to false
        end try
        try
            perform action "AXRaise" of candidate
        end try
        if shown is "" then
            try
                set shown to name of candidate
            end try
        end if
    end repeat
    return shown
end tell
"""

_TITLE_SCRIPT = """
tell application "System Events"
    set target to first process whose unix id is {pid}
    if (count of windows of target) is 0 then return ""
    return name of window 1 of target
end tell
"""


class WindowRaiseError(RuntimeError):
    """The window could not be raised (not macOS, no permission, no such window)."""


@dataclass(frozen=True, slots=True)
class RaisedWindow:
    """Which window was raised: the process and its (page-supplied, so untrusted) title."""

    pid: int
    title: str


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def raise_window(
    pid: int, *, platform: str | None = None, run: Runner | None = None
) -> RaisedWindow:
    """Un-minimize and raise every window of process `pid`; report the first one's title."""
    _require_supported(platform)
    title = _osascript(_RAISE_SCRIPT, pid, run)
    return RaisedWindow(pid=pid, title=_clean(title))


def window_title(pid: int, *, platform: str | None = None, run: Runner | None = None) -> str | None:
    """The title of the process's first window, or None when it cannot be read (best effort)."""
    try:
        _require_supported(platform)
        return _clean(_osascript(_TITLE_SCRIPT, pid, run))
    except WindowRaiseError:
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
    if result.returncode != 0:
        detail = _clean(str(result.stderr))[:120] or "no detail"
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
