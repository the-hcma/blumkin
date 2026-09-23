"""OS-keychain storage for blumkin's OAuth **app** secrets (issue #368).

``blumkin.secret_store`` vaults **user grants** (MSAL cache, Entra auth
record, Google token) - one keychain item per profile, written on every
login/refresh, with a file-backend counterpart blumkin itself owns and can
always fall back to. App secrets are different: Google's ``client_secret``
lives in an *operator-owned* Cloud Console JSON download (not a file blumkin
manages), and Microsoft's public-client ``client_id`` lives inline in
``config.toml``. Neither has a natural "file backend" blumkin could write on
their behalf the way ``secret_store`` does for grants, so this module does
**not** reuse ``secret_store``'s generic ``SecretKind`` read/write/delete
API - it is deliberately **opt-in and keychain-only**:

- ``read_app_secret`` never falls back to a file/toml value itself - it
  returns ``None`` when nothing is vaulted (``token_storage = "file"``, no
  usable backend, or no keychain entry yet). Callers already have their own
  plaintext fallback (the Desktop client JSON for Google, ``cfg.client_id``
  for Microsoft) and apply it themselves when this returns ``None``, giving
  vault-overrides-file precedence without this module needing to know about
  either fallback's storage shape.
- ``write_app_secret`` / ``delete_app_secret`` are only ever called from an
  explicit operator action (``blumkin auth set-app-secret``), never
  implicitly - so, unlike ``secret_store``'s ``token_storage = "auto"``
  grant writes, a write with no usable backend **raises** instead of
  silently falling back to a file: there is no file to fall back to that
  this module owns, and silently discarding an explicit "vault this secret"
  request would be a worse surprise than a clear error.

Reuses ``secret_store``'s low-level keyring primitives (backend lookup,
bounded-timeout calls, the not-found probe, the abandoned-mutation registry)
so both modules share one exact notion of "keyring is usable right now" and
one shared safety net against a hung backend - but keeps its own account
namespace (distinct JSON list content), so a shared timeout registry entry
can never collide with a grant's.

Per the issue's "lean not agent-caching app secrets" note: unlike grants,
this deliberately does **not** go through the macOS `blumkin-agent` cache
(issue #339) - app secrets are read rarely (once per process, at OAuth
client construction) compared to grants (every Graph/Google API call), so
the agent's decrypt-once-then-cache benefit does not apply.
"""

from __future__ import annotations

import json
from typing import Literal

from blumkin import secret_store
from blumkin.config import BlumkinConfig
from blumkin.secret_store import (
    _KEYRING_IO_TIMEOUT_SECONDS,
    _KEYRING_SERVICE,
    SecretWriteError,
    _await_pending_mutation,
    _is_not_found,
)

AppSecretKind = Literal["google_client_secret", "ms_client_id"]


def app_secret_backend(cfg: BlumkinConfig, kind: AppSecretKind) -> Literal["keyring", "none"]:
    """``"keyring"`` when ``kind`` currently has a vaulted value, else ``"none"``.

    A lenient probe (like ``secret_store.exists``): a backend failure or
    timeout reads as ``"none"`` rather than raising, since callers (``doctor``,
    ``auth status``) must never hard-fail just to report where a secret
    lives.
    """
    if cfg.token_storage == "file":
        return "none"
    keyring_module = secret_store._keyring_module()
    if keyring_module is None:
        return "none"
    account = _account(cfg, kind)
    try:
        _await_pending_mutation(account, timeout=_KEYRING_IO_TIMEOUT_SECONDS)
        raw = secret_store._call_keyring_with_timeout(
            keyring_module.get_password, _KEYRING_SERVICE, account
        )
    except Exception:
        return "none"
    return "keyring" if raw else "none"


def read_app_secret(cfg: BlumkinConfig, kind: AppSecretKind) -> str | None:
    """Opt-in keychain read - see the module docstring for the no-fallback contract."""
    if cfg.token_storage == "file":
        return None
    keyring_module = secret_store._keyring_module()
    if keyring_module is None:
        return None
    account = _account(cfg, kind)
    try:
        _await_pending_mutation(account, timeout=_KEYRING_IO_TIMEOUT_SECONDS)
        raw = secret_store._call_keyring_with_timeout(
            keyring_module.get_password, _KEYRING_SERVICE, account
        )
    except Exception:
        return None
    return raw or None


def write_app_secret(cfg: BlumkinConfig, kind: AppSecretKind, value: str) -> None:
    """Vault ``value`` for ``kind``. Raises ``SecretWriteError`` rather than falling
    back - see the module docstring."""
    if not value.strip():
        raise SecretWriteError(f"refusing to vault an empty {kind} value")
    if cfg.token_storage == "file":
        raise SecretWriteError(
            'token_storage = "file" for this profile - app secrets are never vaulted '
            'there; set token_storage = "auto" or "keyring" in config.toml first.'
        )
    keyring_module = secret_store._keyring_module()
    if keyring_module is None:
        raise SecretWriteError(
            'no usable OS keychain backend on this machine - install the "keychain" '
            "extra (`pipx install 'blumkin[keychain]'`) or set token_storage = \"file\" "
            "to opt out of vaulting app secrets."
        )
    account = _account(cfg, kind)
    try:
        _await_pending_mutation(account, timeout=_KEYRING_IO_TIMEOUT_SECONDS)
        secret_store._call_keyring_with_timeout(
            keyring_module.set_password, _KEYRING_SERVICE, account, value
        )
    except Exception as exc:
        raise SecretWriteError(f"could not write {kind} to the OS keychain: {exc}") from exc


def delete_app_secret(cfg: BlumkinConfig, kind: AppSecretKind) -> None:
    """Remove any vaulted value for ``kind``. A missing entry is not an error.

    An unavailable keyring backend *is* an error here (raises
    ``SecretWriteError``) rather than a silent no-op: an entry may already
    exist, and reporting "removed" while the backend is merely unreachable
    would let the caller (``blumkin auth set-app-secret --delete``) believe
    the secret is gone when it is still retrievable once the backend comes
    back (issue #368 review finding).
    """
    keyring_module = secret_store._keyring_module()
    if keyring_module is None:
        raise SecretWriteError(
            f"no usable OS keychain backend on this machine - cannot confirm whether "
            f"a vaulted {kind} was removed."
        )
    account = _account(cfg, kind)
    try:
        _await_pending_mutation(account, timeout=_KEYRING_IO_TIMEOUT_SECONDS)
        secret_store._call_keyring_with_timeout(
            keyring_module.delete_password, _KEYRING_SERVICE, account
        )
    except Exception as exc:
        if _is_not_found(keyring_module, exc):
            return
        raise SecretWriteError(f"could not delete {kind} from the OS keychain: {exc}") from exc


def _account(cfg: BlumkinConfig, kind: AppSecretKind) -> str:
    """Keyring account name, namespaced like ``secret_store._keyring_account``.

    JSON-encoded for the same reason: a ``:`` inside a resolved config dir
    path or profile name cannot be used to construct a colliding account
    string across two config dirs or two profiles.
    """
    resolved_config_dir = str(cfg.config_dir.resolve())
    return json.dumps([resolved_config_dir, cfg.profile, kind], separators=(",", ":"))
