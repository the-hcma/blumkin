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


def test_status_dict_reports_account_type(tmp_path: Path, monkeypatch) -> None:
    """`doctor` prints this key verbatim - a drop or rename must fail loudly (issue #297)."""
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "test-client"\naccount_type = "personal"\n'
    )
    assert status_dict()["account_type"] == "personal"

    (tmp_path / "config.toml").write_text('[profiles.default]\nclient_id = "test-client"\n')
    assert status_dict()["account_type"] == "organizational"


# A regression test proving `status_dict()` makes only one keyring round trip
# for `auth_record`/`token_cache` combined lives in
# tests/test_secret_store_keyring_unit.py
# (test_status_dict_touches_one_keychain_item_for_both_bundled_kinds) - it
# needs the real `_bundle_*` shape (a shared JSON item), which belongs with
# the rest of that module's keyring-backend tests.


def test_status_dict_counts_granted_scopes_for_a_vaulted_only_client_id(
    tmp_path: Path, monkeypatch
) -> None:
    """A profile whose ``ms_client_id`` lives only in the OS keychain (no
    ``client_id`` in ``config.toml``) must still get an accurate
    ``granted_scopes`` / capabilities read.

    The cache diff used to filter on ``cfg.client_id`` directly, which is
    the empty string for a vaulted-only profile - every real
    ``AccessToken`` entry (always carrying a non-empty ``client_id``) was
    therefore excluded, so a fully logged-in vaulted-only profile silently
    reported zero granted scopes and every capability as unavailable
    (issue #368 review, round 4)."""
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.default]\ntenant_id = "example.com"\n')

    from blumkin import secret_store
    from blumkin.app_secrets import write_app_secret
    from blumkin.config import load_config

    class _FakeKeyring:
        def __init__(self) -> None:
            self.store: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, username: str) -> str | None:
            return self.store.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            self.store[(service, username)] = password

    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    cfg = load_config()
    write_app_secret(cfg, "ms_client_id", "vaulted-client-id")
    cache = {
        "AccessToken": {
            "entry1": {
                "client_id": "vaulted-client-id",
                "target": "https://graph.microsoft.com/User.Read",
            }
        },
        "RefreshToken": {},
    }
    secret_store.write_text(cfg, "auth_record", "{}")
    secret_store.write_text(cfg, "token_cache", json.dumps(cache))

    payload = status_dict(cfg)

    assert "User.Read" in payload["granted_scopes"]
    assert "User.Read" not in payload["missing_scopes"]


def test_status_dict_counts_granted_scopes_after_a_client_id_migrated_to_the_vault(
    tmp_path: Path, monkeypatch
) -> None:
    """A profile that vaults a *replacement* ``ms_client_id`` while leaving the
    old ``client_id`` in ``config.toml`` (``app_secrets``'s documented
    leave-in-place default) logs in with the vaulted id - the cache's real
    entries carry it, not the stale toml value. The filter must recognize the
    mismatch and fall back to the vaulted id, rather than reporting zero
    granted scopes for a profile that is actually fully logged in (issue #368
    review, round 5)."""
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.default]\nclient_id = "old-toml-client-id"\n')

    from blumkin import secret_store
    from blumkin.app_secrets import write_app_secret
    from blumkin.config import load_config

    class _FakeKeyring:
        def __init__(self) -> None:
            self.store: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, username: str) -> str | None:
            return self.store.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            self.store[(service, username)] = password

    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    cfg = load_config()
    write_app_secret(cfg, "ms_client_id", "new-vaulted-client-id")
    cache = {
        "AccessToken": {
            "entry1": {
                "client_id": "new-vaulted-client-id",
                "target": "https://graph.microsoft.com/User.Read",
            }
        },
        "RefreshToken": {},
    }
    secret_store.write_text(cfg, "auth_record", "{}")
    secret_store.write_text(cfg, "token_cache", json.dumps(cache))

    payload = status_dict(cfg)

    assert "User.Read" in payload["granted_scopes"]
    assert "User.Read" not in payload["missing_scopes"]
