"""Google Workspace OAuth (installed / desktop client, PKCE-friendly)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from blumkin.auth import SecretWriteError, _ensure_secret_dir, interactive_auth_allowed
from blumkin.config import BlumkinConfig, google_oauth_installed_client, load_config
from blumkin.providers.google_http import refresh_request
from blumkin.providers.kind import ProviderConfigError

GOOGLE_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
    }
)


def get_credentials(
    config: BlumkinConfig | None = None,
    *,
    allow_interactive: bool | None = None,
) -> Credentials:
    """Load or obtain Google OAuth credentials; refresh silently when possible."""
    cfg = config or load_config()
    if not cfg.client_id and cfg.google_oauth_client_file is None:
        raise ValueError(
            "Missing Google OAuth client — set google_oauth_client_file (path to "
            "Desktop client JSON) or client_id in config.toml."
        )
    interactive = interactive_auth_allowed() if allow_interactive is None else allow_interactive
    creds = _load_credentials(cfg)
    if creds is not None:
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(refresh_request(cfg))
            except Exception:
                if not interactive:
                    raise ValueError(
                        "Silent token refresh failed. Run `blumkin auth login` on a TTY "
                        "(or unset BLUMKIN_NONINTERACTIVE), then retry."
                    ) from None
            else:
                _save_credentials(cfg, creds)
                return creds
        elif not interactive:
            raise ValueError(
                "Silent token refresh failed. Run `blumkin auth login` on a TTY "
                "(or unset BLUMKIN_NONINTERACTIVE), then retry."
            )

    if not interactive:
        raise ValueError(
            "Authentication required. Run `blumkin auth login` on a TTY "
            "(agent shells should set BLUMKIN_NONINTERACTIVE=1 and never open a browser)."
        )

    flow = InstalledAppFlow.from_client_config(
        _client_config(cfg),
        scopes=sorted(GOOGLE_SCOPES),
    )
    creds = flow.run_local_server(port=0)
    if not isinstance(creds, Credentials):
        raise TypeError("expected google.oauth2.credentials.Credentials from InstalledAppFlow")
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


def refresh_silent(config: BlumkinConfig | None = None) -> dict[str, Any]:
    """Force silent credential refresh; never open a browser."""
    get_credentials(config, allow_interactive=False)
    return status_dict(config)


def status_dict(config: BlumkinConfig | None = None) -> dict[str, Any]:
    """Auth status without secrets (aligned keys with Microsoft status where possible)."""
    cfg = config or load_config()
    access = _access_token_expiry(cfg)
    return {
        "access_token_expires_at": access.get("expires_at"),
        "access_token_expires_in_seconds": access.get("expires_in_seconds"),
        "access_token_expired": access.get("expired"),
        "auth_record": cfg.google_token_path.is_file(),
        "client_id_configured": bool(cfg.client_id) or cfg.google_oauth_client_file is not None,
        "config_dir": str(cfg.config_dir),
        "config_path": str(cfg.config_path),
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


def _save_credentials(cfg: BlumkinConfig, creds: Credentials) -> None:
    _ensure_secret_dir(cfg.profile_dir, stop_at=cfg.config_dir)
    payload = json.loads(creds.to_json())
    secret = _client_secret_from_oauth_file(cfg)
    if secret:
        payload["client_secret"] = secret
    else:
        payload.setdefault("client_secret", "")
    _write_secret_text(cfg.google_token_path, json.dumps(payload))


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
