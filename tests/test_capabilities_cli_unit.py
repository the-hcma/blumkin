"""`blumkin capabilities` - standalone capability discovery (issue #315)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig, PreferencesConfig, ProviderKind

_CONFIG = '[profiles.default]\nclient_id = "abc"\ntenant_id = "example.com"\ndefault_tz = "UTC"\n'


def _cfg(*, provider: ProviderKind = ProviderKind.MICROSOFT) -> BlumkinConfig:
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
        provider=provider,
        tags=(),
        tenant_id="example.com",
        wo1162425_scopes=False,
        token_storage="auto",
    )


def _provider(*, granted_scopes: list[str] | None = None) -> MagicMock:
    provider = MagicMock()
    provider.auth_status.return_value = {
        "client_id_configured": True,
        "token_cache": True,
        "auth_record": True,
        "requested_scopes": [],
        "granted_scopes": granted_scopes or [],
    }
    provider.account_email.return_value = ""
    return provider


def test_capabilities_json_reports_profile_provider_version_and_families() -> None:
    granted = ["Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite"]
    with (
        patch("blumkin.cli._load_config", return_value=_cfg()),
        patch("blumkin.cli._workspace", return_value=_provider(granted_scopes=granted)),
    ):
        result = CliRunner().invoke(main, ["capabilities", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["profile"] == "default"
    assert payload["provider"] == "microsoft"
    assert isinstance(payload["version"], str) and payload["version"]
    assert payload["capabilities"]["mail"] is True
    assert payload["capabilities"]["calendar"] is True
    assert payload["capabilities"]["docs"] is False
    assert payload["capabilities"]["tasks"] is True


def test_capabilities_human_output_lists_available_families() -> None:
    with (
        patch("blumkin.cli._load_config", return_value=_cfg()),
        patch("blumkin.cli._workspace", return_value=_provider(granted_scopes=["Mail.Send"])),
    ):
        result = CliRunner().invoke(main, ["capabilities"])
    assert result.exit_code == 0
    assert "profile: default" in result.stdout
    assert "provider: microsoft" in result.stdout
    assert "available: tasks" in result.stdout


def test_capabilities_reflects_google_provider_and_scopes() -> None:
    granted = [
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.readonly",
    ]
    with (
        patch("blumkin.cli._load_config", return_value=_cfg(provider=ProviderKind.GOOGLE)),
        patch("blumkin.cli._workspace", return_value=_provider(granted_scopes=granted)),
    ):
        result = CliRunner().invoke(main, ["capabilities", "--json"])
    payload = json.loads(result.stdout)
    assert payload["provider"] == "google"
    assert payload["capabilities"]["calendar"] is True
    assert payload["capabilities"]["meeting"] is False
