"""Keep tests and scratch runs off the real macOS keychain (#759).

A test run or a scratch config directory (pytest temp dirs, a review run's temp dir) must never
write to the operator's real login keychain: every item it left behind was an access prompt
waiting to happen. ``refuse_temporary_config_dir`` is the guard, and ``refuses_directory`` asks
the same question without raising.
"""

from __future__ import annotations

import os
import tempfile

ALLOW_TEMP_ENV = "BLUMKIN_ALLOW_TEMP_KEYCHAIN"

_TEMPORARY_ROOTS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")


class KeychainRefusedError(OSError):
    """A temporary config directory was refused the real keychain."""


def is_temporary(directory: str) -> bool:
    """True for a path under the system temp directories (pytest, scratch and review runs)."""
    try:
        resolved = os.path.realpath(directory)
    except OSError, ValueError:
        return False
    roots = {*_TEMPORARY_ROOTS, os.path.realpath(tempfile.gettempdir())}
    return any(resolved == root or resolved.startswith(root + os.sep) for root in roots)


def refuse_temporary_config_dir(directory: str) -> None:
    """Raise ``KeychainRefusedError`` for a test run or a temporary config directory.

    ``BLUMKIN_ALLOW_TEMP_KEYCHAIN=1`` lifts it for someone who really means it, and a live test
    run (``BLUMKIN_LIVE=1``) is exempt, since it is meant to use the real keychain with a real
    config directory. For ``token_storage = "auto"`` the secret store never gets as far as
    asking (see ``_backend_for``) and uses its file backend; a profile pinned to ``"keyring"``
    gets this error, reported as a secret write failure like any other keychain failure.
    """
    if os.environ.get(ALLOW_TEMP_ENV) == "1":
        return
    in_test_run = os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("BLUMKIN_LIVE") != "1"
    if in_test_run or is_temporary(directory):
        raise KeychainRefusedError(
            f"refusing to use the real macOS keychain from a test run or a temporary config "
            f"directory ({directory}); set {ALLOW_TEMP_ENV}=1 if that is intended"
        )


def refuses_directory(directory: str) -> bool:
    """True when ``refuse_temporary_config_dir`` would refuse ``directory`` (no exception)."""
    try:
        refuse_temporary_config_dir(directory)
    except KeychainRefusedError:
        return True
    return False
