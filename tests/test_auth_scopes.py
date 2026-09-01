"""Unit tests for effective MSAL scopes."""

from __future__ import annotations

from pathlib import Path

from blumkin.auth import BASE_SCOPES, FILES_SCOPES, WO1162425_SCOPES, effective_scopes
from blumkin.config import BlumkinConfig, MailSignatureConfig, load_config
from blumkin.providers.kind import ProviderKind


def test_effective_scopes_default_excludes_phase4() -> None:
    assert effective_scopes(_cfg(wo1162425_scopes=False)) == BASE_SCOPES


def test_effective_scopes_enabled_includes_phase4() -> None:
    assert effective_scopes(_cfg(wo1162425_scopes=True)) == [*BASE_SCOPES, *WO1162425_SCOPES]
    assert "People.Read" in WO1162425_SCOPES
    assert "People.Read" not in BASE_SCOPES


def test_effective_scopes_files_opt_in() -> None:
    assert effective_scopes(_cfg(wo1162425_scopes=False, files_scopes=True)) == [
        *BASE_SCOPES,
        *FILES_SCOPES,
    ]


def test_files_scopes_from_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "abc"\nfiles_scopes = true\n')
    assert load_config().files_scopes is True


def test_files_scopes_off_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "abc"\n')
    assert load_config().files_scopes is False


def test_wo1162425_scopes_from_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "abc"\nwo1162425_scopes = true\n')
    assert load_config().wo1162425_scopes is True


def test_wo1162425_scopes_from_toml_int(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "abc"\nwo1162425_scopes = 1\n')
    assert load_config().wo1162425_scopes is True


def test_scope_env_vars_do_not_override_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BLUMKIN_FILES_SCOPES", "1")
    monkeypatch.setenv("BLUMKIN_WO1162425_SCOPES", "0")
    (tmp_path / "config.toml").write_text(
        'client_id = "abc"\nfiles_scopes = false\nwo1162425_scopes = true\n'
    )
    cfg = load_config()
    assert cfg.files_scopes is False
    assert cfg.wo1162425_scopes is True


def _cfg(*, wo1162425_scopes: bool, files_scopes: bool = False) -> BlumkinConfig:
    return BlumkinConfig(
        client_id="abc",
        config_dir=Path("unused"),
        default_tz="UTC",
        email="",
        files_scopes=files_scopes,
        google_oauth_client_file=None,
        graph_timeout_seconds=60.0,
        legacy_flat=True,
        mail_signature=MailSignatureConfig(),
        profile="default",
        provider=ProviderKind.MICROSOFT,
        tags=(),
        tenant_id="brk.tech",
        wo1162425_scopes=wo1162425_scopes,
    )
