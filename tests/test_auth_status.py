"""Tests for auth status expiry parsing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from blumkin.auth import status_dict


def test_status_reads_access_token_expiry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BLUMKIN_CLIENT_ID", "test-client")
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
    (tmp_path / "msal_token_cache.json").write_text(json.dumps(cache))
    (tmp_path / "auth_record.json").write_text("{}")
    (tmp_path / "config.toml").write_text('client_id = "test-client"\n')
    payload = status_dict()
    assert payload["refresh_token_present"] is True
    assert payload["access_token_expired"] is False
    assert payload["access_token_expires_at"] is not None
    assert payload["access_token_expires_in_seconds"] is not None
    assert payload["access_token_expires_in_seconds"] > 0
