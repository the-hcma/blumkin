"""Find and remove unused keychain items, and keep tests off the real keychain (#759).

Every keychain item is an access prompt waiting to happen, and the stale ones are never used.
An investigation of the prompt storm found eleven items for two live secrets: the rest were an
old per-kind naming scheme, and items written under throwaway config directories (pytest temp
dirs, scratch runs of a review) that nothing would ever clean up.

- ``stale_items`` lists the items under the ``blumkin`` service that nothing uses. It reads only
  attribute names from ``security dump-keychain`` (never a secret value).
- ``remove_item`` deletes one with ``security delete-generic-password``.
- ``refuse_temporary_config_dir`` is the guard that stops a test or a scratch run from writing to
  the real keychain in the first place.

A stale item is one whose account is not a JSON list (the old ``profile:kind`` scheme, which
nothing has read since the JSON naming replaced it and which never migrates), or whose config
directory is a temporary directory. An item of a config directory that is merely missing is not
listed: an unmounted volume or a moved directory still owns its secrets.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass

KEYCHAIN_SERVICE = "blumkin"
ALLOW_TEMP_ENV = "BLUMKIN_ALLOW_TEMP_KEYCHAIN"

_DUMP_TIMEOUT_SECONDS = 30.0
_ACCOUNT_LINE = re.compile(r'^\s*"acct"<blob>="(.*)"\s*$')
# A value with unprintable characters is dumped as `0x<hex>  "<escaped text>"` instead.
_ACCOUNT_HEX_LINE = re.compile(r'^\s*"acct"<blob>=0x([0-9A-Fa-f]+)(?:\s+".*")?\s*$')
_KEYCHAIN_LINE = re.compile(r'^keychain: "(.*)"\s*$')
_SERVICE_LINE = re.compile(r'^\s*"svce"<blob>="(.*)"\s*$')
_TEMPORARY_ROOTS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")


@dataclass(frozen=True, slots=True)
class StaleItem:
    """A keychain account nothing uses, and why.

    ``keychain`` is the file the item lives in, so a deletion targets that item and not the first
    one of the same name in another keychain on the search list.
    """

    account: str
    reason: str
    keychain: str | None = None


class KeychainRefusedError(OSError):
    """A temporary config directory was refused the real keychain."""


def classify(account: str) -> str | None:
    """Why ``account`` is stale, or None when it is in use (or not blumkin's to judge)."""
    try:
        parts = json.loads(account)
    except ValueError:
        return "old per-kind naming (profile:kind)"
    if not (isinstance(parts, list) and parts and all(isinstance(p, str) for p in parts)):
        return "old per-kind naming (profile:kind)"
    directory = parts[0]
    if is_temporary(directory) and os.environ.get(ALLOW_TEMP_ENV) != "1":
        # With the override on, a temporary config directory is used on purpose (a live vault
        # under /tmp), so a temporary path does not show the item is unused.
        return "written under a temporary config directory"
    return None


def is_temporary(directory: str) -> bool:
    """True for a path under the system temp directories (pytest, scratch and review runs)."""
    try:
        resolved = os.path.realpath(directory)
    except OSError, ValueError:
        return False
    roots = {*_TEMPORARY_ROOTS, os.path.realpath(tempfile.gettempdir())}
    return any(resolved == root or resolved.startswith(root + os.sep) for root in roots)


def list_accounts() -> list[tuple[str, str | None]] | None:
    """(account, keychain file) of every ``blumkin`` item; None when it cannot be listed.

    Only attribute names are read. ``dump-keychain`` without ``-d`` never touches a secret value.
    """
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            ["security", "dump-keychain"],
            capture_output=True,
            check=False,
            text=True,
            timeout=_DUMP_TIMEOUT_SECONDS,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return _accounts_from_dump(proc.stdout)


def refuses_directory(directory: str) -> bool:
    """True when ``refuse_temporary_config_dir`` would refuse ``directory`` (no exception)."""
    try:
        refuse_temporary_config_dir(directory)
    except KeychainRefusedError:
        return True
    return False


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


def remove_item(account: str, keychain: str | None = None) -> bool:
    """Delete one ``blumkin`` item from ``keychain`` (default: the first match on the search
    list); True when it is gone afterwards."""
    command = ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account]
    if keychain:
        command.append(keychain)
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=_DUMP_TIMEOUT_SECONDS,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def stale_items() -> list[StaleItem] | None:
    """The ``blumkin`` items nothing uses; None when the keychain cannot be listed."""
    accounts = list_accounts()
    if accounts is None:
        return None
    found = []
    for account, keychain in sorted(set(accounts), key=lambda pair: (pair[0], pair[1] or "")):
        reason = classify(account)
        if reason is not None:
            found.append(StaleItem(account=account, reason=reason, keychain=keychain))
    return found


def _accounts_from_dump(text: str) -> list[tuple[str, str | None]]:
    accounts: list[tuple[str, str | None]] = []
    account: str | None = None
    keychain: str | None = None
    for line in text.splitlines():
        started = _KEYCHAIN_LINE.match(line)
        if started:
            account = None  # a new record starts
            keychain = started.group(1)
            continue
        found = _ACCOUNT_LINE.match(line)
        if found:
            account = found.group(1)
            continue
        hexed = _ACCOUNT_HEX_LINE.match(line)
        if hexed:
            try:
                account = bytes.fromhex(hexed.group(1)).decode("utf-8")
            except ValueError, UnicodeDecodeError:
                account = None
            continue
        service = _SERVICE_LINE.match(line)
        if service and service.group(1) == KEYCHAIN_SERVICE and account is not None:
            accounts.append((account, keychain))
    return accounts
