"""Tests for auth status expiry parsing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from blumkin.auth import status_dict


def test_status_reads_access_token_expiry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    expires = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
    cache = {
        "AccessToken": {
            "entry1": {
                "expires_on": str(expires),
                "target": "User.Read",
            }
        },
        "RefreshToken": {"r1": {"client_id": "test-client"}},
    }
    profile_dir = tmp_path / "profiles" / "default"
    profile_dir.mkdir(parents=True)
    (profile_dir / "msal_token_cache.json").write_text(json.dumps(cache))
    (profile_dir / "auth_record.json").write_text("{}")
    (tmp_path / "config.toml").write_text('[profiles.default]\nclient_id = "test-client"\n')
    payload = status_dict()
    assert payload["refresh_token_present"] is True
    assert payload["access_token_expired"] is False
    assert payload["access_token_expires_at"] is not None
    assert payload["access_token_expires_in_seconds"] is not None
    assert payload["access_token_expires_in_seconds"] > 0


def test_status_dict_reports_token_storage_backend(tmp_path: Path, monkeypatch) -> None:
    """`doctor` prints this key verbatim - a drop or rename must fail loudly (issue #287 review)."""
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "test-client"\ntoken_storage = "file"\n'
    )
    assert status_dict()["token_storage_backend"] == "file"

    from blumkin import secret_store

    class _FakeKeyring:
        def get_password(self, service: str, username: str) -> str | None:
            return None

        def set_password(self, service: str, username: str, password: str) -> None:
            pass

        def delete_password(self, service: str, username: str) -> None:
            pass

    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _FakeKeyring())
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "test-client"\ntoken_storage = "keyring"\n'
    )
    assert status_dict()["token_storage_backend"] == "keyring"
