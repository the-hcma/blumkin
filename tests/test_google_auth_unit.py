"""Unit tests for Google auth error typing and scope auto-escalation (issue #133)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError, TransportError

from blumkin.auth import AuthRequiredError, AuthTransientError, MissingScopeError
from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.providers import google_auth
from blumkin.providers.google_auth import (
    CALENDAR_SCOPES,
    CHAT_SCOPES,
    GOOGLE_SCOPES,
    MAIL_READ_SCOPES,
    MAIL_WRITE_SCOPES,
    PEOPLE_SCOPES,
)
from blumkin.providers.kind import ProviderKind


def test_classify_refresh_error_invalid_grant_is_auth_required() -> None:
    exc = google_auth._classify_refresh_error(RefreshError("invalid_grant: Token has expired"))
    assert isinstance(exc, AuthRequiredError)


def test_classify_refresh_error_retryable_is_transient() -> None:
    """A 5xx token-endpoint outage (google-auth marks it retryable) is not a bad grant."""
    exc = google_auth._classify_refresh_error(
        RefreshError("server_error: internal error", retryable=True)
    )
    assert isinstance(exc, AuthTransientError)


def test_classify_refresh_error_transport_is_transient() -> None:
    exc = google_auth._classify_refresh_error(TransportError("Connection reset by peer"))
    assert isinstance(exc, AuthTransientError)


def test_classify_refresh_error_unknown_falls_back_to_auth_required() -> None:
    exc = google_auth._classify_refresh_error(RuntimeError("boom"))
    assert isinstance(exc, AuthRequiredError)


def test_get_credentials_noninteractive_narrows_gate_to_required_subset(tmp_path: Path) -> None:
    """A subset grant must keep working for the command that only needs that subset.

    Regression from issue #133 review: `people resolve` degrades gracefully on a
    contacts-only grant and `mail list` only needs gmail.readonly - the
    non-interactive fail-fast gate must not block either just because the build's
    full GOOGLE_SCOPES union is not entirely granted.
    """
    cfg = _cfg(tmp_path)
    _write_valid_token(cfg, scopes=sorted(PEOPLE_SCOPES))

    result = google_auth.get_credentials(
        cfg, allow_interactive=False, required_scopes=PEOPLE_SCOPES
    )
    assert result.valid

    with pytest.raises(MissingScopeError):
        google_auth.get_credentials(cfg, allow_interactive=False, required_scopes=CHAT_SCOPES)


def test_get_credentials_noninteractive_sufficient_scope_returns_creds(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _write_valid_token(cfg, scopes=sorted(GOOGLE_SCOPES))

    result = google_auth.get_credentials(cfg, allow_interactive=False)

    assert result.valid


def test_get_credentials_noninteractive_valid_but_insufficient_scope_fails_fast(
    tmp_path: Path,
) -> None:
    """Bug from issue #133: a valid-but-scope-short token must not be handed back."""
    cfg = _cfg(tmp_path)
    granted = {"https://www.googleapis.com/auth/gmail.readonly"}
    _write_valid_token(cfg, scopes=sorted(granted))

    with (
        patch.object(google_auth, "_consent_once") as consent_once,
        pytest.raises(MissingScopeError) as excinfo,
    ):
        google_auth.get_credentials(cfg, allow_interactive=False)

    consent_once.assert_not_called()
    assert excinfo.value.current == granted
    assert excinfo.value.missing == GOOGLE_SCOPES - granted


def test_google_scopes_is_the_union_of_every_required_subset() -> None:
    """Every per-skill-area subset must stay covered by the build-wide scope set.

    Guards against the two drifting apart silently (they are separate literals,
    not derived from each other, for lexicographic ordering - see the comment
    above CALENDAR_SCOPES in google_auth.py).
    """
    directory_readonly = frozenset({"https://www.googleapis.com/auth/directory.readonly"})
    assert GOOGLE_SCOPES == (
        CALENDAR_SCOPES
        | CHAT_SCOPES
        | MAIL_READ_SCOPES
        | MAIL_WRITE_SCOPES
        | PEOPLE_SCOPES
        | directory_readonly
    )


def test_interactive_consent_forces_prompt_immediately_when_grant_already_insufficient(
    tmp_path: Path,
) -> None:
    """A stored-but-short grant must ask for full consent on the very first attempt."""
    cfg = _cfg(tmp_path)
    _write_token(cfg, scopes=["https://www.googleapis.com/auth/gmail.readonly"])
    final_creds = MagicMock()

    with patch.object(google_auth, "_consent_once", return_value=final_creds) as consent_once:
        google_auth._run_interactive_consent(cfg)

    consent_once.assert_called_once_with(cfg, force_consent=True)


def test_interactive_consent_retries_once_on_partial_grant_then_succeeds(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    partial = frozenset({"https://www.googleapis.com/auth/gmail.readonly"})
    warning = _scope_warning('Scope has changed from "" to "gmail.readonly".', sorted(partial))
    final_creds = MagicMock()

    with patch.object(
        google_auth, "_consent_once", side_effect=[warning, final_creds]
    ) as consent_once:
        result = google_auth._run_interactive_consent(cfg)

    assert result is final_creds
    assert consent_once.call_count == 2
    # Second attempt must force prompt=consent so the operator sees every box.
    assert consent_once.call_args_list[1].kwargs == {"force_consent": True}


def test_interactive_consent_stops_after_second_partial_grant(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    first = _scope_warning(
        'Scope has changed from "" to "gmail.readonly".',
        ["https://www.googleapis.com/auth/gmail.readonly"],
    )
    second_scopes = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/chat.messages",
    ]
    second = _scope_warning(
        'Scope has changed from "gmail.readonly" to "gmail.readonly chat.messages".',
        second_scopes,
    )

    with (
        patch.object(google_auth, "_consent_once", side_effect=[first, second]) as consent_once,
        pytest.raises(MissingScopeError) as excinfo,
    ):
        google_auth._run_interactive_consent(cfg)

    assert consent_once.call_count == 2
    assert excinfo.value.current == frozenset(second_scopes)
    assert "https://www.googleapis.com/auth/gmail.compose" in excinfo.value.missing
    assert excinfo.value.missing == GOOGLE_SCOPES - excinfo.value.current


def test_status_dict_missing_scopes_empty_before_first_login(tmp_path: Path) -> None:
    payload = google_auth.status_dict(_cfg(tmp_path))
    assert payload["granted_scopes"] == []
    assert payload["missing_scopes"] == []


def test_status_dict_reports_granted_and_missing_scopes(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _write_valid_token(cfg, scopes=["https://www.googleapis.com/auth/gmail.readonly"])
    payload = google_auth.status_dict(cfg)
    assert payload["granted_scopes"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert payload["missing_scopes"] == sorted(
        GOOGLE_SCOPES - {"https://www.googleapis.com/auth/gmail.readonly"}
    )


def _cfg(config_dir: Path, *, oauth_file: Path | None = None) -> BlumkinConfig:
    path = oauth_file
    if path is None:
        path = config_dir / "desktop-client.json"
        if not path.is_file():
            path.write_text(
                '{"installed": {'
                '"client_id": "fake-google-desktop-client.apps.googleusercontent.com", '
                '"client_secret": "fake-google-client-secret"}}'
            )
    return BlumkinConfig(
        client_id="fake-google-desktop-client.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=path,
        graph_timeout_seconds=60.0,
        legacy_flat=True,
        mail_signature=MailSignatureConfig(),
        profile="default",
        provider=ProviderKind.GOOGLE,
        tags=(),
        tenant_id="",
        wo1162425_scopes=False,
    )


def _scope_warning(message: str, new_scope: list[str]) -> Warning:
    """Build the ``Warning`` oauthlib raises on a partial grant, with its scope attrs."""
    warning = Warning(message)
    setattr(warning, "new_scope", new_scope)  # noqa: B010 - mirrors oauthlib's own dynamic attr
    return warning


def _write_token(cfg: BlumkinConfig, *, scopes: list[str]) -> None:
    cfg.google_token_path.write_text(
        json.dumps(
            {
                "client_id": "fake-google-desktop-client.apps.googleusercontent.com",
                "client_secret": "fake-google-client-secret",
                "refresh_token": "fake-refresh",
                "scopes": scopes,
                "token": "fake-access",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )


def _write_valid_token(cfg: BlumkinConfig, *, scopes: list[str]) -> None:
    """Write a token that ``Credentials.from_authorized_user_info`` loads as valid."""
    expiry = datetime.now(UTC) + timedelta(hours=1)
    cfg.google_token_path.write_text(
        json.dumps(
            {
                "client_id": "fake-google-desktop-client.apps.googleusercontent.com",
                "client_secret": "fake-google-client-secret",
                "expiry": expiry.isoformat().replace("+00:00", "Z"),
                "refresh_token": "fake-refresh",
                "scopes": scopes,
                "token": "fake-access",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )
