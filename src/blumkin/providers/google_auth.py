"""Google Workspace OAuth (installed / desktop client, PKCE-friendly)."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from blumkin.auth import (
    AuthRequiredError,
    AuthTransientError,
    MissingScopeError,
    SecretWriteError,
    _ensure_secret_dir,
    format_scope_gap,
    interactive_auth_allowed,
)
from blumkin.config import BlumkinConfig, google_oauth_installed_client, load_config
from blumkin.output import emit_warning
from blumkin.providers.google_http import refresh_request
from blumkin.providers.kind import ProviderConfigError

# Per-skill-area subsets, each the minimum a command in that area needs to work at
# all - passed as `get_credentials(..., required_scopes=...)` so a non-interactive
# fail-fast gate only blocks the scopes the invoked command actually needs, not
# every scope this build could ever request (issue #133 review: a contacts-only
# grant must keep working for `people resolve`, which already degrades gracefully
# without directory.readonly; a gmail.readonly-only grant must keep `mail list`
# working without chat/calendar/gmail.compose). GOOGLE_SCOPES must be their union
# plus directory.readonly (enhances people.resolve, required by no single command,
# admin-restricted in some Workspaces) - `test_google_scopes_is_the_union_of_every_
# required_subset` in tests/test_google_auth_unit.py guards that invariant.
# calendar_freebusy / calendar_suggest only ever call freebusy().query - gating
# them on the full CALENDAR_SCOPES (which includes the write-only calendar.events)
# would fail a {calendar.readonly, calendar.freebusy} grant before any provider
# call (issue #133 review, round 3).
CALENDAR_FREEBUSY_SCOPES = frozenset({"https://www.googleapis.com/auth/calendar.freebusy"})

# calendar_view / calendar_today only ever call events().list - gating them on the
# full CALENDAR_SCOPES (which includes the write-only calendar.events) would fail a
# calendar.readonly-only grant before any provider call, even though the read itself
# needs nothing else (issue #133 review, round 2).
CALENDAR_READ_SCOPES = frozenset({"https://www.googleapis.com/auth/calendar.readonly"})

CALENDAR_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.readonly",
    }
)

# chat_find / chat_last / chat_attachments_* only ever read - gating them on the
# full CHAT_SCOPES (which includes the write-only chat.messages) would fail a
# read-only chat grant before any provider call (issue #133 review, round 3).
CHAT_READ_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/chat.memberships.readonly",
        "https://www.googleapis.com/auth/chat.messages.readonly",
        "https://www.googleapis.com/auth/chat.spaces.readonly",
    }
)

CHAT_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/chat.memberships.readonly",
        "https://www.googleapis.com/auth/chat.messages",
        "https://www.googleapis.com/auth/chat.messages.readonly",
        "https://www.googleapis.com/auth/chat.spaces.readonly",
    }
)

# The default `get_credentials(..., required_scopes=None)` gate, and status_dict's
# missing_scopes: the union of every scope some command actually requires - unlike
# GOOGLE_SCOPES, this excludes directory.readonly (required by no single command,
# see PEOPLE_SCOPES below), so `auth refresh` / `auth status` / `doctor` do not
# report a permanent, unsatisfiable gap for a Workspace that admin-restricts it
# (issue #133 review, round 3).
GOOGLE_REQUIRED_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/chat.memberships.readonly",
        "https://www.googleapis.com/auth/chat.messages",
        "https://www.googleapis.com/auth/chat.messages.readonly",
        "https://www.googleapis.com/auth/chat.spaces.readonly",
        "https://www.googleapis.com/auth/contacts.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.readonly",
    }
)

GOOGLE_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/chat.memberships.readonly",
        "https://www.googleapis.com/auth/chat.messages",
        "https://www.googleapis.com/auth/chat.messages.readonly",
        "https://www.googleapis.com/auth/chat.spaces.readonly",
        "https://www.googleapis.com/auth/contacts.readonly",
        "https://www.googleapis.com/auth/directory.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.readonly",
    }
)

MAIL_READ_SCOPES = frozenset({"https://www.googleapis.com/auth/gmail.readonly"})

MAIL_WRITE_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.readonly",
    }
)

PEOPLE_SCOPES = frozenset({"https://www.googleapis.com/auth/contacts.readonly"})


def get_credentials(
    config: BlumkinConfig | None = None,
    *,
    allow_interactive: bool | None = None,
    required_scopes: frozenset[str] | None = None,
) -> Credentials:
    """Load or obtain Google OAuth credentials; refresh silently when possible.

    Non-interactive callers fail fast with :class:`MissingScopeError` when the
    stored grant does not cover ``required_scopes`` — before any provider call,
    not after a 403 (issue #133). ``required_scopes`` defaults to
    ``GOOGLE_REQUIRED_SCOPES`` (the union every command could actually need, used
    by ``auth refresh`` / ``doctor`` - excludes ``directory.readonly``, which no
    command requires and some Workspaces admin-restrict, issue #133 review round 3);
    pass a skill area's narrower subset (``CALENDAR_SCOPES``, ``PEOPLE_SCOPES``, …)
    so a grant missing an unrelated scope does not block a command that never
    needed it (issue #133 review - e.g. a contacts-only grant must keep working for
    `people resolve`, which already degrades without ``directory.readonly``).
    """
    cfg = config or load_config()
    if not cfg.client_id and cfg.google_oauth_client_file is None:
        raise ProviderConfigError(
            "Missing Google OAuth client — set google_oauth_client_file (path to "
            "Desktop client JSON) or client_id in config.toml."
        )
    required = GOOGLE_REQUIRED_SCOPES if required_scopes is None else required_scopes
    interactive = interactive_auth_allowed() if allow_interactive is None else allow_interactive
    creds = _load_credentials(cfg)
    if creds is not None:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(refresh_request(cfg))
            except Exception as exc:
                if not interactive:
                    raise _classify_refresh_error(exc) from exc
            else:
                _save_credentials(cfg, creds, preserve_granted_scopes=True)
                if not _needs_additional_scopes(cfg, required):
                    return creds
                if not interactive:
                    raise _missing_scope_error(cfg, required=required)
        elif creds.valid:
            if not _needs_additional_scopes(cfg, required):
                return creds
            if not interactive:
                raise _missing_scope_error(cfg, required=required)
        elif not interactive:
            raise AuthRequiredError(
                "Silent token refresh failed. Run `blumkin auth login` on a TTY "
                "(or unset BLUMKIN_NONINTERACTIVE), then retry."
            )

    if not interactive:
        raise AuthRequiredError(
            "Authentication required. Run `blumkin auth login` on a TTY "
            "(agent shells should set BLUMKIN_NONINTERACTIVE=1 and never open a browser)."
        )

    creds = _run_interactive_consent(cfg)
    _save_credentials(cfg, creds)
    return creds


def login(config: BlumkinConfig | None = None) -> Credentials:
    """Interactive browser login; persist token JSON under the config dir."""
    return get_credentials(config, allow_interactive=True)


def logout(config: BlumkinConfig | None = None) -> None:
    """Delete the Google token file when present."""
    cfg = config or load_config()
    if cfg.google_token_path.is_file():
        cfg.google_token_path.unlink()


def persisted_granted_scopes(cfg: BlumkinConfig) -> frozenset[str]:
    """Scopes the user actually consented to, as stored in the token file.

    ``Credentials.scopes`` after load reports ``GOOGLE_SCOPES`` (what this build
    wants), not the grant. Use this for permission checks against stored tokens.
    """
    scopes = _read_persisted_scopes(cfg)
    return frozenset(scopes or ())


def refresh_silent(config: BlumkinConfig | None = None) -> dict[str, Any]:
    """Force silent credential refresh; never open a browser."""
    get_credentials(config, allow_interactive=False)
    return status_dict(config)


def status_dict(config: BlumkinConfig | None = None) -> dict[str, Any]:
    """Auth status without secrets (aligned keys with Microsoft status where possible)."""
    cfg = config or load_config()
    access = _access_token_expiry(cfg)
    granted = persisted_granted_scopes(cfg)
    return {
        "access_token_expires_at": access.get("expires_at"),
        "access_token_expires_in_seconds": access.get("expires_in_seconds"),
        "access_token_expired": access.get("expired"),
        "auth_record": cfg.google_token_path.is_file(),
        "client_id_configured": bool(cfg.client_id) or cfg.google_oauth_client_file is not None,
        "config_dir": str(cfg.config_dir),
        "config_path": str(cfg.config_path),
        "granted_scopes": sorted(granted),
        # Empty until a token file exists: nothing to diff a fresh, never-logged-in
        # profile against (auth_required already covers that state). Diffed against
        # GOOGLE_REQUIRED_SCOPES, not GOOGLE_SCOPES: directory.readonly is optional
        # (issue #133 review, round 3).
        "missing_scopes": sorted(GOOGLE_REQUIRED_SCOPES - granted)
        if cfg.google_token_path.is_file()
        else [],
        "provider": "google",
        "refresh_token_present": access.get("refresh_token_present", False),
        "requested_scopes": sorted(GOOGLE_SCOPES),
        "tenant_id": "",
        # Google stores the OAuth session in one token JSON (no separate MSAL auth record).
        "token_cache": cfg.google_token_path.is_file(),
    }


def _access_token_expiry(cfg: BlumkinConfig) -> dict[str, Any]:
    out: dict[str, Any] = {
        "expired": None,
        "expires_at": None,
        "expires_in_seconds": None,
        "refresh_token_present": False,
    }
    creds = _load_credentials(cfg)
    if creds is None:
        return out
    out["refresh_token_present"] = bool(creds.refresh_token)
    if creds.expiry is None:
        return out
    expires_at = creds.expiry
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    else:
        expires_at = expires_at.astimezone(UTC)
    now = datetime.now(UTC)
    remaining = int((expires_at - now).total_seconds())
    out["expires_at"] = expires_at.isoformat()
    out["expires_in_seconds"] = remaining
    out["expired"] = remaining <= 0
    return out


def _classify_refresh_error(exc: BaseException) -> AuthRequiredError | AuthTransientError:
    """Map a ``Credentials.refresh()`` failure to a typed auth error (issue #133)."""
    if isinstance(exc, TransportError | TimeoutError):
        return AuthTransientError(
            f"Google token refresh hit a transient network error: {exc}. Safe to retry."
        )
    if isinstance(exc, RefreshError):
        # google-auth itself marks a 5xx/server-side token-endpoint failure
        # retryable (GoogleAuthError.retryable) - trust that over guessing from
        # the message, so a plain outage does not read as "grant revoked"
        # (issue #133 review).
        if getattr(exc, "retryable", False):
            return AuthTransientError(
                f"Google token endpoint returned a transient error: {exc}. Safe to retry."
            )
        if "invalid_grant" in str(exc):
            return AuthRequiredError(
                "Google refresh token was revoked or expired (invalid_grant). Run "
                "`blumkin auth login` on a TTY, then retry."
            )
    return AuthRequiredError(
        "Silent token refresh failed. Run `blumkin auth login` on a TTY "
        "(or unset BLUMKIN_NONINTERACTIVE), then retry."
    )


def _client_config(cfg: BlumkinConfig) -> dict[str, Any]:
    """Build InstalledAppFlow client config from the Desktop download JSON.

    Google Cloud Desktop clients ship a ``client_secret`` in that file; the
    token endpoint rejects the exchange when it is omitted. Secrets stay in the
    referenced JSON (mode 0600), never in env or ``config.toml``.
    """
    path = cfg.google_oauth_client_file
    if path is None:
        raise ProviderConfigError(
            "google_oauth_client_file is required for Google auth "
            "(path to Cloud Console Desktop client JSON)."
        )
    if not path.is_file():
        raise ProviderConfigError(f"google_oauth_client_file not found: {path}")
    installed = dict(google_oauth_installed_client(path))
    client_id = installed.get("client_id")
    if not isinstance(client_id, str) or not client_id.strip():
        raise ProviderConfigError(f"google_oauth_client_file {path} missing client_id")
    secret = installed.get("client_secret")
    if not isinstance(secret, str) or not secret.strip():
        raise ProviderConfigError(
            f"google_oauth_client_file {path} missing client_secret "
            "(required for Desktop token exchange)."
        )
    installed.setdefault("auth_uri", "https://accounts.google.com/o/oauth2/auth")
    installed.setdefault("redirect_uris", ["http://localhost"])
    installed.setdefault("token_uri", "https://oauth2.googleapis.com/token")
    return {"installed": installed}


def _client_secret_from_oauth_file(cfg: BlumkinConfig) -> str:
    path = cfg.google_oauth_client_file
    if path is None or not path.is_file():
        return ""
    try:
        installed = google_oauth_installed_client(path)
    except ProviderConfigError:
        return ""
    raw = installed.get("client_secret")
    return raw.strip() if isinstance(raw, str) else ""


def _consent_once(cfg: BlumkinConfig, *, force_consent: bool) -> Credentials:
    """Run the browser consent flow exactly once; let a partial-grant Warning propagate."""
    flow = InstalledAppFlow.from_client_config(_client_config(cfg), scopes=sorted(GOOGLE_SCOPES))
    url_params = {"prompt": "consent"} if force_consent else None
    creds = flow.run_local_server(port=0, authorization_url_params=url_params)
    if not isinstance(creds, Credentials):
        raise TypeError("expected google.oauth2.credentials.Credentials from InstalledAppFlow")
    return creds


def _load_credentials(cfg: BlumkinConfig) -> Credentials | None:
    path = cfg.google_token_path
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError, OSError:
        return None
    if not isinstance(data, dict):
        return None
    # Prefer Desktop JSON client_secret when present so rotated secrets win over
    # a stale value persisted in google_token.json; fall back to the token file.
    info = dict(data)
    secret = _client_secret_from_oauth_file(cfg)
    if secret:
        info["client_secret"] = secret
    try:
        return Credentials.from_authorized_user_info(info, scopes=sorted(GOOGLE_SCOPES))
    except Exception:
        return None


def _missing_scope_error(
    cfg: BlumkinConfig,
    *,
    current: frozenset[str] | None = None,
    required: frozenset[str] = GOOGLE_SCOPES,
) -> MissingScopeError:
    """Build a :class:`MissingScopeError` showing the granted-vs-needed gap.

    ``current`` overrides the persisted grant when the caller already knows the
    actual scopes Google returned (e.g. from a partial-consent Warning) — more
    accurate than the token file on a fresh install that never got that far.
    ``required`` narrows "needed" to a skill area's subset (default: the whole
    build); the message always reports against ``required``, not every scope
    this build could ever request.
    """
    granted = current if current is not None else persisted_granted_scopes(cfg)
    missing = required - granted
    return MissingScopeError(
        "Stored Google grant is missing scopes this build needs.\n"
        + format_scope_gap(current=granted, missing=missing),
        current=granted,
        missing=missing,
    )


def _needs_additional_scopes(cfg: BlumkinConfig, required: frozenset[str]) -> bool:
    """True when the stored grant is missing any scope in ``required``."""
    if not cfg.google_token_path.is_file():
        return False
    granted = persisted_granted_scopes(cfg)
    if not granted:
        # Pre-scope-tracking token, or an empty list — re-consent so the file
        # and server grant align with GOOGLE_SCOPES.
        return True
    return not required.issubset(granted)


def _read_persisted_scopes(cfg: BlumkinConfig) -> list[str] | None:
    path = cfg.google_token_path
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError, OSError:
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("scopes")
    if not isinstance(raw, list):
        return None
    scopes = [scope for scope in raw if isinstance(scope, str) and scope]
    return scopes if scopes else None


def _run_interactive_consent(cfg: BlumkinConfig) -> Credentials:
    """Run the browser consent flow, auto-escalating once on a partial grant.

    Google's consent screen lets the user leave scope checkboxes unticked; when
    that happens, oauthlib raises a bare ``Warning`` instead of returning partial
    credentials (issue #133). Retry exactly once with ``prompt=consent`` and a
    warning telling the operator to tick every box; a second partial grant stops
    instead of looping.
    """
    force_consent = _needs_additional_scopes(cfg, GOOGLE_SCOPES)
    if force_consent:
        _warn_scope_gap(persisted_granted_scopes(cfg))
    try:
        return _consent_once(cfg, force_consent=force_consent)
    except Warning as exc:
        granted = _scopes_from_oauthlib_warning(exc)
        if force_consent:
            raise _missing_scope_error(cfg, current=granted) from exc
        _warn_scope_gap(granted or frozenset())
        try:
            return _consent_once(cfg, force_consent=True)
        except Warning as exc2:
            raise _missing_scope_error(
                cfg, current=_scopes_from_oauthlib_warning(exc2) or granted
            ) from exc2


def _save_credentials(
    cfg: BlumkinConfig,
    creds: Credentials,
    *,
    preserve_granted_scopes: bool = False,
) -> None:
    _ensure_secret_dir(cfg.profile_dir, stop_at=cfg.config_dir)
    payload = json.loads(creds.to_json())
    if preserve_granted_scopes:
        granted = _read_persisted_scopes(cfg)
        if granted is not None:
            payload["scopes"] = sorted(granted)
        else:
            # Pre-scope-tracking token: do not stamp GOOGLE_SCOPES from to_json().
            payload.pop("scopes", None)
    secret = _client_secret_from_oauth_file(cfg)
    if secret:
        payload["client_secret"] = secret
    else:
        payload.setdefault("client_secret", "")
    _write_secret_text(cfg.google_token_path, json.dumps(payload))


def _scopes_from_oauthlib_warning(exc: Warning) -> frozenset[str] | None:
    """Scopes Google actually granted, parsed from oauthlib's partial-consent Warning.

    oauthlib stamps ``.new_scope`` / ``.old_scope`` list attributes on the
    ``Warning`` it raises when the returned grant differs from what was
    requested; fall back to regexing the message if a future oauthlib drops
    those. ``None`` means neither worked - caller falls back to the persisted
    grant.
    """
    new_scope = getattr(exc, "new_scope", None)
    if isinstance(new_scope, list) and all(isinstance(scope, str) for scope in new_scope):
        return frozenset(new_scope)
    match = re.search(r'Scope has changed from "[^"]*" to "([^"]*)"', str(exc))
    return frozenset(match.group(1).split()) if match else None


def _warn_scope_gap(current: frozenset[str]) -> None:
    emit_warning(
        "Stored Google grant is missing scopes this build needs. Re-opening the "
        'consent screen - tick every box (or click "Select all") this time.\n'
        + format_scope_gap(current=current, missing=GOOGLE_SCOPES - current)
    )


def _write_secret_text(path: Path, text: str) -> None:
    """Write sensitive text at 0600 with O_NOFOLLOW when available (see blumkin.auth)."""
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
