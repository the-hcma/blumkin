"""Google Workspace OAuth (installed / desktop client, PKCE-friendly)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from blumkin import secret_store
from blumkin.app_secrets import read_app_secret
from blumkin.auth import (
    AuthRequiredError,
    AuthTransientError,
    MissingScopeError,
    format_scope_gap,
    interactive_auth_allowed,
)
from blumkin.config import BlumkinConfig, google_oauth_installed_client, load_config
from blumkin.output import emit_warning, hyperlink
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

# docs_create authors a real Google Doc (documents) and files it under a folder
# blumkin made (drive.file - narrow, only ever sees blumkin's own files). A
# root-level `docs create` needs nothing more. `docs create --folder <path>`
# targeting a *pre-existing* folder additionally needs the full `drive` scope
# (DOCS_FOLDER_SCOPES below); the fail-fast gate widens only when a folder is
# passed, so a `{documents, drive.file}` grant keeps working for the common case.
DOCS_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive.file",
    }
)

# `docs create --folder <path>` resolving a folder the user made by hand (see D11).
DOCS_FOLDER_SCOPES = DOCS_SCOPES | {"https://www.googleapis.com/auth/drive"}

# The `drive` skill area (list / get / download / export / read, and mkdir / move
# / rename) needs full read+write to the user's Drive so it can target folders the
# user made by hand - `drive.file` only ever sees blumkin's own files, and
# `drive.readonly` cannot rewrite a file's `parents`. Unlike Microsoft (gated
# behind the `docs_scopes` toggle), Google widens its standard scope set and
# re-prompts consent on the next `blumkin auth login`, like any added scope.
DRIVE_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/drive",
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
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.settings.basic",
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
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.settings.basic",
    }
)

MAIL_READ_SCOPES = frozenset({"https://www.googleapis.com/auth/gmail.readonly"})

MAIL_WRITE_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.readonly",
    }
)

# Mail triage (move / mark / delete): gmail.compose does NOT permit
# messages.modify / trash, so this needs its own consent (issue #177).
MAIL_MODIFY_SCOPES = frozenset({"https://www.googleapis.com/auth/gmail.modify"})

# Vacation responder (mail auto-reply): users.settings.getVacation /
# updateVacation - its own consent (issue #179).
MAIL_SETTINGS_SCOPES = frozenset({"https://www.googleapis.com/auth/gmail.settings.basic"})

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
    # A fresh interactive consent may be a different Google account (or a
    # re-grant after a revoked refresh token), so the previous agent-cached
    # token must not keep being served for the rest of its TTL (PR #346
    # review, mirroring auth.create_credential's Microsoft interactive
    # branch). The silent-refresh save above (preserve_granted_scopes=True)
    # deliberately skips this, since it is the same credential.
    secret_store.invalidate_agent_cache(cfg)
    _save_credentials(cfg, creds)
    return creds


def login(config: BlumkinConfig | None = None) -> Credentials:
    """Interactive browser login; persist token JSON under the config dir."""
    return get_credentials(config, allow_interactive=True)


def logout(config: BlumkinConfig | None = None) -> None:
    """Delete the Google token file when present."""
    cfg = config or load_config()
    secret_store.delete(cfg, "google_token")


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
    # Read the token once and reuse it below - `_access_token_expiry`,
    # `persisted_granted_scopes`, the presence flag, and `token_storage_backend`
    # each used to make their own independent keyring round trip for this same
    # secret, so a single `doctor` / `auth status` call could hit the OS
    # keychain for one item up to four times.
    raw_token, token_backend = secret_store.read_text_and_backend(cfg, "google_token")
    access = _access_token_expiry(raw_token)
    granted = frozenset(_scopes_from_raw(raw_token) or ())
    token_present = raw_token is not None
    return {
        "access_token_expires_at": access.get("expires_at"),
        "access_token_expires_in_seconds": access.get("expires_in_seconds"),
        "access_token_expired": access.get("expired"),
        "auth_record": token_present,
        "client_id_configured": bool(cfg.client_id) or cfg.google_oauth_client_file is not None,
        "config_dir": str(cfg.config_dir),
        "config_path": str(cfg.config_path),
        "granted_scopes": sorted(granted),
        # Empty until a token file exists: nothing to diff a fresh, never-logged-in
        # profile against (auth_required already covers that state). Diffed against
        # GOOGLE_REQUIRED_SCOPES, not GOOGLE_SCOPES: directory.readonly is optional
        # (issue #133 review, round 3).
        "missing_scopes": sorted(GOOGLE_REQUIRED_SCOPES - granted) if token_present else [],
        "provider": "google",
        "refresh_token_present": access.get("refresh_token_present", False),
        "requested_scopes": sorted(GOOGLE_SCOPES),
        "tenant_id": "",
        # Google stores the OAuth session in one token JSON (no separate MSAL auth record).
        "token_cache": token_present,
        "token_storage_backend": token_backend,
    }


def _access_token_expiry(raw: str | None) -> dict[str, Any]:
    """Takes the already-read ``google_token`` text rather than reading it itself,
    so ``status_dict`` does not pay for a second keyring round trip on top of
    its own read of the same secret. Uses ``_credentials_for_status`` (not
    ``_credentials_from_raw``) so this stays to zero app-secret keychain
    touches - it never needs a live ``client_secret``, only expiry/refresh_token
    metadata (issue #368 review)."""
    out: dict[str, Any] = {
        "expired": None,
        "expires_at": None,
        "expires_in_seconds": None,
        "refresh_token_present": False,
    }
    creds = _credentials_for_status(raw)
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


def _authorization_prompt_message() -> str:
    """The line ``run_local_server`` prints before opening the browser.

    ``google_auth_oauthlib`` substitutes ``{url}`` with the (very long) consent
    URL; wrap that placeholder in an OSC 8 hyperlink so a capable terminal shows
    a short click target instead of a dozen wrapped rows. Falls back to the plain
    URL off a TTY (see :func:`blumkin.output.hyperlink`).
    """
    link = hyperlink("Google authorization page", "{url}")
    return f"Opening your browser to authorize blumkin. If it does not open, visit:\n  {link}"


def _client_config(cfg: BlumkinConfig) -> dict[str, Any]:
    """Build InstalledAppFlow client config from toml, the OS keychain, and (optionally)
    the Desktop download JSON.

    ``client_secret`` prefers the OS keychain (``blumkin auth set-app-secret``,
    issue #368) over the Desktop client JSON's own value; at least one of the
    two must supply it. The Desktop client JSON (``google_oauth_client_file``)
    itself is now optional - once the keychain holds ``client_secret`` and
    ``client_id`` / ``auth_uri`` / ``token_uri`` / ``redirect_uris`` are set in
    ``config.toml`` (or fall back to blumkin's own defaults), no side-car file
    is required at all (issue #368 goal: ``config.toml`` + the keychain as the
    complete config surface, no mandatory JSON file). When the file is still
    configured, it continues to supply anything toml/keychain do not
    (``.setdefault()``-equivalent tolerance preserved via the ``_resolved_*``
    helpers below).
    """
    path = cfg.google_oauth_client_file
    installed: dict[str, Any] = {}
    if path is not None:
        if not path.is_file():
            raise ProviderConfigError(f"google_oauth_client_file not found: {path}")
        installed = dict(google_oauth_installed_client(path))
    if not cfg.client_id.strip():
        where = f"google_oauth_client_file {path}" if path is not None else "config.toml"
        raise ProviderConfigError(f"client_id is required for Google auth (set it in {where}).")
    secret = _resolved_client_secret(cfg, installed)
    if not secret:
        raise ProviderConfigError(
            "client_secret is required for Google auth - vault it with "
            "`blumkin auth set-app-secret`, or set google_oauth_client_file to a "
            "Desktop client JSON that has one."
        )
    installed["client_secret"] = secret
    installed["client_id"] = cfg.client_id
    installed["auth_uri"] = _resolved_google_endpoint(
        cfg.google_auth_uri, installed, "auth_uri", "https://accounts.google.com/o/oauth2/auth"
    )
    installed["redirect_uris"] = list(cfg.google_redirect_uris) or _resolved_google_redirect_uris(
        installed
    )
    installed["token_uri"] = _resolved_google_endpoint(
        cfg.google_token_uri, installed, "token_uri", "https://oauth2.googleapis.com/token"
    )
    return {"installed": installed}


def _resolved_client_secret(cfg: BlumkinConfig, installed: dict[str, Any]) -> str:
    """The OS keychain's ``client_secret`` (issue #368) > the Desktop JSON's own value."""
    vaulted = read_app_secret(cfg, "google_client_secret")
    if vaulted:
        return vaulted
    raw = installed.get("client_secret")
    return raw.strip() if isinstance(raw, str) else ""


def _resolved_google_endpoint(
    override: str, installed: dict[str, Any], file_key: str, default: str
) -> str:
    """``config.toml`` override > the Desktop JSON's own value > ``default``."""
    if override:
        return override
    raw = installed.get(file_key)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return default


def _resolved_google_redirect_uris(installed: dict[str, Any]) -> list[str]:
    """The Desktop JSON's own ``redirect_uris`` (validated) > ``["http://localhost"]``."""
    raw = installed.get("redirect_uris")
    if (
        isinstance(raw, list)
        and raw
        and all(isinstance(item, str) and item.strip() for item in raw)
    ):
        return [item.strip() for item in raw]
    return ["http://localhost"]


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


def _vaulted_or_file_client_secret(cfg: BlumkinConfig) -> str:
    """The OS keychain's ``client_secret`` (issue #368) > the Desktop JSON's own value.

    Used by call sites that only need the value (not the rest of an ``installed``
    dict already in hand) - ``_client_config`` uses ``_resolved_client_secret``
    instead so it does not re-read the file a second time.
    """
    vaulted = read_app_secret(cfg, "google_client_secret")
    return vaulted if vaulted else _client_secret_from_oauth_file(cfg)


def _consent_once(cfg: BlumkinConfig, *, force_consent: bool) -> Credentials:
    """Run the browser consent flow exactly once; let a partial-grant Warning propagate."""
    flow = InstalledAppFlow.from_client_config(_client_config(cfg), scopes=sorted(GOOGLE_SCOPES))
    url_params = {"prompt": "consent"} if force_consent else None
    creds = flow.run_local_server(
        port=0,
        authorization_url_params=url_params,
        authorization_prompt_message=_authorization_prompt_message(),
    )
    if not isinstance(creds, Credentials):
        raise TypeError("expected google.oauth2.credentials.Credentials from InstalledAppFlow")
    return creds


def _load_credentials(cfg: BlumkinConfig) -> Credentials | None:
    return _credentials_from_raw(cfg, secret_store.read_text(cfg, "google_token"))


def _credentials_for_status(raw: str | None) -> Credentials | None:
    """Parse an already-read ``google_token`` payload for status/doctor reporting only.

    Deliberately does *not* resolve the vaulted ``client_secret`` - status only
    reads expiry/refresh_token metadata, never refreshes, and the persisted
    token JSON already carries a ``client_secret`` key (written by
    ``_save_credentials``) sufficient to satisfy
    ``Credentials.from_authorized_user_info``'s required-keys check. Adding an
    app-secret keychain lookup here would give a status read a second keychain
    touch (and potentially a second OS authorization prompt) for a value it
    never uses (issue #368 review).
    """
    data = _parse_token_payload(raw)
    if data is None:
        return None
    return _credentials_from_info(dict(data))


def _credentials_from_info(info: dict[str, Any]) -> Credentials | None:
    try:
        return Credentials.from_authorized_user_info(info, scopes=sorted(GOOGLE_SCOPES))
    except Exception:
        return None


def _credentials_from_raw(cfg: BlumkinConfig, raw: str | None) -> Credentials | None:
    """Parse an already-read ``google_token`` payload into ``Credentials``, resolving
    the current (possibly vaulted) ``client_secret`` for real refresh use.

    Only used by the actual credential-construction path (``_load_credentials``,
    called from ``get_credentials`` on every skill invocation) - the
    status/doctor path uses ``_credentials_for_status`` instead, which skips
    the app-secret keychain lookup entirely so a status read stays to one
    keychain touch (issue #368 review).
    """
    data = _parse_token_payload(raw)
    if data is None:
        return None
    # Prefer the vaulted/Desktop-JSON client_secret when present so a rotated
    # secret wins over a stale value persisted in google_token.json; fall back
    # to whatever is already in the token file.
    info = dict(data)
    secret = _vaulted_or_file_client_secret(cfg)
    if secret:
        info["client_secret"] = secret
    return _credentials_from_info(info)


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
    if not secret_store.exists(cfg, "google_token"):
        return False
    granted = persisted_granted_scopes(cfg)
    if not granted:
        # Pre-scope-tracking token, or an empty list — re-consent so the file
        # and server grant align with GOOGLE_SCOPES.
        return True
    return not required.issubset(granted)


def _parse_token_payload(raw: str | None) -> dict[str, Any] | None:
    """Best-effort JSON-object parse of a ``google_token`` payload, else ``None``."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError, OSError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _read_persisted_scopes(cfg: BlumkinConfig) -> list[str] | None:
    return _scopes_from_raw(secret_store.read_text(cfg, "google_token"))


def _scopes_from_raw(raw: str | None) -> list[str] | None:
    """Parse an already-read ``google_token`` payload's granted scopes.

    Split out of ``_read_persisted_scopes`` so a caller that already has the
    raw text (``status_dict``) does not pay for a second keyring round trip
    on top of its own read of the same secret.
    """
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError, OSError:
        return None
    if not isinstance(data, dict):
        return None
    raw_scopes = data.get("scopes")
    if not isinstance(raw_scopes, list):
        return None
    scopes = [scope for scope in raw_scopes if isinstance(scope, str) and scope]
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
    payload = json.loads(creds.to_json())
    if preserve_granted_scopes:
        granted = _read_persisted_scopes(cfg)
        if granted is not None:
            payload["scopes"] = sorted(granted)
        else:
            # Pre-scope-tracking token: do not stamp GOOGLE_SCOPES from to_json().
            payload.pop("scopes", None)
    # A vaulted client_secret (issue #368) must never reach the token file -
    # `write_text` can fall back to plaintext under `token_storage = "auto"`
    # if a keyring write fails, which would leak a value the operator
    # explicitly chose to vault out of plaintext. When a vault is currently
    # set, `payload["client_secret"]` (from `creds.to_json()`) may *be* that
    # vaulted value - `_credentials_from_raw` injects it before constructing
    # `Credentials` - so it must be replaced with the Desktop JSON's own
    # value (or blanked); real credential-refresh call sites always
    # re-resolve the current vaulted-or-file secret at load time instead.
    # When nothing is vaulted, `payload`'s own secret came from the token
    # file/Desktop JSON itself (never a keychain-only value) and is safe to
    # keep as-is if the Desktop JSON does not override it - matches the
    # pre-#368 behavior, and preserves an already-working profile whose
    # `google_oauth_client_file` has since gone missing (issue #368 review).
    file_secret = _client_secret_from_oauth_file(cfg)
    if read_app_secret(cfg, "google_client_secret"):
        payload["client_secret"] = file_secret
    elif file_secret:
        payload["client_secret"] = file_secret
    else:
        payload.setdefault("client_secret", "")
    secret_store.write_text(cfg, "google_token", json.dumps(payload))


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
