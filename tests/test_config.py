"""Unit tests for config loading."""

from __future__ import annotations

from pathlib import Path

from blumkin.config import load_config


def test_load_config_from_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_CLIENT_ID", raising=False)
    monkeypatch.delenv("BLUMKIN_TENANT_ID", raising=False)
    monkeypatch.delenv("BLUMKIN_WO1162425_SCOPES", raising=False)
    (tmp_path / "config.toml").write_text(
        'client_id = "abc-123"\ntenant_id = "contoso.com"\ndefault_tz = "UTC"\n'
    )
    cfg = load_config()
    assert cfg.client_id == "abc-123"
    assert cfg.tenant_id == "contoso.com"
    assert cfg.default_tz == "UTC"
    assert cfg.config_dir == tmp_path
    assert cfg.wo1162425_scopes is False


def test_env_overrides_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BLUMKIN_CLIENT_ID", "from-env")
    monkeypatch.setenv("BLUMKIN_TENANT_ID", "env.tenant")
    (tmp_path / "config.toml").write_text('client_id = "from-file"\ntenant_id = "file.tenant"\n')
    cfg = load_config()
    assert cfg.client_id == "from-env"
    assert cfg.tenant_id == "env.tenant"
    assert cfg.default_tz == ""


def test_missing_tenant_and_tz_have_no_code_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_CLIENT_ID", raising=False)
    monkeypatch.delenv("BLUMKIN_TENANT_ID", raising=False)
    monkeypatch.delenv("BLUMKIN_TZ", raising=False)
    (tmp_path / "config.toml").write_text('client_id = "abc"\n')
    cfg = load_config()
    assert cfg.tenant_id == ""
    assert cfg.default_tz == ""
    assert cfg.provider.value == "microsoft"
