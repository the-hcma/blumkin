"""Unit tests for workspace provider factory and Microsoft adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from blumkin.config import BlumkinConfig, MailSignatureConfig, load_config
from blumkin.providers import get_provider
from blumkin.providers.kind import ProviderKind, parse_provider_kind
from blumkin.providers.microsoft import MicrosoftWorkspaceProvider


def test_parse_provider_kind_defaults_and_aliases() -> None:
    assert parse_provider_kind("") is ProviderKind.MICROSOFT
    assert parse_provider_kind("Microsoft") is ProviderKind.MICROSOFT
    assert parse_provider_kind("m365") is ProviderKind.MICROSOFT


def test_parse_provider_kind_rejects_google_and_unknown() -> None:
    with pytest.raises(ValueError, match="not implemented"):
        parse_provider_kind("google")
    with pytest.raises(ValueError, match="unknown provider"):
        parse_provider_kind("yahoo")


def test_load_config_provider_default_and_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_PROVIDER", raising=False)
    (tmp_path / "config.toml").write_text('client_id = "abc"\n')
    assert load_config().provider is ProviderKind.MICROSOFT
    monkeypatch.setenv("BLUMKIN_PROVIDER", "microsoft")
    assert load_config().provider is ProviderKind.MICROSOFT


def test_load_config_provider_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_PROVIDER", raising=False)
    (tmp_path / "config.toml").write_text('client_id = "abc"\nprovider = "microsoft"\n')
    assert load_config().provider is ProviderKind.MICROSOFT


def test_get_provider_returns_microsoft() -> None:
    cfg = _cfg()
    provider = get_provider(cfg)
    assert isinstance(provider, MicrosoftWorkspaceProvider)
    assert provider.kind is ProviderKind.MICROSOFT


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


def _cfg() -> BlumkinConfig:
    return BlumkinConfig(
        client_id="abc",
        config_dir=Path("unused"),
        default_tz="UTC",
        files_scopes=False,
        graph_timeout_seconds=60.0,
        mail_signature=MailSignatureConfig(),
        provider=ProviderKind.MICROSOFT,
        tenant_id="brk.tech",
        wo1162425_scopes=False,
    )
