"""Unit tests for config loading."""

from __future__ import annotations

from pathlib import Path

from blumkin.config import load_config


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
