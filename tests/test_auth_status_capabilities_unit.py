"""`auth status --json` enrichment: account, capabilities (issue #316)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from blumkin.cli import main
from blumkin.exit_codes import EXIT_SUCCESS

_CONFIG = '[profiles.default]\nclient_id = "abc"\ntenant_id = "example.com"\ndefault_tz = "UTC"\n'
_CONFIG_WITH_EMAIL = (
    '[profiles.default]\nclient_id = "abc"\ntenant_id = "example.com"\n'
    'default_tz = "UTC"\nemail = "rivera@example.com"\n'
)


def _provider(*, granted_scopes: list[str] | None = None) -> MagicMock:
    provider = MagicMock()
    provider.auth_status.return_value = {
        "client_id_configured": True,
        "token_cache": True,
        "auth_record": True,
        "requested_scopes": [],
        "granted_scopes": granted_scopes or [],
        "provider": "microsoft",
        "config_dir": "/unused",
        "config_path": "/unused/config.toml",
        "tenant_id": "example.com",
        "refresh_token_present": False,
        "access_token_expires_at": None,
    }
    return provider


def test_auth_status_json_reports_account_and_capabilities(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_CONFIG_WITH_EMAIL)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    provider = _provider(granted_scopes=["Mail.ReadWrite", "Mail.Send"])
    with patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(main, ["auth", "status", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.stdout)
    assert payload["account"] == "rivera@example.com"
    assert payload["capabilities"]["mail"] is True
    assert payload["capabilities"]["tasks"] is True
    assert payload["capabilities"]["calendar"] is False
    provider.account_email.assert_not_called()


def test_auth_status_json_account_is_none_when_unresolved(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_CONFIG)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    provider = _provider()
    with patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(main, ["auth", "status", "--json"])
    payload = json.loads(result.stdout)
    assert payload["account"] is None
    provider.account_email.assert_not_called()


def test_auth_status_human_output_lists_account_and_available(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_CONFIG_WITH_EMAIL)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    provider = _provider(granted_scopes=["Mail.Send"])
    with patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(main, ["auth", "status"])
    assert "account: rivera@example.com" in result.stdout
    assert "available: tasks" in result.stdout
