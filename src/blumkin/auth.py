"""Delegated Graph auth: InteractiveBrowserCredential + file cache + auth record."""

from __future__ import annotations

import atexit
import json
import os
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from azure.identity import AuthenticationRecord, InteractiveBrowserCredential
from msal import SerializableTokenCache

from blumkin import secret_store
from blumkin.app_secrets import read_app_secret
from blumkin.config import BlumkinConfig, load_config
from blumkin.providers.kind import ProviderConfigError
from blumkin.secret_store import SecretWriteError as SecretWriteError


class AuthError(ValueError):
    """Base for typed, classified auth failures (issue #133).

    A ``ValueError`` subclass so existing ``except ValueError`` call sites keep
    working; new code should ``isinstance``-check the specific subclass instead
    of sniffing ``str(exc)``.
    """


class AuthRequiredError(AuthError):
    """No usable token: the refresh token is dead, revoked, or missing.

    The fix is always the same across providers and flows: `blumkin auth login`
    on a TTY.
    """


class AuthTransientError(AuthError):
    """A network or server-side error while talking to the auth provider.

    Distinct from :class:`AuthRequiredError` so the CLI can tell the operator
    this is safe to retry, rather than implying the grant itself is bad.
    """


# Request exact granted scope names for MSAL silent refresh (e.g. Calendars.ReadWrite
# not .Read). Phase 4 add-ons stay off until config enables them and Entra grant
# + re-consent land (see wo1162425_scopes in config.toml).
BASE_SCOPES = [
    "Calendars.ReadWrite",
    "Chat.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "User.Read",
]

# `docs create` uploads a .docx to the user's OneDrive. Off until the tenant
# grants Files.ReadWrite and the user re-consents (docs_scopes in config.toml) -
# a separate toggle from files_scopes, which unlocks only chat-file reads
# (docs/DECISIONS.md D10). Requesting an ungranted scope breaks silent refresh.
DOCS_SCOPES = [
    "Files.ReadWrite",
]

# Teams chat files live in SharePoint/OneDrive, so `chat attachments download` needs a
# Files scope. Off until the tenant grants it and the user re-consents
# (files_scopes in config.toml), because requesting an ungranted scope breaks silent refresh.
FILES_SCOPES = [
    "Files.Read",
]


class MissingScopeError(AuthError):
    """The stored grant does not cover every scope this build requests.

    ``current`` and ``missing`` are the granted vs. still-needed scope sets, so
    the error message can show the gap explicitly instead of failing late on a
    provider 403.
    """

    def __init__(self, message: str, *, current: frozenset[str], missing: frozenset[str]) -> None:
        super().__init__(message)
        self.current = current
        self.missing = missing


# Remedy WO1162425 was augmented beyond the original chat/meetings pair; Identity has
# not finished granting the new asks. Keep these off until wo1162425_scopes + re-consent.
# Full ask list (Teams / mailbox / productivity) lives in HANDOFF.md — only scopes we
# actually request at runtime belong here.
WO1162425_SCOPES = [
    "Chat.ReadWrite",
    "MailboxSettings.ReadWrite",
    "OnlineMeetings.ReadWrite",
    "People.Read",
]

# Teams/People-directory features are work/school-only - Entra will never grant
# these to a personal Microsoft Account (MSA), so effective_scopes() drops them
# for an `account_type = "personal"` profile (issue #297) rather than requesting
# a scope that would break silent refresh entirely. MailboxSettings.ReadWrite is
# deliberately not in this set - auto-reply is a plain mailbox setting, not a
# Teams/org-only feature.
PERSONAL_ACCOUNT_UNSUPPORTED_SCOPES = frozenset(
    {"Chat.Read", "Chat.ReadWrite", "OnlineMeetings.ReadWrite", "People.Read"}
)

_token_cache = SerializableTokenCache()
_atexit_registered = False
_cache_bound_cfg: BlumkinConfig | None = None
_cache_bound_key: tuple[str, str] | None = None


def _resolved_client_id(cfg: BlumkinConfig) -> str:
    """The OS keychain's ``ms_client_id`` (issue #368) > ``config.toml``'s own value.

    Mirrors Google's ``client_secret`` precedence (``_vaulted_or_file_client_secret``
    in ``google_auth.py``): vaulting is opt-in, so an unvaulted profile is
    unaffected and keeps reading straight from ``config.toml``.
    """
    return read_app_secret(cfg, "ms_client_id") or cfg.client_id


def create_credential(
    config: BlumkinConfig | None = None,
    *,
    allow_interactive: bool | None = None,
) -> InteractiveBrowserCredential:
    """Build a credential that can silently reuse the cached refresh token.

    When ``allow_interactive`` is false (or auto-detected non-TTY /
    ``BLUMKIN_NONINTERACTIVE=1``), never call ``authenticate()`` — raise so the
    CLI can exit ``auth_required`` instead of hanging on a browser prompt.
    """
    cfg = config or load_config()
    scopes = effective_scopes(cfg)
    client_id = _resolved_client_id(cfg)
    if not client_id:
        raise ProviderConfigError(
            "Missing client_id — set client_id in ~/.config/blumkin/config.toml, "
            "or vault it with `blumkin auth set-app-secret --kind ms_client_id`."
        )
    interactive = interactive_auth_allowed() if allow_interactive is None else allow_interactive
    _ensure_cache(cfg)
    record = _load_auth_record(cfg)
    timeout_s = float(cfg.graph_timeout_seconds)
    connect_s = min(30.0, timeout_s)
    kwargs: dict = {
        "client_id": client_id,
        "tenant_id": cfg.tenant_id,
        "_cache": _token_cache,
        "_cae_cache": _token_cache,
        "connection_timeout": connect_s,
        "read_timeout": timeout_s,
        "timeout": int(timeout_s) if timeout_s >= 1 else 1,
    }
    if not interactive:
        kwargs["disable_automatic_authentication"] = True
    if record:
        kwargs["authentication_record"] = record

    credential = InteractiveBrowserCredential(**kwargs)
    if record:
        try:
            credential.get_token(*scopes)
        except Exception as exc:
            # Stale auth record / missing refresh token — force interactive login
            # when a TTY is available. Do not wrap save_token_cache here: its
            # OSError (e.g. O_NOFOLLOW) must surface, and get_token transport
            # OSErrors must still fall through when interactive is allowed.
            if not interactive:
                raise _classify_get_token_error(exc) from exc
        else:
            save_token_cache(cfg)
            return credential

    if not interactive:
        raise AuthRequiredError(
            "Authentication required. Run `blumkin auth login` on a TTY "
            "(agent shells should set BLUMKIN_NONINTERACTIVE=1 and never open a browser)."
        )

    record = credential.authenticate(scopes=scopes)
    secret_store.invalidate_agent_cache(cfg)
    _save_auth_record(cfg, record)
    save_token_cache(cfg)
    return credential


def effective_scopes(config: BlumkinConfig | None = None) -> list[str]:
    """Return MSAL scopes for the current config (Phase 4 add-ons optional)."""
    cfg = config or load_config()
    scopes = list(BASE_SCOPES)
    if cfg.docs_scopes:
        scopes.extend(DOCS_SCOPES)
    if cfg.files_scopes:
        scopes.extend(FILES_SCOPES)
    if cfg.wo1162425_scopes:
        scopes.extend(WO1162425_SCOPES)
    if cfg.account_type == "personal":
        scopes = [s for s in scopes if s not in PERSONAL_ACCOUNT_UNSUPPORTED_SCOPES]
    return scopes


def format_scope_gap(*, current: Iterable[str], missing: Iterable[str]) -> str:
    """Render the granted-vs-needed scope sets so the gap is obvious (issue #133).

    Provider scope names are shortened for display (Google's full
    ``https://www.googleapis.com/auth/...`` URIs collapse to the trailing
    segment; Microsoft's bare names pass through unchanged).
    """
    current_s = ", ".join(sorted(_short_scope(s) for s in current)) or "(none)"
    missing_s = ", ".join(sorted(_short_scope(s) for s in missing)) or "(none)"
    return f"current scopes:  {current_s}\nmissing scopes:  {missing_s}"


def interactive_auth_allowed() -> bool:
    """False for non-TTY or when ``BLUMKIN_NONINTERACTIVE`` is truthy."""
    raw = os.environ.get("BLUMKIN_NONINTERACTIVE", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return False
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def refresh_silent(config: BlumkinConfig | None = None) -> dict[str, Any]:
    """Force silent ``get_token`` + persist cache; never open a browser."""
    create_credential(config, allow_interactive=False)
    return status_dict(config)


def logout(config: BlumkinConfig | None = None) -> None:
    global _cache_bound_cfg, _cache_bound_key
    cfg = config or load_config()
    secret_store.delete(cfg, "token_cache")
    secret_store.delete(cfg, "auth_record")
    # Drop in-memory cache so atexit cannot recreate deleted secrets.
    _token_cache.deserialize("")
    if _cache_bound_key == _cache_key(cfg):
        _cache_bound_cfg = None
        _cache_bound_key = None


def reload_token_cache_from_disk(config: BlumkinConfig | None = None) -> None:
    """Force re-read MSAL cache from disk (e.g. after a test mutates the file)."""
    global _cache_bound_cfg, _cache_bound_key
    cfg = config or load_config()
    _cache_bound_cfg = None
    _cache_bound_key = None
    _ensure_cache(cfg)


def save_token_cache(config: BlumkinConfig | None = None) -> None:
    cfg = config or load_config()
    if _cache_bound_key != _cache_key(cfg):
        return
    if _token_cache.has_state_changed:
        secret_store.write_text(cfg, "token_cache", _token_cache.serialize())


def status_dict(config: BlumkinConfig | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    # Read auth_record and token_cache together in one round trip and reuse
    # it below - they share one keychain item (see
    # `secret_store._BUNDLED_KINDS`), so `_access_token_expiry`,
    # `_granted_scopes_from_cache`, both presence flags, and
    # `token_storage_backend` each independently reading/probing their own
    # kind would still cost a second keyring round trip (and, on a Keychain
    # that reprompts on every access rather than remembering "Always Allow",
    # a second authorization prompt) for the exact same item.
    bundle, cache_backend = secret_store.read_ms_bundle_and_backend(cfg)
    raw_cache = bundle.get("token_cache")
    access = _access_token_expiry(raw_cache)
    requested = effective_scopes(cfg)
    granted = _granted_scopes_from_cache(raw_cache, cfg, requested)
    return {
        "access_token_expires_at": access.get("expires_at"),
        "access_token_expires_in_seconds": access.get("expires_in_seconds"),
        "access_token_expired": access.get("expired"),
        "account_type": cfg.account_type,
        "auth_record": "auth_record" in bundle,
        "client_id_configured": bool(cfg.client_id),
        "config_dir": str(cfg.config_dir),
        "config_path": str(cfg.config_path),
        "docs_scopes": cfg.docs_scopes,
        "files_scopes": cfg.files_scopes,
        "wo1162425_scopes": cfg.wo1162425_scopes,
        "granted_scopes": sorted(granted),
        # Empty until a cache exists: nothing to diff a fresh, never-logged-in
        # profile against (auth_required already covers that state).
        "missing_scopes": sorted(_missing_scopes(requested, granted)) if granted else [],
        "refresh_token_present": access.get("refresh_token_present", False),
        "requested_scopes": requested,
        "tenant_id": cfg.tenant_id,
        "token_cache": raw_cache is not None,
        # token_cache (not auth_record): it is the one refreshed - and thus
        # re-serialized/re-persisted - on virtually every silent auth call,
        # so it is the secret most likely to reveal a backend that only
        # falls back to the file at write time (issue #287 review).
        "token_storage_backend": cache_backend,
    }


def _access_token_expiry(raw: str | None) -> dict[str, Any]:
    """Read earliest access-token expires_on from the MSAL cache (no secrets).

    Takes the already-read cache text rather than reading it itself, so a
    caller building a full status payload (``status_dict``) does not pay for
    a second keyring round trip on top of its own read of the same secret.
    """
    out: dict[str, Any] = {
        "expired": None,
        "expires_at": None,
        "expires_in_seconds": None,
        "refresh_token_present": False,
    }
    if raw is None:
        return out
    try:
        data = json.loads(raw)
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


def _cache_key(cfg: BlumkinConfig) -> tuple[str, str]:
    """Identify the profile a bound in-memory cache belongs to (config dir + profile).

    A tuple, not an f-string join: ``:`` does not safely delimit the two
    values (e.g. ``("/tmp/a:work", "one")`` and ``("/tmp/a", "work:one")``
    would otherwise collide onto the same string key), and a collision here
    lets ``_ensure_cache``/``save_token_cache`` persist one profile's token
    cache into another's (issue #287 review).
    """
    return (str(cfg.config_dir), cfg.profile)


def _classify_get_token_error(exc: BaseException) -> AuthError:
    """Map a non-interactive ``get_token`` failure to a typed auth error."""
    if isinstance(exc, OSError):
        # TimeoutError and most requests/httpx transport errors are OSError
        # subclasses — a real network problem, not a bad grant.
        return AuthTransientError(
            f"Microsoft token refresh hit a transient network error: {exc}. Safe to retry."
        )
    msg = str(exc)
    # MSAL/AAD use the standard OAuth2 (RFC 6749 §5.2) codes for a token-endpoint
    # outage - server_error / temporarily_unavailable - distinct from a dead grant
    # (issue #133 review: a plain HTTP 5xx must not read as "re-login").
    if "temporarily_unavailable" in msg or "server_error" in msg:
        return AuthTransientError(
            f"Microsoft token endpoint returned a transient error: {exc}. Safe to retry."
        )
    if "invalid_grant" in msg or "AADSTS70008" in msg or "AADSTS700082" in msg:
        return AuthRequiredError(
            "Microsoft refresh token was revoked or expired. Run `blumkin auth login` "
            "on a TTY, then retry."
        )
    return AuthRequiredError(
        "Silent token refresh failed. Run `blumkin auth login` on a TTY "
        "(or unset BLUMKIN_NONINTERACTIVE), then retry."
    )


def _ensure_cache(cfg: BlumkinConfig) -> None:
    global _atexit_registered, _cache_bound_cfg, _cache_bound_key
    key = _cache_key(cfg)
    if _cache_bound_key == key:
        return
    _token_cache.deserialize("")
    cached = secret_store.read_text(cfg, "token_cache")
    if cached is not None:
        _token_cache.deserialize(cached)
    _cache_bound_cfg = cfg
    _cache_bound_key = key
    # Register once: save only the currently bound profile (never stale ones).
    if not _atexit_registered:
        atexit.register(_save_bound_token_cache_at_exit)
        _atexit_registered = True


def _granted_scopes_from_cache(
    raw: str | None, cfg: BlumkinConfig, requested: Iterable[str]
) -> frozenset[str]:
    """Bare scope names granted per the MSAL cache's ``AccessToken`` ``target`` claims.

    Empty when there is no cache yet — nothing to diff a fresh, never-logged-in
    profile against. MSAL's ``SerializableTokenCache`` never prunes, so an entry
    for a previously configured ``client_id`` (issue #133 review) or an earlier,
    wider ``effective_scopes(cfg)`` (issue #133 review, round 3 - e.g. a scope
    toggled off, or an Entra grant later shrunk) can linger in the same file. A
    scope only counts when it is both in an entry for the *active* client **and**
    still in ``requested`` - otherwise a stale entry could mask a real gap for
    the client's *current* configuration. Unlike the client_id filter, entries
    are **not** filtered by ``expires_on``: access tokens live ~1h and this is
    read by ``auth status`` / ``doctor`` without a refresh first, so excluding
    expired-but-current entries would report an empty (falsely reassuring) gap
    in the routine post-expiry steady state (issue #133 review, round 2) -
    Google's equivalent (``persisted_granted_scopes``, reading the token file)
    has no expiry filter either, for the same reason.

    Takes the already-read cache text (``raw``) rather than reading it itself,
    so ``status_dict`` does not pay for a second keyring round trip on top of
    its own read of the same secret. Filters on ``cfg.client_id`` (the
    ``config.toml`` value), not ``_resolved_client_id`` (issue #368's
    keychain-first resolution) - for the same reason: ``status_dict`` must
    stay a single keychain touch (the bundled auth_record/token_cache read),
    so an ``ms_client_id`` vaulted-only-in-keychain profile keeps this diff
    scoped to whatever ``config.toml`` says (doctor/status keychain-aware
    reporting is a tracked follow-up, not this slice).
    """
    if raw is None:
        return frozenset()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError, OSError:
        return frozenset()
    requested_casefold = {s.casefold() for s in requested}
    scopes: set[str] = set()
    for entry in (data.get("AccessToken") or {}).values():
        if not isinstance(entry, dict):
            continue
        if entry.get("client_id") != cfg.client_id:
            continue
        target = entry.get("target")
        if not isinstance(target, str):
            continue
        for raw in target.split():
            name = raw.rsplit("/", 1)[-1]
            if name.casefold() in requested_casefold:
                scopes.add(name)
    return frozenset(scopes)


def _load_auth_record(cfg: BlumkinConfig) -> AuthenticationRecord | None:
    raw = secret_store.read_text(cfg, "auth_record")
    if raw is None:
        return None
    try:
        return AuthenticationRecord.deserialize(raw)
    except Exception:
        return None


def _missing_scopes(requested: Iterable[str], granted: frozenset[str]) -> set[str]:
    """Requested scope names not present in ``granted`` (case-insensitive)."""
    granted_casefold = {g.casefold() for g in granted}
    return {s for s in requested if s.casefold() not in granted_casefold}


def _save_auth_record(cfg: BlumkinConfig, record: AuthenticationRecord) -> None:
    secret_store.write_text(cfg, "auth_record", record.serialize())


def _save_bound_token_cache_at_exit() -> None:
    """Persist the in-memory MSAL cache to the currently bound profile only."""
    if _cache_bound_cfg is None or not _token_cache.has_state_changed:
        return
    try:
        secret_store.write_text(_cache_bound_cfg, "token_cache", _token_cache.serialize())
    except OSError:
        # Avoid "Exception ignored" on atexit when the secret path is a symlink
        # or the filesystem rejects mode/write; process is already exiting.
        pass


def _short_scope(scope: str) -> str:
    """Collapse a Google scope URI to its trailing segment; other scopes pass through."""
    return scope.removeprefix("https://www.googleapis.com/auth/")
