"""Delegated Graph auth: InteractiveBrowserCredential + file cache + auth record."""

from __future__ import annotations

import atexit
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from azure.identity import AuthenticationRecord, InteractiveBrowserCredential
from msal import SerializableTokenCache

from blumkin.config import BlumkinConfig, load_config

# Request exact granted scope names for MSAL silent refresh (e.g. Calendars.ReadWrite
# not .Read). Phase 4 add-ons stay off until config/env enables them and Entra grant
# + re-consent land (see wo1162425_scopes / BLUMKIN_WO1162425_SCOPES).
BASE_SCOPES = [
    "Calendars.ReadWrite",
    "Chat.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "People.Read",
    "User.Read",
]

# Teams chat files live in SharePoint/OneDrive, so `chat attachments download` needs a
# Files scope. Off until the tenant grants it and the user re-consents (files_scopes /
# BLUMKIN_FILES_SCOPES), because requesting an ungranted scope breaks silent refresh.
FILES_SCOPES = [
    "Files.Read",
]


class SecretWriteError(OSError):
    """Failed to persist the MSAL cache or auth record (symlink, perms, etc.)."""


WO1162425_SCOPES = [
    "Chat.ReadWrite",
    "OnlineMeetings.ReadWrite",
]

_token_cache = SerializableTokenCache()
_atexit_registered = False
_cache_bound_path: str | None = None


def create_credential(config: BlumkinConfig | None = None) -> InteractiveBrowserCredential:
    """Build a credential that can silently reuse the cached refresh token."""
    cfg = config or load_config()
    scopes = effective_scopes(cfg)
    if not cfg.client_id:
        raise ValueError(
            "Missing client_id — set client_id in ~/.config/blumkin/config.toml "
            "or BLUMKIN_CLIENT_ID."
        )
    _ensure_cache(cfg)
    record = _load_auth_record(cfg)
    kwargs: dict = {
        "client_id": cfg.client_id,
        "tenant_id": cfg.tenant_id,
        "_cache": _token_cache,
        "_cae_cache": _token_cache,
    }
    if record:
        kwargs["authentication_record"] = record

    credential = InteractiveBrowserCredential(**kwargs)
    if record:
        try:
            credential.get_token(*scopes)
        except Exception:
            # Stale auth record / missing refresh token — force interactive login.
            # Do not wrap save_token_cache here: its OSError (e.g. O_NOFOLLOW) must
            # surface, and get_token transport OSErrors must still fall through.
            pass
        else:
            save_token_cache(cfg)
            return credential

    record = credential.authenticate(scopes=scopes)
    _save_auth_record(cfg, record)
    save_token_cache(cfg)
    return credential


def effective_scopes(config: BlumkinConfig | None = None) -> list[str]:
    """Return MSAL scopes for the current config (Phase 4 add-ons optional)."""
    cfg = config or load_config()
    scopes = list(BASE_SCOPES)
    if cfg.files_scopes:
        scopes.extend(FILES_SCOPES)
    if cfg.wo1162425_scopes:
        scopes.extend(WO1162425_SCOPES)
    return scopes


def logout(config: BlumkinConfig | None = None) -> None:
    global _cache_bound_path
    cfg = config or load_config()
    for path in (cfg.token_cache_path, cfg.auth_record_path):
        if path.is_file():
            path.unlink()
    # Drop in-memory cache so atexit cannot recreate deleted secrets.
    _token_cache.deserialize("")
    if _cache_bound_path == str(cfg.token_cache_path):
        _cache_bound_path = None


def reload_token_cache_from_disk(config: BlumkinConfig | None = None) -> None:
    """Force re-read MSAL cache from disk (e.g. after a test mutates the file)."""
    global _cache_bound_path
    cfg = config or load_config()
    _cache_bound_path = None
    _ensure_cache(cfg)


def save_token_cache(config: BlumkinConfig | None = None) -> None:
    cfg = config or load_config()
    if _cache_bound_path != str(cfg.token_cache_path):
        return
    if _token_cache.has_state_changed:
        _ensure_secret_dir(cfg.config_dir)
        _write_secret_text(cfg.token_cache_path, _token_cache.serialize())


def status_dict(config: BlumkinConfig | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    access = _access_token_expiry(cfg)
    return {
        "access_token_expires_at": access.get("expires_at"),
        "access_token_expires_in_seconds": access.get("expires_in_seconds"),
        "access_token_expired": access.get("expired"),
        "auth_record": cfg.auth_record_path.is_file(),
        "client_id_configured": bool(cfg.client_id),
        "config_dir": str(cfg.config_dir),
        "config_path": str(cfg.config_path),
        "files_scopes": cfg.files_scopes,
        "wo1162425_scopes": cfg.wo1162425_scopes,
        "refresh_token_present": access.get("refresh_token_present", False),
        "requested_scopes": effective_scopes(cfg),
        "tenant_id": cfg.tenant_id,
        "token_cache": cfg.token_cache_path.is_file(),
    }


def _access_token_expiry(cfg: BlumkinConfig) -> dict[str, Any]:
    """Read earliest access-token expires_on from the MSAL cache (no secrets)."""
    out: dict[str, Any] = {
        "expired": None,
        "expires_at": None,
        "expires_in_seconds": None,
        "refresh_token_present": False,
    }
    if not cfg.token_cache_path.is_file():
        return out
    try:
        data = json.loads(cfg.token_cache_path.read_text())
    except json.JSONDecodeError, OSError:
        return out
    out["refresh_token_present"] = bool(data.get("RefreshToken"))
    expires_values: list[int] = []
    for entry in (data.get("AccessToken") or {}).values():
        if not isinstance(entry, dict):
            continue
        raw = entry.get("expires_on")
        if raw is None:
            continue
        try:
            expires_values.append(int(raw))
        except TypeError, ValueError:
            continue
    if not expires_values:
        return out
    expires_on = min(expires_values)
    expires_at = datetime.fromtimestamp(expires_on, tz=UTC)
    now = datetime.now(UTC)
    remaining = int((expires_at - now).total_seconds())
    out["expires_at"] = expires_at.isoformat()
    out["expires_in_seconds"] = remaining
    out["expired"] = remaining <= 0
    return out


def _ensure_cache(cfg: BlumkinConfig) -> None:
    global _atexit_registered, _cache_bound_path
    path = str(cfg.token_cache_path)
    if _cache_bound_path == path:
        return
    _token_cache.deserialize("")
    if cfg.token_cache_path.is_file():
        _token_cache.deserialize(cfg.token_cache_path.read_text())
    _cache_bound_path = path
    # Register once: save only the currently bound path (never stale dirs).
    if not _atexit_registered:
        atexit.register(_save_bound_token_cache_at_exit)
        _atexit_registered = True


def _ensure_secret_dir(directory: Path) -> None:
    """Create ``directory`` at 0700; best-effort tighten if it already existed looser.

    Mode-setting is optional: SMB/FUSE mounts may reject ``chmod``. A symlinked
    config dir is refused (same class of attack as a symlinked secret file).
    """
    if directory.is_symlink():
        raise SecretWriteError(f"cannot use symlinked config dir {directory}")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass


def _load_auth_record(cfg: BlumkinConfig) -> AuthenticationRecord | None:
    if not cfg.auth_record_path.is_file():
        return None
    try:
        return AuthenticationRecord.deserialize(cfg.auth_record_path.read_text())
    except Exception:
        return None


def _save_auth_record(cfg: BlumkinConfig, record: AuthenticationRecord) -> None:
    _ensure_secret_dir(cfg.config_dir)
    _write_secret_text(cfg.auth_record_path, record.serialize())


def _save_bound_token_cache_at_exit() -> None:
    """Persist the in-memory MSAL cache to the currently bound path only."""
    if _cache_bound_path is None or not _token_cache.has_state_changed:
        return
    path = Path(_cache_bound_path)
    try:
        _ensure_secret_dir(path.parent)
        _write_secret_text(path, _token_cache.serialize())
    except OSError:
        # Avoid "Exception ignored" on atexit when the secret path is a symlink
        # or the filesystem rejects mode/write; process is already exiting.
        pass


def _write_secret_text(path: Path, text: str) -> None:
    """Write sensitive text, tightening Unix modes when the file already exists.

    On POSIX, ``O_CREAT`` mode is ignored when the path already exists, so a
    leftover world-readable cache would keep leaking tokens on every rewrite
    without an explicit ``fchmod`` to ``0600``. ``O_NOFOLLOW`` (when available)
    refuses a symlink swap at the path. Mode-setting is best-effort so
    chmod-less filesystems still persist the cache after ``O_TRUNC``.

    On Windows, ``os.fchmod`` is unavailable and ``os.chmod`` only toggles the
    read-only bit (ACLs govern access). The post-close ``chmod`` there avoids
    ``AttributeError`` so login/cache persistence still works; it does not
    claim a Unix ``0600`` guarantee.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        # Symlink at the secret path (ELOOP) or other open failure — never follow.
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
