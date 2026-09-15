"""Read/write blumkin's small on-disk secrets, preferring the OS keychain.

The MSAL token cache, Entra auth record, and Google token JSON were each a
plain ``0600`` file under ``~/.config/blumkin/profiles/<name>/`` (see
``blumkin.auth`` / ``blumkin.providers.google_auth``). That protects them only
by filesystem permission — no encryption at rest, no OS-native access control.
This module adds an OS-keychain-backed alternative via the optional
``keychain`` extra (``pipx install 'blumkin[keychain]'``, which pulls in the
``keyring`` package - macOS Keychain, Windows Credential Manager, Linux Secret
Service), selected per profile by ``token_storage`` in ``config.toml``:

- ``"auto"`` (default): prefer the keychain when a real backend is usable at
  runtime, silently fall back to the file for most write trouble (headless
  Linux with no Secret Service running, the ``keychain`` extra not
  installed, a keychain write failing at runtime, etc.) — a non-interactive
  agent shell must never hang or fail because no keychain backend can
  service it. It only raises if that fallback write's own cleanup of a
  stale keyring entry then fails too, since that would otherwise leave the
  two backends silently disagreeing.
- ``"keyring"``: same preference, but warn once (not on every call) if no
  usable backend is found, since the operator explicitly asked for one; a
  write failure of any kind raises rather than silently downgrading to the
  file.
- ``"file"``: always use the file, even if a keyring backend is available.

A legacy plaintext file is migrated into the keyring transparently the first
time it is read under a keyring-preferring config: written into the keyring,
then removed, so a secret never ends up duplicated across both backends.
Tracked in issue #287; a further macOS-only access-control layer (Touch
ID / passphrase-cache style re-auth) is a separate, follow-up enhancement.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Literal

from blumkin.config import BlumkinConfig
from blumkin.output import emit_warning

SecretKind = Literal["auth_record", "google_token", "token_cache"]


class SecretWriteError(OSError):
    """Failed to persist a secret (symlink at the path, keyring backend error, etc.)."""


def active_backend(cfg: BlumkinConfig, kind: SecretKind) -> str:
    """Backend name ("keyring" or "file") actually holding ``kind`` right now.

    Reports where the secret really is, not just this profile's preference:
    ``token_storage = "auto"``/``"keyring"`` can still end up on the file on
    *every* write for reasons ``_backend_for`` can't see from config alone -
    a serialized MSAL token cache exceeding a backend's own size cap (e.g.
    Windows Credential Manager's 2560-byte ``CRED_MAX_CREDENTIAL_BLOB_SIZE``),
    or a keychain that only fails at write time. Reporting the static
    preference in that case would have `doctor`/`profiles list` permanently
    claim "keyring" for a profile that has in fact always fallen back to the
    file (issue #287 review). When neither backend has anything stored yet
    (a brand new, never-logged-in profile) there is nothing on disk to
    disagree with the preference, so the preference is reported.
    """
    if _backend_for(cfg) == "file":
        return "file"
    keyring = _keyring_module()
    if keyring is None:
        return "file"
    try:
        account = _keyring_account(cfg, kind)
        if _call_keyring_with_timeout(keyring.get_password, _KEYRING_SERVICE, account) is not None:
            return "keyring"
    except Exception:
        pass
    if _file_path(cfg, kind).is_file():
        return "file"
    return "keyring"


def delete(cfg: BlumkinConfig, kind: SecretKind) -> None:
    """Remove the secret for ``kind`` from both backends, wherever it lives.

    A keyring backend that confirms nothing is stored for this account is
    left alone (there is nothing to report); a backend that *does* have an
    entry, or that cannot even be probed for one, still gets a
    ``delete_password`` attempt - a probe failure must not look like "nothing
    stored" and skip deletion, since the entry could very well still be there
    (issue #287 review). A backend that fails to delete an entry *confirmed*
    to exist raises ``SecretWriteError`` instead of silently pretending the
    logout succeeded. When existence could not be confirmed either way and
    the delete attempt itself reports "nothing to delete", that is treated as
    resolved rather than a failure - keyring's own delete APIs use the same
    exception for "not found" and other failures, so re-raising here would
    turn an ordinary already-logged-out profile into a reported error on
    every logout call (issue #287 review).
    """
    path = _file_path(cfg, kind)
    if path.is_file():
        try:
            path.unlink()
        except OSError as exc:
            # Every other file mutation in this module already converts a
            # failed OSError into SecretWriteError; this unlink was the one
            # exception, so a read-only mount / permission mismatch (a file
            # created by an earlier `sudo blumkin auth login`, etc.) escaped
            # `auth logout` as a bare, unclassified traceback instead of the
            # documented secret_write_failed error (issue #287 review).
            raise SecretWriteError(f"cannot delete {kind} file {path}: {exc}") from exc
    keyring = _keyring_module()
    if keyring is None:
        return
    account = _keyring_account(cfg, kind)
    probed_existence: bool | None
    try:
        probed_existence = (
            _call_keyring_with_timeout(keyring.get_password, _KEYRING_SERVICE, account) is not None
        )
    except Exception:
        # Can't tell whether an entry exists - fall through to a real delete
        # attempt below rather than assume it's gone.
        probed_existence = None
    if probed_existence is False:
        return
    try:
        _call_keyring_with_timeout(keyring.delete_password, _KEYRING_SERVICE, account)
    except Exception as exc:
        if probed_existence is None and _is_not_found(keyring, exc):
            # Existence couldn't be probed above, but the backend's own
            # delete call now confirms there was nothing there - resolved.
            return
        raise SecretWriteError(f"cannot delete {kind} from the OS keychain: {exc}") from exc


def _is_not_found(keyring_module: Any, exc: Exception) -> bool:
    """True when ``exc`` is a keyring "delete" failure, not a timeout or other error."""
    errors = getattr(keyring_module, "errors", None)
    not_found_type = getattr(errors, "PasswordDeleteError", None)
    return not_found_type is not None and isinstance(exc, not_found_type)


def exists(cfg: BlumkinConfig, kind: SecretKind) -> bool:
    """True when a secret is stored for ``kind`` in the active backend for this profile.

    Must agree with ``read_text``/``write_text`` on which backend is
    authoritative: consulting the keyring unconditionally here would report a
    stale keychain leftover as present for a profile explicitly pinned to
    ``token_storage = "file"``, even though ``read_text`` (honoring
    ``_backend_for``) can never see it - `doctor`/`profiles list` would show
    ``auth_present: true`` for a profile every real command then fails to
    authenticate with (issue #287 review).
    """
    if _backend_for(cfg) == "file":
        return _file_path(cfg, kind).is_file()
    keyring = _keyring_module()
    if keyring is None:
        # _backend_for only returns "keyring" when a real backend is usable.
        return False
    try:
        account = _keyring_account(cfg, kind)
        if _call_keyring_with_timeout(keyring.get_password, _KEYRING_SERVICE, account) is not None:
            return True
    except Exception:
        pass
    # A legacy file not yet migrated is still a real, readable secret.
    return _file_path(cfg, kind).is_file()


def read_text(cfg: BlumkinConfig, kind: SecretKind) -> str | None:
    """Read the secret for ``kind``, migrating a legacy file into the keyring once."""
    path = _file_path(cfg, kind)
    if _backend_for(cfg) == "file":
        if not path.is_file():
            return None
        try:
            return path.read_text()
        except OSError:
            return None
    keyring = _keyring_module()
    if keyring is None:
        # _backend_for only returns "keyring" when a real backend is usable.
        return None
    account = _keyring_account(cfg, kind)
    try:
        value = _call_keyring_with_timeout(keyring.get_password, _KEYRING_SERVICE, account)
    except Exception:
        value = None
    if value is not None:
        # The keyring is authoritative once it holds a value. A plaintext
        # file can still be sitting alongside it - e.g. a prior "auto" write
        # fell back to the file, but the best-effort stale-keyring cleanup
        # that write attempted didn't actually land (a delayed keychain
        # unlock, a second process racing the cleanup, etc.) - and it must
        # not linger forever once the keyring is reachable again, or the
        # file's on-disk plaintext copy outlives its purpose (issue #287
        # review). Best-effort: a cleanup failure here does not change what
        # is returned - the keyring value stays authoritative either way.
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
        return value
    if not path.is_file():
        return None
    try:
        legacy = path.read_text()
    except OSError:
        return None
    try:
        _call_keyring_with_timeout(keyring.set_password, _KEYRING_SERVICE, account, legacy)
    except Exception:
        # Keychain write failed - keep serving the file untouched rather than
        # lose the secret.
        return legacy
    try:
        path.unlink()
    except OSError:
        # Migration copied the secret into the keyring but couldn't remove the
        # plaintext original - roll the keyring copy back so the file stays
        # authoritative and the next read retries the migration, instead of
        # reporting success while a stale plaintext copy lingers untracked
        # (issue #287 review: a partial migration must not look complete).
        # Best-effort: both backends already hold the same value at this
        # point, so a rollback failure here cannot cause the two backends to
        # disagree (unlike write_text's stale-entry cleanup below) - it can
        # only leave migration incomplete, safely retried on the next read.
        try:
            _call_keyring_with_timeout(keyring.delete_password, _KEYRING_SERVICE, account)
        except Exception:
            pass
        return legacy
    return legacy


def write_text(cfg: BlumkinConfig, kind: SecretKind, text: str) -> None:
    """Persist the secret for ``kind`` to the active backend for this profile."""
    if _backend_for(cfg) == "keyring":
        keyring = _keyring_module()
        if keyring is None:
            raise SecretWriteError(f"cannot write {kind}: no usable keyring backend")
        account = _keyring_account(cfg, kind)
        try:
            _call_keyring_with_timeout(keyring.set_password, _KEYRING_SERVICE, account, text)
        except Exception as exc:
            if cfg.token_storage != "auto":
                # The operator explicitly asked for "keyring" - surface the
                # failure loudly rather than silently downgrade to the file.
                raise SecretWriteError(f"cannot write {kind} to the OS keychain: {exc}") from exc
            # "auto" promises a silent fallback to the file on any backend
            # trouble (locked keychain over SSH, access denied, ...), not
            # just when no backend is installed at all (issue #287 review).
            # A *stale* keyring entry from a prior successful write must not
            # be left behind: read_text() always prefers a present keyring
            # value over the file, so the value we are about to write to the
            # file would otherwise be permanently unreachable (issue #287
            # review). Unlike the migration rollback in read_text() (where
            # both backends already agree), a failure to remove this stale
            # entry leaves the two backends genuinely disagreeing - the
            # keyring still has the *old* value, the file has the *new* one
            # - so it is surfaced as a failure instead of swallowed, even
            # though we are inside "auto": a loud, rare double-fault (keyring
            # write failed *and* keyring cleanup failed) is safer than a
            # quiet, indefinite split-brain between the two backends (issue
            # #287 review).
            try:
                _call_keyring_with_timeout(keyring.delete_password, _KEYRING_SERVICE, account)
            except Exception as cleanup_exc:
                if not _is_not_found(keyring, cleanup_exc):
                    raise SecretWriteError(
                        f"cannot write {kind}: the OS keychain write failed ({exc}) and "
                        f"the stale keychain entry left behind could not be removed "
                        f"({cleanup_exc}) - the file and keychain backends now disagree"
                    ) from cleanup_exc
        else:
            return
    path = _file_path(cfg, kind)
    _ensure_secret_dir(path.parent, stop_at=cfg.config_dir)
    _write_file_secret(path, text)


_KEYRING_SERVICE = "blumkin"


def _backend_for(cfg: BlumkinConfig) -> Literal["file", "keyring"]:
    if cfg.token_storage == "file":
        return "file"
    if _keyring_module() is not None:
        return "keyring"
    if cfg.token_storage == "keyring":
        _warn_keyring_unusable_once(cfg)
    return "file"


def _ensure_secret_dir(directory: Path, *, stop_at: Path) -> None:
    """Create ``directory`` at 0700; best-effort tighten if it already existed looser.

    Mode-setting is optional: SMB/FUSE mounts may reject ``chmod``. A symlinked
    config dir or ``profiles/`` segment is refused — ``mkdir(parents=True)``
    follows intermediate symlinks. The walk stops at ``stop_at`` (the config
    directory) so platform symlinks such as macOS ``/var`` → ``/private/var``
    do not break TMPDIR / XDG layouts. Intermediate dirs from ``stop_at``
    through the leaf are tightened to 0700 when the filesystem allows it.
    """
    if directory != stop_at and stop_at not in directory.parents:
        raise SecretWriteError(f"secret dir {directory} is outside config dir {stop_at}")
    _refuse_symlinked_path_components(directory, stop_at=stop_at)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    current = directory
    chain: list[Path] = []
    while True:
        chain.append(current)
        if current == stop_at:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    for path in reversed(chain):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def _file_path(cfg: BlumkinConfig, kind: SecretKind) -> Path:
    if kind == "auth_record":
        return cfg.auth_record_path
    if kind == "google_token":
        return cfg.google_token_path
    return cfg.token_cache_path


def _keyring_account(cfg: BlumkinConfig, kind: SecretKind) -> str:
    """Keyring account name, namespaced like the file backend's own path.

    The file backend identifies a secret by its full path (``config_dir``
    included), so two config dirs (``BLUMKIN_CONFIG_DIR`` / ``XDG_CONFIG_HOME``
    - e.g. a sandbox/CI tenant vs. the primary one) that both happen to have a
    profile named the same must not collide on one keychain item (issue #287
    review). JSON-encoded so ``:`` inside a config dir path or profile name
    cannot be used to construct a colliding account string.
    """
    return json.dumps([str(cfg.config_dir), cfg.profile, kind], separators=(",", ":"))


_keyring_checked = False
_keyring_lock = threading.Lock()
_keyring_mod: Any | None = None


_KEYRING_IO_TIMEOUT_SECONDS = 5.0
_KEYRING_PROBE_TIMEOUT_SECONDS = 2.0


def _call_keyring_with_timeout(
    func: Any, *args: Any, timeout: float = _KEYRING_IO_TIMEOUT_SECONDS
) -> Any:
    """Call a keyring backend method on a bounded daemon thread.

    Only backend *selection* (``_probe_keyring_backend``) used to be
    time-bounded; every actual read/write/delete call went straight to the
    backend uncapped. A locked Linux Secret Service (or a keychain daemon
    prompting for interactive unlock) can hang a synchronous call
    indefinitely, and a non-interactive agent shell must not block forever on
    a single keychain call any more than it should on backend selection
    (issue #287 review). Run on a daemon thread purely to bound wall-clock
    time: a call that never returns is abandoned rather than joined, so it
    cannot block process exit. Raises ``TimeoutError`` on timeout, or
    re-raises whatever ``func`` raised - both are treated by callers the same
    as any other backend failure.
    """
    result: dict[str, Any] = {}

    def _run() -> None:
        try:
            result["value"] = func(*args)
        except Exception as exc:  # noqa: BLE001 - re-raised verbatim on the caller's thread
            result["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise TimeoutError(f"keyring backend call timed out after {timeout}s")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _keyring_module() -> Any | None:
    """Import ``keyring`` lazily; ``None`` when unavailable or unusable.

    Cached after the first call — the active backend cannot change mid
    process. A backend that resolves to ``keyring.backends.fail`` (no real
    backend found: headless Linux with no Secret Service, etc.) counts as
    unavailable, same as the extra not being installed, so callers never
    block a non-interactive agent shell on a backend that cannot service it.

    Guarded by a lock so ``_keyring_checked`` only ever flips to ``True``
    after ``_keyring_mod`` has been fully resolved - setting the flag first
    and populating the module after let a concurrent caller observe
    "checked" but still see the pre-probe (``None``) module and wrongly
    conclude no backend is usable (issue #287 review).
    """
    global _keyring_mod, _keyring_checked
    if _keyring_checked:
        return _keyring_mod
    with _keyring_lock:
        if _keyring_checked:
            return _keyring_mod
        try:
            import keyring
        except ImportError:
            _keyring_mod = None
            _keyring_checked = True
            return None
        if not _probe_keyring_backend(keyring):
            _keyring_mod = None
            _keyring_checked = True
            return None
        _keyring_mod = keyring
        _keyring_checked = True
        return _keyring_mod


def _probe_keyring_backend(keyring_module: Any) -> bool:
    """True when ``get_keyring()`` resolves to a real (non-fail) backend.

    Bounded to ``_KEYRING_PROBE_TIMEOUT_SECONDS``: backend selection (e.g.
    keyring 25.x's ``SecretService.Keyring.priority``) can make a synchronous,
    un-timed D-Bus availability call, and a headless or misconfigured D-Bus
    session must not hang a login or a `doctor` check (issue #287 review). Run
    on a daemon thread purely to bound wall-clock time, not for concurrency -
    a probe that never returns is abandoned rather than joined, so it cannot
    block process exit. The probe itself only asks which backend would be
    used; it does not unlock a collection or otherwise prompt.
    """
    result: dict[str, Any] = {}

    def _probe() -> None:
        try:
            result["backend"] = keyring_module.get_keyring()
        except Exception as exc:  # noqa: BLE001 - any backend failure means "unusable"
            result["error"] = exc

    thread = threading.Thread(target=_probe, daemon=True)
    thread.start()
    thread.join(timeout=_KEYRING_PROBE_TIMEOUT_SECONDS)
    if thread.is_alive() or "backend" not in result:
        return False
    return not type(result["backend"]).__module__.startswith("keyring.backends.fail")


def _refuse_symlinked_path_components(directory: Path, *, stop_at: Path) -> None:
    """Refuse ``directory`` or ancestors down to ``stop_at`` that are symlinks.

    Caller must ensure ``stop_at`` is ``directory`` or an ancestor.
    """
    current = directory
    while True:
        if current.is_symlink():
            raise SecretWriteError(f"cannot use symlinked config dir {current}")
        if current == stop_at:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent


def _warn_keyring_unusable_once(cfg: BlumkinConfig) -> None:
    """Warn at most once per profile per process when the keychain can't be honored."""
    if cfg.profile in _warned_unusable_profiles:
        return
    _warned_unusable_profiles.add(cfg.profile)
    emit_warning(
        f"token_storage is 'keyring' for profile {cfg.profile!r}, but no usable OS "
        "keychain backend was found (install the 'keychain' extra: "
        "pipx install 'blumkin[keychain]', or check that a Secret Service / "
        "keychain daemon is running) - falling back to the file-based cache."
    )


_warned_unusable_profiles: set[str] = set()


def _write_all(fd: int, data: bytes) -> None:
    """``os.write`` can write fewer bytes than given; loop until all are written.

    Without this, a short write reports success while silently truncating the
    persisted credential - later reads of the truncated JSON fail to parse and
    look like "not signed in" rather than a corrupted cache (issue #287
    review).
    """
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError("secret file write made no progress")
        view = view[written:]


def _write_file_secret(path: Path, text: str) -> None:
    """Write sensitive text at 0600, tightening the mode when the file already exists.

    On POSIX, ``O_CREAT`` mode is ignored when the path already exists, so a
    leftover world-readable cache would keep leaking tokens on every rewrite
    without an explicit ``fchmod`` to ``0600``. ``O_NOFOLLOW`` (when
    available) refuses a symlink swap at the path. Mode-setting is
    best-effort so chmod-less filesystems still persist the cache after
    ``O_TRUNC``.

    On Windows, ``os.fchmod`` is unavailable and ``os.chmod`` only toggles
    the read-only bit (ACLs govern access). The post-close ``chmod`` there
    avoids ``AttributeError`` so login/cache persistence still works; it does
    not claim a Unix ``0600`` guarantee.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise SecretWriteError(f"cannot write secret file {path}: {exc}") from exc
    try:
        try:
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
            _write_all(fd, text.encode())
        except OSError as exc:
            raise SecretWriteError(f"cannot write secret file {path}: {exc}") from exc
    finally:
        try:
            os.close(fd)
        except OSError as exc:
            raise SecretWriteError(f"cannot write secret file {path}: {exc}") from exc
    if not hasattr(os, "fchmod"):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
