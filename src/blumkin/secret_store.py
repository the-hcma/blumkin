"""Read/write blumkin's small on-disk secrets, preferring the OS keychain.

The MSAL token cache, Entra auth record, and Google token JSON were each a
plain ``0600`` file under ``~/.config/blumkin/profiles/<name>/`` (see
``blumkin.auth`` / ``blumkin.providers.google_auth``). That protects them only
by filesystem permission — no encryption at rest, no OS-native access control.
This module adds an OS-keychain-backed alternative via the optional
``keyring`` extra (macOS Keychain, Windows Credential Manager, Linux Secret
Service), selected per profile by ``token_storage`` in ``config.toml``:

- ``"auto"`` (default): prefer keyring when a real backend is usable at
  runtime, silently fall back to the file otherwise (headless Linux with no
  Secret Service running, the ``keyring`` extra not installed, etc.) — a
  non-interactive agent shell must never hang or fail because no keychain
  backend can service it.
- ``"keyring"``: same preference, but warn once (not on every call) if no
  usable backend is found, since the operator explicitly asked for one.
- ``"file"``: always use the file, even if a keyring backend is available.

A legacy plaintext file is migrated into the keyring transparently the first
time it is read under a keyring-preferring config: written into the keyring,
then removed, so a secret never ends up duplicated across both backends.
Tracked in issue #287; a further macOS-only access-control layer (Touch
ID / passphrase-cache style re-auth) is a separate, follow-up enhancement.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from blumkin.config import BlumkinConfig
from blumkin.output import emit_warning

SecretKind = Literal["auth_record", "google_token", "token_cache"]


class SecretWriteError(OSError):
    """Failed to persist a secret (symlink at the path, keyring backend error, etc.)."""


def active_backend(cfg: BlumkinConfig) -> str:
    """Backend name ("keyring" or "file") that would service the next read/write."""
    return _backend_for(cfg)


def delete(cfg: BlumkinConfig, kind: SecretKind) -> None:
    """Remove the secret for ``kind`` from both backends, wherever it lives."""
    path = _file_path(cfg, kind)
    if path.is_file():
        path.unlink()
    keyring = _keyring_module()
    if keyring is None:
        return
    try:
        keyring.delete_password(_KEYRING_SERVICE, _keyring_account(cfg, kind))
    except Exception:
        pass


def exists(cfg: BlumkinConfig, kind: SecretKind) -> bool:
    """True when a secret is stored for ``kind`` in either backend."""
    if _file_path(cfg, kind).is_file():
        return True
    keyring = _keyring_module()
    if keyring is None:
        return False
    try:
        return keyring.get_password(_KEYRING_SERVICE, _keyring_account(cfg, kind)) is not None
    except Exception:
        return False


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
        value = keyring.get_password(_KEYRING_SERVICE, account)
    except Exception:
        value = None
    if value is not None:
        return value
    if not path.is_file():
        return None
    try:
        legacy = path.read_text()
    except OSError:
        return None
    try:
        keyring.set_password(_KEYRING_SERVICE, account, legacy)
    except Exception:
        # Keychain write failed - keep serving the file untouched rather than
        # lose the secret.
        return legacy
    try:
        path.unlink()
    except OSError:
        pass
    return legacy


def write_text(cfg: BlumkinConfig, kind: SecretKind, text: str) -> None:
    """Persist the secret for ``kind`` to the active backend for this profile."""
    if _backend_for(cfg) == "keyring":
        keyring = _keyring_module()
        if keyring is None:
            raise SecretWriteError(f"cannot write {kind}: no usable keyring backend")
        try:
            keyring.set_password(_KEYRING_SERVICE, _keyring_account(cfg, kind), text)
        except Exception as exc:
            raise SecretWriteError(f"cannot write {kind} to the OS keychain: {exc}") from exc
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
    return f"{cfg.profile}:{kind}"


_keyring_checked = False
_keyring_mod: Any | None = None


def _keyring_module() -> Any | None:
    """Import ``keyring`` lazily; ``None`` when unavailable or unusable.

    Cached after the first call — the active backend cannot change mid
    process. A backend that resolves to ``keyring.backends.fail`` (no real
    backend found: headless Linux with no Secret Service, etc.) counts as
    unavailable, same as the extra not being installed, so callers never
    block a non-interactive agent shell on a backend that cannot service it.
    """
    global _keyring_mod, _keyring_checked
    if _keyring_checked:
        return _keyring_mod
    _keyring_checked = True
    try:
        import keyring
    except ImportError:
        return None
    try:
        backend = keyring.get_keyring()
        if type(backend).__module__.startswith("keyring.backends.fail"):
            return None
    except Exception:
        return None
    _keyring_mod = keyring
    return _keyring_mod


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
            os.write(fd, text.encode())
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
