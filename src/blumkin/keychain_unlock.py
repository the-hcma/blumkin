"""Opt-in CLI unlock of the macOS login keychain (issue #444).

A keychain access from a session nobody is watching (agent, MCP server, SSH)
hangs on macOS's GUI unlock prompt until ``secret_store``'s I/O timeout fires.
``keychain_unlock`` in ``config.toml`` picks how blumkin gets past a *locked*
keychain:

- ``"gui"`` (default): leave it to the macOS prompt (today's behaviour) - unless
  no GUI is reachable (SSH session, no Aqua login session), where nobody could
  answer that prompt, so it behaves like ``"cli"``.
- ``"cli"``: when the keychain is locked and stdin is a TTY, run
  ``security unlock-keychain`` and let it prompt for the password itself; with
  no TTY, fail fast with the command to run instead of hanging.
- ``"off"``: never attempt an unlock.

The password is never passed on the command line (``-p`` is visible in ``ps``),
read, logged or stored by blumkin; ``security`` prompts on the TTY directly.

An "Always Allow" dialog for a *new binary* is an item access-list problem, not
a locked keychain, and unlocking does not clear it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading

KEYCHAIN_UNLOCK_MODES = ("cli", "gui", "off")
UNLOCK_COMMAND = "security unlock-keychain"

# `security show-keychain-info` blocks on the GUI unlock prompt when the
# keychain is locked, so a short timeout is the lock signal itself.
_LOCK_PROBE_TIMEOUT_SECONDS = 2.0

_checked_lock = threading.Lock()
_checked = False
# Per-process cache: the session type does not change mid-run, and the default
# "gui" mode would otherwise fork `launchctl` on every keychain read.
_gui_available: bool | None = None


class KeychainLockedError(OSError):
    """The login keychain is locked and cannot be unlocked from this session."""


def ensure_unlocked(mode: str) -> None:
    """Make sure the login keychain is usable before keychain I/O, per ``mode``.

    No-op unless the effective mode is ``"cli"`` (``mode == "cli"``, or
    ``"gui"`` with no GUI available) on macOS with ``security`` available, and
    checked at most once per process. Raises ``KeychainLockedError`` when the
    keychain is locked and there is no TTY to prompt on (or the unlock fails).
    """
    global _checked
    if _checked or not _supported():
        return
    if not (mode == "cli" or (mode == "gui" and not gui_available())):
        return
    with _checked_lock:
        if _checked:
            return
        if keychain_is_locked():
            if not sys.stdin.isatty():
                raise KeychainLockedError(_locked_message())
            if not unlock():
                raise KeychainLockedError("the keychain unlock failed; " + _locked_message())
        _checked = True


def gui_available() -> bool:
    """True when a macOS GUI prompt could be answered from this session.

    False over SSH or outside the Aqua login session (``launchctl managername``
    is ``Background``/``System`` there); True when it cannot be determined, so
    the default behaviour stays today's.
    """
    global _gui_available
    if _gui_available is None:
        _gui_available = _probe_gui_available()
    return _gui_available


def _probe_gui_available() -> bool:
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    try:
        proc = subprocess.run(
            ["launchctl", "managername"],
            capture_output=True,
            check=False,
            text=True,
            timeout=_LOCK_PROBE_TIMEOUT_SECONDS,
        )
    except OSError, subprocess.TimeoutExpired:
        return True
    name = proc.stdout.strip()
    return proc.returncode != 0 or not name or name == "Aqua"


def keychain_is_locked() -> bool:
    """True when the default (login) keychain is locked. False off macOS."""
    if not _supported():
        return False
    try:
        proc = subprocess.run(
            ["security", "show-keychain-info"],
            capture_output=True,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
            text=True,
            timeout=_LOCK_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return True
    except OSError:
        return False
    return proc.returncode != 0 and "interaction is not allowed" in proc.stderr.lower()


def unlock() -> bool:
    """Run ``security unlock-keychain`` attached to the terminal; True on success."""
    try:
        return subprocess.run(["security", "unlock-keychain"], check=False).returncode == 0
    except OSError:
        return False


def _locked_message() -> str:
    return (
        "the macOS login keychain is locked and there is no GUI or terminal to unlock it on; "
        f"run `blumkin auth unlock` (or `! {UNLOCK_COMMAND}`) in your own terminal, "
        "then retry"
    )


def _reset_for_tests() -> None:
    global _checked, _gui_available
    with _checked_lock:
        _checked = False
    _gui_available = None


def _supported() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None
