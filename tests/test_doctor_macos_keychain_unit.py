"""`doctor`'s macOS-keychain-missing warning (issue #308)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from blumkin import cli
from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig, PreferencesConfig, ProviderKind
from blumkin.secret_store import macos_keychain_missing

_CONFIG = '[profiles.default]\nclient_id = "abc"\ntenant_id = "example.com"\ndefault_tz = "UTC"\n'


def _cfg(*, token_storage: str = "auto") -> BlumkinConfig:
    return BlumkinConfig(
        client_id="abc",
        config_dir=Path("unused"),
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=None,
        graph_timeout_seconds=60.0,
        mail_signature=MailSignatureConfig(),
        preferences=PreferencesConfig(),
        profile="default",
        provider=ProviderKind.MICROSOFT,
        tags=(),
        tenant_id="example.com",
        wo1162425_scopes=False,
        token_storage=token_storage,
    )


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.auth_status.return_value = {
        "client_id_configured": True,
        "token_cache": True,
        "auth_record": True,
        "requested_scopes": [],
    }
    provider.account_email.return_value = ""
    return provider


def test_doctor_is_quiet_when_macos_keychain_is_present(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_CONFIG)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "macos_keychain_missing", lambda cfg: False)
    with patch("blumkin.cli._workspace", return_value=_provider()):
        result = CliRunner().invoke(main, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    assert payload["warnings"] == []


def test_doctor_warns_when_macos_keychain_is_missing(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_CONFIG)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "macos_keychain_missing", lambda cfg: True)
    with patch("blumkin.cli._workspace", return_value=_provider()):
        result = CliRunner().invoke(main, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    # Non-fatal, like the rest of `token_storage = "auto"` (issue #287): ok stays
    # true and the exit code stays 0, it is only reported.
    assert payload["ok"] is True
    assert result.exit_code == 0
    assert payload["problems"] == []
    assert any("keychain" in warning.lower() for warning in payload["warnings"])


def test_doctor_json_reports_a_capability_summary(tmp_path: Path, monkeypatch) -> None:
    """`doctor --json`'s `capabilities` block reuses capability_summary (issue #313)."""
    (tmp_path / "config.toml").write_text(_CONFIG)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "macos_keychain_missing", lambda cfg: False)
    provider = _provider()
    provider.auth_status.return_value["granted_scopes"] = [
        "Calendars.ReadWrite",
        "Mail.ReadWrite",
        "Mail.Send",
    ]
    with patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(main, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    assert payload["capabilities"]["mail"] is True
    assert payload["capabilities"]["calendar"] is True
    assert payload["capabilities"]["tasks"] is True
    assert payload["capabilities"]["docs"] is False


def test_doctor_human_output_lists_available_families(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_CONFIG)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "macos_keychain_missing", lambda cfg: False)
    provider = _provider()
    provider.auth_status.return_value["granted_scopes"] = ["Mail.ReadWrite", "Mail.Send"]
    with patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(main, ["doctor"])
    assert "available: mail, tasks" in result.stdout


def test_macos_keychain_missing_false_off_darwin(monkeypatch) -> None:
    from blumkin import secret_store

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    assert macos_keychain_missing(_cfg()) is False


def test_macos_keychain_missing_false_when_backend_usable(monkeypatch) -> None:
    from blumkin import secret_store

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: object())
    assert macos_keychain_missing(_cfg()) is False


def test_macos_keychain_missing_false_when_opted_into_file(monkeypatch) -> None:
    from blumkin import secret_store

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    assert macos_keychain_missing(_cfg(token_storage="file")) is False


def test_macos_keychain_missing_true_when_darwin_and_no_backend(monkeypatch) -> None:
    from blumkin import secret_store

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    assert macos_keychain_missing(_cfg()) is True
