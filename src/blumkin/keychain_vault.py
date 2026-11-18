"""One keychain item per config directory instead of one per secret (issue #759).

macOS keychain items each carry their own access list, and an ad-hoc signed Python
(Homebrew's) is a *new application* to every item whenever its binary changes, so N items
meant N "Python wants to use your confidential information" dialogs, per process. This module
wraps the real ``keyring`` module so that every secret of one config directory lives in a
single item (service ``blumkin``, account ``["<config dir>","vault"]``) whose value is a JSON
object mapping the old per-secret account name to its text. ``secret_store`` and
``app_secrets`` keep calling ``get_password`` / ``set_password`` / ``delete_password`` with the
account names they always used; nothing above this layer knows the storage changed.

- **Nothing from an older layout is read.** There is no migration of per-secret items written
  by earlier versions: an account that is not in the vault is simply absent, so a miss never
  touches the keychain a second time. Deleting the last secret removes the item, so an
  uninstall leaves nothing behind.
- **Cross-process writes.** One item shared by unrelated writers turns a write into
  read-modify-write, so every write runs under an exclusive
  ``flock`` on ``<config dir>/.keychain.lock``. A second process waits and then re-reads, so
  it neither drops the first one's change nor stacks a second keychain request.
- **At most one read per run.** The parsed item is cached in memory for a few seconds, so a
  command that reads the token cache, the auth record and the client id touches the keychain
  once. The cache is short-lived because a long-lived process (``mcp serve``) must still see a
  token another process refreshed.
- **Scope.** macOS only: that is where the prompt storm is, and other platforms' credential
  stores cap one item's size far below a multi-profile vault.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from blumkin import keychain_hygiene

try:  # POSIX only; Windows never reaches the vault (see ``vault_applies``).
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows only
    fcntl = None  # type: ignore[assignment]

_CACHE_TTL_SECONDS = 5.0
_LOCK_FILENAME = ".keychain.lock"
_LOCK_POLL_SECONDS = 0.05
_LOCK_TIMEOUT_SECONDS = 30.0
_VAULT_SLOT = "vault"

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()
_generation: dict[str, int] = {}
_held = threading.local()
_holders: dict[str, list[Any]] = {}
_thread_locks: dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()


class VaultKeyring:
    """A ``keyring``-shaped facade that stores every account of a config dir in one item."""

    def __init__(self, backend: Any, *, refuse_temporary: bool = False) -> None:
        self._backend = backend
        self._refuse_temporary = refuse_temporary
        self.errors = getattr(backend, "errors", None)

    def __getattr__(self, name: str) -> Any:
        # Anything this facade does not override (``get_keyring``, ``errors``) is the real thing.
        return getattr(self._backend, name)

    def delete_password(self, service: str, account: str) -> None:
        directory = _config_dir_of(account)
        if directory is None:
            self._backend.delete_password(service, account)
            return
        self._guard(directory)
        found = False
        with _locked(directory, service):
            vault = self._load(service, directory, fresh=True)
            if account in vault:
                del vault[account]
                found = True
            self._store(service, directory, vault)
        if not found:
            self._raise_not_found(service, account)

    def get_password(self, service: str, account: str) -> str | None:
        directory = _config_dir_of(account)
        if directory is None:
            return self._backend.get_password(service, account)
        self._guard(directory)
        vault = self._load(service, directory)
        value = vault.get(account)
        return value if isinstance(value, str) else None

    def set_password(self, service: str, account: str, password: str) -> None:
        directory = _config_dir_of(account)
        if directory is None:
            self._backend.set_password(service, account, password)
            return
        self._guard(directory)
        with _locked(directory, service):
            vault = self._load(service, directory, fresh=True)
            vault[account] = password
            self._store(service, directory, vault)

    def refuses(self, directory: str) -> bool:
        """True when this facade would refuse ``directory`` the real keychain (temp dir, test)."""
        return self._refuse_temporary and keychain_hygiene.refuses_directory(directory)

    def _guard(self, directory: str) -> None:
        """Keep a test run or a scratch config directory off the real keychain (#759)."""
        if self._refuse_temporary:
            keychain_hygiene.refuse_temporary_config_dir(directory)

    def _load(self, service: str, directory: str, *, fresh: bool = False) -> dict[str, Any]:
        vault_account = _vault_account(directory)
        # Inside a transaction every read is fresh: its caller is about to write back.
        fresh = fresh or _holds_lock(service, directory)
        if not fresh:
            with _cache_lock:
                hit = _cache.get(vault_account)
            if hit is not None and time.monotonic() - hit[0] < _CACHE_TTL_SECONDS:
                return dict(hit[1])
        with _cache_lock:
            generation = _generation.get(vault_account, 0)
        raw = self._backend.get_password(service, vault_account)
        vault = _parse(raw)
        with _cache_lock:
            # Published only if no mutation landed meanwhile (a slow read must not resurrect
            # a deleted credential), and stamped after the read so a dialog cannot eat the TTL.
            if _generation.get(vault_account, 0) == generation:
                _cache[vault_account] = (time.monotonic(), dict(vault))
        return vault

    def _raise_not_found(self, service: str, account: str) -> None:
        """Raise the backend's own "nothing to delete" error without touching the backend.

        Asking the backend to delete ``account`` would remove a standalone item of that name, a
        leftover of an older layout that this facade deliberately never reads or removes.
        """
        not_found = getattr(self.errors, "PasswordDeleteError", None)
        if not (isinstance(not_found, type) and issubclass(not_found, Exception)):
            not_found = KeyError
        raise not_found(f"Item not found: {service}/{account}")

    def _store(self, service: str, directory: str, vault: dict[str, Any]) -> None:
        vault_account = _vault_account(directory)
        # Once the last secret is deleted the item goes, so an uninstall leaves nothing behind.
        if not any(isinstance(v, str) for v in vault.values()):
            if self._backend.get_password(service, vault_account) is not None:
                self._backend.delete_password(service, vault_account)
            with _cache_lock:
                _generation[vault_account] = _generation.get(vault_account, 0) + 1
                _cache[vault_account] = (time.monotonic(), dict(vault))
            return
        self._backend.set_password(service, vault_account, json.dumps(vault, separators=(",", ":")))
        with _cache_lock:
            _generation[vault_account] = _generation.get(vault_account, 0) + 1
            _cache[vault_account] = (time.monotonic(), dict(vault))

    @contextmanager
    def transaction(self, service: str, account: str) -> Iterator[None]:
        """Hold the vault lock across a caller's own read-modify-write of one account.

        Reads inside it bypass the cache, so a value another process changed meanwhile is
        never written back stale.
        """
        directory = _config_dir_of(account)
        if directory is None:
            yield
            return
        with _locked(directory, service):
            yield


def adopt_held(tokens: frozenset[object]) -> None:
    """Let this thread act for a caller that holds the vault lock and is blocked on it.

    ``secret_store`` runs each keyring call on a helper thread to bound its time. The helper
    counts as holding a lock only for as long as that very acquisition is still in force: once
    the caller releases it (say, after a timeout) the token is stale, and an abandoned helper
    takes the lock like any other writer.
    """
    _held.tokens = set(tokens)


def clear_cache() -> None:
    """Forget every cached vault (tests, and a caller that just changed the keychain itself)."""
    with _cache_lock:
        _cache.clear()


def current_held() -> frozenset[object]:
    """The lock acquisitions this thread holds, for ``adopt_held`` on a helper thread."""
    return frozenset(_held_tokens())


def vault_applies() -> bool:
    """True where secrets are consolidated into one item per config directory."""
    return sys.platform == "darwin"


def _acquire(handle: Any) -> None:
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[union-attr]
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "another blumkin process has held the keychain lock for "
                    f"{_LOCK_TIMEOUT_SECONDS:.0f}s; try again in a moment"
                ) from None
            time.sleep(_LOCK_POLL_SECONDS)


def _config_dir_of(account: str) -> str | None:
    """The config directory an account name was namespaced with, or None for a foreign name."""
    try:
        parts = json.loads(account)
    except TypeError, ValueError:
        return None
    if isinstance(parts, list) and len(parts) >= 3 and all(isinstance(p, str) for p in parts):
        return parts[0]
    return None


def _held_tokens() -> set[object]:
    tokens = getattr(_held, "tokens", None)
    if tokens is None:
        tokens = _held.tokens = set()
    return tokens


def _holds(key: str) -> bool:
    entry = _holders.get(key)
    return entry is not None and entry[0] in _held_tokens()


def _holds_lock(service: str, directory: str) -> bool:
    return _holds(f"{service}:{directory}")


@contextmanager
def _locked(directory: str, service: str) -> Iterator[None]:
    """Exclusive, cross-process lock for one config dir's vault (and an in-process one).

    Reentrant for whoever holds it, helper threads acting for the holder included: a
    transaction that calls into the facade again must not wait on its own flock (a second open
    of the lock file is a different lock holder).
    """
    key = f"{service}:{directory}"
    entry = _holders.get(key)
    if entry is not None and entry[0] in _held_tokens():
        entry[1] += 1
        try:
            yield
        finally:
            entry[1] -= 1
        return
    with _thread_locks_guard:
        thread_lock = _thread_locks.setdefault(key, threading.RLock())
    if not thread_lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
        raise TimeoutError(
            f"another thread has held the keychain lock for {_LOCK_TIMEOUT_SECONDS:.0f}s"
        )
    try:
        handle = _open_lock_file(directory)
        if handle is None and fcntl is not None:
            # Writing without the lock could drop another process's change.
            raise OSError(f"cannot create the keychain lock file in {directory}")
        try:
            if handle is not None:
                _acquire(handle)
            token = object()
            _holders[key] = [token, 1]
            _held_tokens().add(token)
            try:
                yield
            finally:
                _holders.pop(key, None)
                _held_tokens().discard(token)
        finally:
            if handle is not None:
                try:
                    fcntl.flock(handle, fcntl.LOCK_UN)  # type: ignore[union-attr]
                finally:
                    handle.close()
    finally:
        thread_lock.release()


def _open_lock_file(directory: str) -> Any | None:
    """Open (creating, ``0600``) the lock file; None when there is nowhere to put it."""
    if fcntl is None:
        return None
    path = Path(directory)
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd = os.open(path / _LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    return os.fdopen(fd, "r+")


def _parse(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    data.pop("_legacy_checked", None)  # bookkeeping an earlier release wrote; no longer used
    return data


def _vault_account(directory: str) -> str:
    return json.dumps([directory, _VAULT_SLOT], separators=(",", ":"))
