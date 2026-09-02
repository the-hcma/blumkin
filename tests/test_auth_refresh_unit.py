"""CI unit tests for access-token expiry reporting and silent-refresh wiring."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from blumkin.auth import (
    AuthRequiredError,
    AuthTransientError,
    SecretWriteError,
    create_credential,
    status_dict,
)


def test_create_credential_falls_back_to_authenticate_when_get_token_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope/cache miss on get_token must fall through to interactive authenticate()."""
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "test-client"\ntenant_id = "contoso.com"\n')
    (tmp_path / "msal_token_cache.json").write_text("{}")
    (tmp_path / "auth_record.json").write_text("record-bytes")

    fake_record = MagicMock(name="AuthenticationRecord")
    fresh_record = MagicMock(name="FreshAuthenticationRecord")
    fake_cred = MagicMock(name="InteractiveBrowserCredential")
    fake_cred.get_token.side_effect = Exception("AADSTS70011: scope mismatch")
    fake_cred.authenticate.return_value = fresh_record

    with (
        patch("blumkin.auth.AuthenticationRecord.deserialize", return_value=fake_record),
        patch("blumkin.auth.InteractiveBrowserCredential", return_value=fake_cred),
        patch("blumkin.auth._save_auth_record") as save_record,
        patch("blumkin.auth.save_token_cache") as save_cache,
    ):
        cred = create_credential(allow_interactive=True)

    assert cred is fake_cred
    fake_cred.get_token.assert_called_once()
    fake_cred.authenticate.assert_called_once()
    save_record.assert_called_once()
    assert save_cache.call_count >= 1


def test_create_credential_falls_back_when_get_token_raises_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transport OSError from get_token must still fall through to authenticate()."""
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "test-client"\ntenant_id = "contoso.com"\n')
    (tmp_path / "msal_token_cache.json").write_text("{}")
    (tmp_path / "auth_record.json").write_text("record-bytes")

    fake_record = MagicMock(name="AuthenticationRecord")
    fresh_record = MagicMock(name="FreshAuthenticationRecord")
    fake_cred = MagicMock(name="InteractiveBrowserCredential")
    fake_cred.get_token.side_effect = OSError("Name or service not known")
    fake_cred.authenticate.return_value = fresh_record

    with (
        patch("blumkin.auth.AuthenticationRecord.deserialize", return_value=fake_record),
        patch("blumkin.auth.InteractiveBrowserCredential", return_value=fake_cred),
        patch("blumkin.auth._save_auth_record"),
        patch("blumkin.auth.save_token_cache"),
    ):
        cred = create_credential(allow_interactive=True)

    assert cred is fake_cred
    fake_cred.authenticate.assert_called_once()


def test_create_credential_noninteractive_invalid_grant_is_auth_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BLUMKIN_NONINTERACTIVE", "1")
    (tmp_path / "config.toml").write_text('client_id = "test-client"\ntenant_id = "contoso.com"\n')
    (tmp_path / "msal_token_cache.json").write_text("{}")
    (tmp_path / "auth_record.json").write_text("record-bytes")

    fake_record = MagicMock(name="AuthenticationRecord")
    fake_cred = MagicMock(name="InteractiveBrowserCredential")
    fake_cred.get_token.side_effect = Exception("invalid_grant: AADSTS700082 refresh token expired")

    with (
        patch("blumkin.auth.AuthenticationRecord.deserialize", return_value=fake_record),
        patch("blumkin.auth.InteractiveBrowserCredential", return_value=fake_cred),
        pytest.raises(AuthRequiredError, match="revoked or expired"),
    ):
        create_credential()


def test_create_credential_noninteractive_server_error_is_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AAD's standard OAuth2 server_error/temporarily_unavailable is not a bad grant."""
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BLUMKIN_NONINTERACTIVE", "1")
    (tmp_path / "config.toml").write_text('client_id = "test-client"\ntenant_id = "contoso.com"\n')
    (tmp_path / "msal_token_cache.json").write_text("{}")
    (tmp_path / "auth_record.json").write_text("record-bytes")

    fake_record = MagicMock(name="AuthenticationRecord")
    fake_cred = MagicMock(name="InteractiveBrowserCredential")
    fake_cred.get_token.side_effect = Exception("temporarily_unavailable: AAD is busy")

    with (
        patch("blumkin.auth.AuthenticationRecord.deserialize", return_value=fake_record),
        patch("blumkin.auth.InteractiveBrowserCredential", return_value=fake_cred),
        pytest.raises(AuthTransientError, match="transient"),
    ):
        create_credential()


def test_create_credential_noninteractive_skips_authenticate_on_get_token_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BLUMKIN_NONINTERACTIVE", "1")
    (tmp_path / "config.toml").write_text('client_id = "test-client"\ntenant_id = "contoso.com"\n')
    (tmp_path / "msal_token_cache.json").write_text("{}")
    (tmp_path / "auth_record.json").write_text("record-bytes")

    fake_record = MagicMock(name="AuthenticationRecord")
    fake_cred = MagicMock(name="InteractiveBrowserCredential")
    fake_cred.get_token.side_effect = Exception("AADSTS70011: scope mismatch")

    with (
        patch("blumkin.auth.AuthenticationRecord.deserialize", return_value=fake_record),
        patch("blumkin.auth.InteractiveBrowserCredential", return_value=fake_cred) as ctor,
    ):
        with pytest.raises(ValueError, match="Silent token refresh failed"):
            create_credential()

    kwargs = ctor.call_args.kwargs
    assert kwargs["disable_automatic_authentication"] is True
    fake_cred.authenticate.assert_not_called()


def test_create_credential_noninteractive_transient_get_token_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network OSError from get_token is not a bad grant - it's retryable (issue #133)."""
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BLUMKIN_NONINTERACTIVE", "1")
    (tmp_path / "config.toml").write_text('client_id = "test-client"\ntenant_id = "contoso.com"\n')
    (tmp_path / "msal_token_cache.json").write_text("{}")
    (tmp_path / "auth_record.json").write_text("record-bytes")

    fake_record = MagicMock(name="AuthenticationRecord")
    fake_cred = MagicMock(name="InteractiveBrowserCredential")
    fake_cred.get_token.side_effect = OSError("Network is unreachable")

    with (
        patch("blumkin.auth.AuthenticationRecord.deserialize", return_value=fake_record),
        patch("blumkin.auth.InteractiveBrowserCredential", return_value=fake_cred),
        pytest.raises(AuthTransientError, match="transient"),
    ):
        create_credential()

    fake_cred.authenticate.assert_not_called()


def test_create_credential_reraises_oserror_from_save_token_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Secret-path write failures must not be treated as a stale auth record."""
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "test-client"\ntenant_id = "contoso.com"\n')
    (tmp_path / "msal_token_cache.json").write_text("{}")
    (tmp_path / "auth_record.json").write_text("record-bytes")

    fake_record = MagicMock(name="AuthenticationRecord")
    fake_cred = MagicMock(name="InteractiveBrowserCredential")

    with (
        patch("blumkin.auth.AuthenticationRecord.deserialize", return_value=fake_record),
        patch("blumkin.auth.InteractiveBrowserCredential", return_value=fake_cred),
        patch(
            "blumkin.auth.save_token_cache",
            side_effect=SecretWriteError("cannot write secret file"),
        ),
        pytest.raises(SecretWriteError, match="cannot write secret file"),
    ):
        create_credential()

    fake_cred.get_token.assert_called_once()
    fake_cred.authenticate.assert_not_called()


def test_create_credential_uses_cached_auth_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent path: with AuthenticationRecord + cache, no interactive authenticate()."""
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "test-client"\ntenant_id = "contoso.com"\n')
    (tmp_path / "msal_token_cache.json").write_text("{}")
    (tmp_path / "auth_record.json").write_text("record-bytes")

    fake_record = MagicMock(name="AuthenticationRecord")
    fake_cred = MagicMock(name="InteractiveBrowserCredential")

    with (
        patch("blumkin.auth.AuthenticationRecord.deserialize", return_value=fake_record),
        patch("blumkin.auth.InteractiveBrowserCredential", return_value=fake_cred) as ctor,
    ):
        cred = create_credential()

    assert cred is fake_cred
    ctor.assert_called_once()
    kwargs = ctor.call_args.kwargs
    assert kwargs["authentication_record"] is fake_record
    assert kwargs["client_id"] == "test-client"
    fake_cred.get_token.assert_called_once()
    fake_cred.authenticate.assert_not_called()


def test_status_dict_missing_scopes_empty_without_a_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "test-client"\n')
    payload = status_dict()
    assert payload["granted_scopes"] == []
    assert payload["missing_scopes"] == []


def test_status_dict_reports_granted_and_missing_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    cache = {
        "AccessToken": {
            "entry1": {
                "client_id": "test-client",
                "expires_on": str(int((datetime.now(UTC) + timedelta(hours=1)).timestamp())),
                "target": "https://graph.microsoft.com/User.Read",
            },
            # A leftover entry for a previously configured client, still valid,
            # must not count toward the active client's granted scopes.
            "entry2": {
                "client_id": "stale-other-client",
                "expires_on": str(int((datetime.now(UTC) + timedelta(hours=1)).timestamp())),
                "target": "https://graph.microsoft.com/Mail.ReadWrite",
            },
        },
        "RefreshToken": {"r1": {"client_id": "test-client"}},
    }
    (tmp_path / "msal_token_cache.json").write_text(json.dumps(cache))
    (tmp_path / "auth_record.json").write_text("{}")
    (tmp_path / "config.toml").write_text('client_id = "test-client"\n')
    payload = status_dict()
    assert payload["granted_scopes"] == ["User.Read"]
    assert "Calendars.ReadWrite" in payload["missing_scopes"]
    assert "Mail.ReadWrite" in payload["missing_scopes"]
    assert "User.Read" not in payload["missing_scopes"]


def test_status_reports_expired_access_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    expired = int((datetime.now(UTC) - timedelta(minutes=5)).timestamp())
    cache = {
        "AccessToken": {"entry1": {"expires_on": str(expired), "target": "User.Read"}},
        "RefreshToken": {"r1": {"client_id": "test-client"}},
    }
    (tmp_path / "msal_token_cache.json").write_text(json.dumps(cache))
    (tmp_path / "auth_record.json").write_text("{}")
    (tmp_path / "config.toml").write_text('client_id = "test-client"\n')
    payload = status_dict()
    assert payload["access_token_expired"] is True
    assert payload["refresh_token_present"] is True
    assert payload["access_token_expires_in_seconds"] is not None
    assert payload["access_token_expires_in_seconds"] < 0
