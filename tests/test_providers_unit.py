"""Unit tests for workspace provider factory and Microsoft adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from blumkin.config import BlumkinConfig, MailSignatureConfig, load_config
from blumkin.providers import get_provider
from blumkin.providers.kind import ProviderConfigError, ProviderKind, parse_provider_kind
from blumkin.providers.microsoft import MicrosoftWorkspaceProvider


def test_get_provider_returns_microsoft() -> None:
    cfg = _cfg()
    provider = get_provider(cfg)
    assert isinstance(provider, MicrosoftWorkspaceProvider)
    assert provider.kind is ProviderKind.MICROSOFT


def test_load_config_provider_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "abc"\n')
    assert load_config().provider is ProviderKind.MICROSOFT


def test_load_config_provider_google_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "abc"\nprovider = "google"\n')
    with pytest.raises(ProviderConfigError, match="not implemented"):
        load_config()


def test_load_config_provider_ignores_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "abc"\nprovider = "microsoft"\n')
    monkeypatch.setenv("BLUMKIN_PROVIDER", "google")
    assert load_config().provider is ProviderKind.MICROSOFT


def test_load_config_provider_non_string_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "abc"\nprovider = true\n')
    with pytest.raises(ProviderConfigError, match="must be a string"):
        load_config()


def test_load_config_provider_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "abc"\nprovider = "microsoft"\n')
    assert load_config().provider is ProviderKind.MICROSOFT


def test_microsoft_provider_auth_login_delegates() -> None:
    cfg = _cfg()
    provider = MicrosoftWorkspaceProvider(cfg)
    with (
        patch("blumkin.providers.microsoft.create_credential") as cred,
        patch("blumkin.providers.microsoft.save_token_cache") as save,
    ):
        provider.auth_login()
    cred.assert_called_once_with(cfg, allow_interactive=True)
    save.assert_called_once_with(cfg)


def test_microsoft_provider_delegates_calendar_today() -> None:
    cfg = _cfg()
    provider = MicrosoftWorkspaceProvider(cfg)
    with patch(
        "blumkin.providers.microsoft.calendar_today",
        new=AsyncMock(return_value={"date": "2026-08-29", "items": []}),
    ) as mocked:
        payload = asyncio.run(provider.calendar_today(tz_name="UTC"))
    assert payload["items"] == []
    mocked.assert_awaited_once()
    assert mocked.await_args is not None
    assert mocked.await_args.kwargs["config"] is cfg
    assert mocked.await_args.kwargs["tz_name"] == "UTC"


def test_parse_provider_kind_accepts_microsoft() -> None:
    assert parse_provider_kind("") is ProviderKind.MICROSOFT
    assert parse_provider_kind("Microsoft") is ProviderKind.MICROSOFT
    assert parse_provider_kind("microsoft") is ProviderKind.MICROSOFT


def test_parse_provider_kind_rejects_aliases_google_and_unknown() -> None:
    with pytest.raises(ProviderConfigError, match="unknown provider"):
        parse_provider_kind("m365")
    with pytest.raises(ProviderConfigError, match="not implemented"):
        parse_provider_kind("google")
    with pytest.raises(ProviderConfigError, match="unknown provider"):
        parse_provider_kind("yahoo")


def _cfg() -> BlumkinConfig:
    return BlumkinConfig(
        client_id="abc",
        config_dir=Path("unused"),
        default_tz="UTC",
        files_scopes=False,
        graph_timeout_seconds=60.0,
        mail_signature=MailSignatureConfig(),
        provider=ProviderKind.MICROSOFT,
        tenant_id="contoso.com",
        wo1162425_scopes=False,
    )
