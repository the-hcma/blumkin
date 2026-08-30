"""Unit tests for config loading and multi-profile resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blumkin import auth
from blumkin.config import list_profiles, load_config
from blumkin.providers import google_auth
from blumkin.providers.kind import ProviderConfigError


def test_load_config_from_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'client_id = "abc-123"\ntenant_id = "contoso.com"\ndefault_tz = "UTC"\n'
    )
    cfg = load_config()
    assert cfg.client_id == "abc-123"
    assert cfg.tenant_id == "contoso.com"
    assert cfg.default_tz == "UTC"
    assert cfg.config_dir == tmp_path
    assert cfg.profile == "default"
    assert cfg.legacy_flat is True
    assert cfg.tags == ()
    assert cfg.token_cache_path == tmp_path / "msal_token_cache.json"
    assert cfg.wo1162425_scopes is False
    assert cfg.google_oauth_client_file is None


def test_credential_env_vars_do_not_override_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BLUMKIN_CLIENT_ID", "from-env")
    monkeypatch.setenv("BLUMKIN_TENANT_ID", "env.tenant")
    monkeypatch.setenv("BLUMKIN_TZ", "Europe/London")
    (tmp_path / "config.toml").write_text(
        'client_id = "from-file"\ntenant_id = "file.tenant"\ndefault_tz = "UTC"\n'
    )
    cfg = load_config()
    assert cfg.client_id == "from-file"
    assert cfg.tenant_id == "file.tenant"
    assert cfg.default_tz == "UTC"


def test_missing_tenant_and_tz_have_no_code_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "abc"\n')
    cfg = load_config()
    assert cfg.tenant_id == ""
    assert cfg.default_tz == ""
    assert cfg.provider.value == "microsoft"


def test_google_oauth_client_file_loads_client_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    oauth = tmp_path / "desktop-client.json"
    oauth.write_text(
        '{"installed": {"client_id": "gid.apps.googleusercontent.com", '
        '"client_secret": "not-a-secret"}}'
    )
    (tmp_path / "config.toml").write_text(
        f'provider = "google"\ngoogle_oauth_client_file = "{oauth}"\ndefault_tz = "UTC"\n'
    )
    cfg = load_config()
    assert cfg.provider.value == "google"
    assert cfg.client_id == "gid.apps.googleusercontent.com"
    assert cfg.google_oauth_client_file == oauth


def _write_multi_profile(tmp_path: Path, oauth: Path | None = None) -> None:
    oauth_line = ""
    if oauth is not None:
        oauth_line = f'google_oauth_client_file = "{oauth}"\n'
    (tmp_path / "config.toml").write_text(
        'default_profile = "work"\n'
        "\n"
        "[profiles.personal]\n"
        'provider = "google"\n'
        'default_tz = "America/New_York"\n'
        f"{oauth_line}"
        'tags = ["@personal", "personal", "google", "gmail"]\n'
        "\n"
        "[profiles.work]\n"
        'provider = "microsoft"\n'
        'client_id = "ms-client"\n'
        'tenant_id = "brk.tech"\n'
        'default_tz = "America/New_York"\n'
        'tags = ["@work", "work", "microsoft", "m365"]\n'
        "\n"
        "[profiles.work.mail.signature]\n"
        "enabled = true\n"
        'name = "Ada"\n'
    )


def test_multi_profile_default_and_token_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_PROFILE", raising=False)
    oauth = tmp_path / "desktop-client.json"
    oauth.write_text(
        '{"installed": {"client_id": "gid.apps.googleusercontent.com", '
        '"client_secret": "not-a-secret"}}'
    )
    _write_multi_profile(tmp_path, oauth)
    cfg = load_config()
    assert cfg.profile == "work"
    assert cfg.legacy_flat is False
    assert cfg.provider.value == "microsoft"
    assert cfg.client_id == "ms-client"
    assert cfg.mail_signature.enabled is True
    assert cfg.mail_signature.name == "Ada"
    assert cfg.tags == ("@work", "m365", "microsoft", "work")
    assert cfg.profile_dir == tmp_path / "profiles" / "work"
    assert cfg.token_cache_path == tmp_path / "profiles" / "work" / "msal_token_cache.json"
    assert cfg.config_path == tmp_path / "config.toml"


def test_load_config_by_tag_and_cli_profile_wins_over_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    oauth = tmp_path / "desktop-client.json"
    oauth.write_text(
        '{"installed": {"client_id": "gid.apps.googleusercontent.com", '
        '"client_secret": "not-a-secret"}}'
    )
    _write_multi_profile(tmp_path, oauth)
    monkeypatch.setenv("BLUMKIN_PROFILE", "work")
    cfg = load_config(profile="@personal")
    assert cfg.profile == "personal"
    assert cfg.provider.value == "google"
    assert cfg.google_token_path == tmp_path / "profiles" / "personal" / "google_token.json"


def test_load_config_blumkin_profile_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    oauth = tmp_path / "desktop-client.json"
    oauth.write_text(
        '{"installed": {"client_id": "gid.apps.googleusercontent.com", '
        '"client_secret": "not-a-secret"}}'
    )
    _write_multi_profile(tmp_path, oauth)
    monkeypatch.setenv("BLUMKIN_PROFILE", "gmail")
    cfg = load_config()
    assert cfg.profile == "personal"


def test_load_config_ambiguous_without_default_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_PROFILE", raising=False)
    (tmp_path / "config.toml").write_text(
        "[profiles.a]\n"
        'client_id = "a"\n'
        'tags = ["one"]\n'
        "\n"
        "[profiles.b]\n"
        'client_id = "b"\n'
        'tags = ["two"]\n'
    )
    with pytest.raises(ProviderConfigError, match="multiple profiles"):
        load_config()


def test_load_config_unknown_selector_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.work]\nclient_id = "ms"\ntags = ["@work"]\n')
    with pytest.raises(ProviderConfigError, match="matches no profile"):
        load_config(profile="@personal")


def test_load_config_duplicate_tag_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "[profiles.a]\n"
        'client_id = "a"\n'
        'tags = ["shared"]\n'
        "\n"
        "[profiles.b]\n"
        'client_id = "b"\n'
        'tags = ["@Shared"]\n'
    )
    with pytest.raises(ProviderConfigError, match="multiple profiles by tag"):
        load_config(profile="shared")


def test_list_profiles_safe_summaries(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    oauth = tmp_path / "desktop-client.json"
    oauth.write_text(
        '{"installed": {"client_id": "gid.apps.googleusercontent.com", '
        '"client_secret": "not-a-secret"}}'
    )
    _write_multi_profile(tmp_path, oauth)
    work_dir = tmp_path / "profiles" / "work"
    work_dir.mkdir(parents=True)
    (work_dir / "msal_token_cache.json").write_text("{}")
    (work_dir / "auth_record.json").write_text("{}")
    personal_dir = tmp_path / "profiles" / "personal"
    personal_dir.mkdir(parents=True)
    (personal_dir / "google_token.json").write_text("{}")

    summaries = list_profiles()
    assert [item["name"] for item in summaries] == ["personal", "work"]
    work = summaries[1]
    assert work["is_default"] is True
    assert work["provider"] == "microsoft"
    assert work["auth_present"]["msal_token_cache"] is True
    assert work["auth_present"]["auth_record"] is True
    assert work["auth_present"]["google_token"] is False
    personal = summaries[0]
    assert personal["is_default"] is False
    assert personal["provider"] == "google"
    assert personal["auth_present"]["google_token"] is True
    dumped = json.dumps(summaries)
    assert "not-a-secret" not in dumped
    assert "ms-client" not in dumped


def test_missing_config_has_zero_profiles(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_PROFILE", raising=False)
    assert list_profiles() == []
    with pytest.raises(ProviderConfigError, match="no profiles configured"):
        load_config()


def test_named_profile_secret_writes_under_profile_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_PROFILE", raising=False)
    oauth = tmp_path / "desktop-client.json"
    oauth.write_text(
        '{"installed": {"client_id": "gid.apps.googleusercontent.com", '
        '"client_secret": "not-a-secret"}}'
    )
    _write_multi_profile(tmp_path, oauth)

    work = load_config(profile="work")
    auth._cache_bound_path = str(work.token_cache_path)
    auth._token_cache.deserialize("")
    auth._token_cache.has_state_changed = True
    auth.save_token_cache(work)
    assert work.token_cache_path.is_file()
    assert work.token_cache_path == tmp_path / "profiles" / "work" / "msal_token_cache.json"
    assert not (tmp_path / "msal_token_cache.json").exists()

    class _Record:
        def serialize(self) -> str:
            return '{"homeAccountId":"test"}'

    auth._save_auth_record(work, _Record())  # type: ignore[arg-type]
    assert work.auth_record_path == tmp_path / "profiles" / "work" / "auth_record.json"
    assert work.auth_record_path.is_file()
    assert not (tmp_path / "auth_record.json").exists()

    personal = load_config(profile="personal")

    class _Creds:
        def to_json(self) -> str:
            return (
                '{"token": "t", "refresh_token": "r", '
                '"token_uri": "https://oauth2.googleapis.com/token", "client_id": "gid"}'
            )

    google_auth._save_credentials(personal, _Creds())  # type: ignore[arg-type]
    assert personal.google_token_path == tmp_path / "profiles" / "personal" / "google_token.json"
    assert personal.google_token_path.is_file()
    assert not (tmp_path / "google_token.json").exists()
