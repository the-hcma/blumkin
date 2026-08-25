"""CI unit tests for access-token expiry reporting and silent-refresh wiring."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from blumkin.auth import create_credential, status_dict


def test_status_reports_expired_access_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BLUMKIN_CLIENT_ID", "test-client")
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


def test_create_credential_uses_cached_auth_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent path: with AuthenticationRecord + cache, no interactive authenticate()."""
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BLUMKIN_CLIENT_ID", "test-client")
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


def test_create_credential_falls_back_to_authenticate_when_get_token_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope/cache miss on get_token must fall through to interactive authenticate()."""
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BLUMKIN_CLIENT_ID", "test-client")
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
        cred = create_credential()

    assert cred is fake_cred
    fake_cred.get_token.assert_called_once()
    fake_cred.authenticate.assert_called_once()
    save_record.assert_called_once()
    assert save_cache.call_count >= 1
