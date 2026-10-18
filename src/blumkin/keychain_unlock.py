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

import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time

KEYCHAIN_UNLOCK_MODES = ("cli", "gui", "off")
UNLOCK_COMMAND = "security unlock-keychain"

# `security show-keychain-info` blocks on the GUI unlock prompt when the
# keychain is locked, so a short timeout is the lock signal itself.
_LOCK_PROBE_TIMEOUT_SECONDS = 2.0
# Generous: the user is typing a password. Bounds a `security` that never exits.
_UNLOCK_TIMEOUT_SECONDS = 120.0

# A successful check is trusted for this long, so a long-lived process (an MCP
# server) notices when the keychain re-locks (sleep, idle timeout) instead of
# reverting to the old hang, without probing on every keychain call.
_RECHECK_AFTER_SECONDS = 60.0

# A locked result is remembered just long enough that one command's several keychain
# reads probe (and fail) once instead of once per read.
_LOCKED_CACHE_SECONDS = 5.0

# Security.framework's lock-state query: unlike `security show-keychain-info` it never
# raises the GUI unlock dialog on a locked keychain (SecKeychain.h, kSecUnlockStateStatus).
_CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_SEC_UNLOCK_STATE_STATUS = 1
_SECURITY_FRAMEWORK = "/System/Library/Frameworks/Security.framework/Security"

_checked_at: float | None = None
_checked_lock = threading.Lock()
_locked_at: float | None = None
# Per-process cache: the session type does not change mid-run, and the default
# "gui" mode would otherwise fork `launchctl` on every keychain read.
_gui_available: bool | None = None


class KeychainLockedError(OSError):
    """The login keychain is locked and cannot be unlocked from this session."""


def ensure_unlocked(mode: str) -> None:
    """Make sure the login keychain is usable before keychain I/O, per ``mode``.

    No-op unless the effective mode is ``"cli"`` (``mode == "cli"``, or
    ``"gui"`` with no GUI available) on macOS, and re-checked at most once a
    minute. Raises ``KeychainLockedError`` when the keychain is locked and there
    is no TTY to prompt on (or the unlock fails), and when ``security`` is
    missing on macOS (fail closed: the keyring backend would still reach the
    keychain through the native API, so the lock state cannot be assumed fine).
    """
    global _checked_at, _locked_at
    if not _is_macos() or _recently_checked():
        return
    if not (mode == "cli" or (mode == "gui" and not gui_available())):
        return
    if not _supported():
        raise KeychainLockedError(
            "the macOS `security` tool was not found on PATH, so the keychain lock state "
            "cannot be checked; restore it (it ships in /usr/bin) or set "
            'keychain_unlock = "off"'
        )
    with _checked_lock:
        if _recently_checked():
            return
        if _recently_locked():
            raise KeychainLockedError(_locked_message())
        if keychain_is_locked():
            if not sys.stdin.isatty():
                _locked_at = time.monotonic()
                raise KeychainLockedError(_locked_message())
            if not unlock():
                _locked_at = time.monotonic()
                raise KeychainLockedError("the keychain unlock failed; " + _locked_message())
        _locked_at = None
        _checked_at = time.monotonic()


def explain_if_locked(prefix: str, default: str) -> str:
    """``default``, or ``"<prefix>: <locked-keychain message>"`` right after a locked finding.

    Never probes: it only reflects what this process just found, so an auth error caused
    by a locked keychain says so instead of telling the user to log in again.
    """
    return f"{prefix}: {_locked_message()}" if _recently_locked() else default


def available() -> bool:
    """True on macOS when the ``security`` executable can be found."""
    return _supported()


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


def keychain_is_locked() -> bool:
    """True unless a probe establishes the default (login) keychain is unlocked.

    A probe that cannot run counts as locked, like any other unsuccessful probe;
    only a clean exit means unlocked. False off macOS (nothing to unlock).
    """
    if not _is_macos():
        return False
    return keychain_lock_state() is not False


def keychain_lock_state() -> bool | None:
    """Tri-state lock probe: True locked, False unlocked, None when it cannot be determined.

    Asks Security.framework first (no GUI dialog on a locked keychain); only when that
    cannot answer does it run ``security show-keychain-info``, whose timeout is the lock
    signal. ``None`` covers a non-macOS host and a missing/unrunnable ``security``;
    diagnostics (``doctor``) report it as "unknown" instead of "unlocked".
    """
    if not _is_macos():
        return None
    native = _native_lock_state()
    if native is not None:
        return native
    if not _supported():
        return None
    try:
        proc = subprocess.run(
            ["security", "show-keychain-info"],
            capture_output=True,
            check=False,
            text=True,
            timeout=_LOCK_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return True
    except OSError:
        return None
    # Only a clean exit establishes "unlocked": any other failure is treated as locked.
    return proc.returncode != 0


def unlock() -> bool:
    """Run ``security unlock-keychain`` attached to the terminal; True on success."""
    try:
        proc = subprocess.run(
            ["security", "unlock-keychain"],
            check=False,
            timeout=_UNLOCK_TIMEOUT_SECONDS,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _locked_message() -> str:
    return (
        "the macOS login keychain is locked and this session cannot unlock it "
        "(no terminal to prompt on); "
        f"run `blumkin auth unlock` (or `! {UNLOCK_COMMAND}`) in your own terminal, "
        "then retry"
    )


def _native_lock_state() -> bool | None:
    """True locked / False unlocked / None unknown, from ``SecKeychainGetStatus``.

    Reads the default keychain's state without prompting. Deprecated but present on every
    supported macOS; any failure to load or call it returns ``None`` so the caller falls
    back to the ``security`` probe.
    """
    if not _is_macos():
        return None
    try:
        security = ctypes.CDLL(_SECURITY_FRAMEWORK)
        core_foundation = ctypes.CDLL(_CORE_FOUNDATION)
        copy_default = security.SecKeychainCopyDefault
        copy_default.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        get_status = security.SecKeychainGetStatus
        get_status.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        release = core_foundation.CFRelease
        release.argtypes = [ctypes.c_void_p]
        keychain = ctypes.c_void_p()
        status = ctypes.c_uint32()
        if copy_default(ctypes.byref(keychain)) != 0 or not keychain.value:
            return None
        try:
            if get_status(keychain, ctypes.byref(status)) != 0:
                return None
        finally:
            release(keychain)
    except AttributeError, OSError:
        return None
    return not status.value & _SEC_UNLOCK_STATE_STATUS


def _probe_gui_available() -> bool:
    """``launchctl managername`` is authoritative; SSH env is only the fallback.

    Trusting ``SSH_CONNECTION`` first would misclassify a local GUI session
    that merely inherited it (e.g. a tmux server started over SSH).
    """
    try:
        proc = subprocess.run(
            ["launchctl", "managername"],
            capture_output=True,
            check=False,
            text=True,
            timeout=_LOCK_PROBE_TIMEOUT_SECONDS,
        )
    except OSError, subprocess.TimeoutExpired:
        return not _ssh_env()
    name = proc.stdout.strip()
    if proc.returncode != 0 or not name:
        return not _ssh_env()
    return name == "Aqua"


def _recently_checked() -> bool:
    checked_at = _checked_at
    return checked_at is not None and time.monotonic() - checked_at < _RECHECK_AFTER_SECONDS


def _recently_locked() -> bool:
    locked_at = _locked_at
    return locked_at is not None and time.monotonic() - locked_at < _LOCKED_CACHE_SECONDS


def _reset_for_tests() -> None:
    global _checked_at, _gui_available, _locked_at
    with _checked_lock:
        _checked_at = None
        _locked_at = None
    _gui_available = None


def _ssh_env() -> bool:
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))


def _supported() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None
