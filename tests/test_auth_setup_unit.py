"""Unit tests for blumkin.auth_setup (issue #368 guided setup)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blumkin import auth_setup, secret_store
from blumkin.app_secrets import read_app_secret
from blumkin.config import load_config
from blumkin.providers.kind import ProviderConfigError


class _FakeKeyring:
    """Minimal in-memory stand-in for the ``keyring`` module's module-level API."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, provider: str, extra: str = ""):
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(f'[profiles.default]\nprovider = "{provider}"\n{extra}')
    return load_config()


def test_apply_google_setup_vaults_secret_and_writes_toml_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch, provider="google")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    data = auth_setup.GoogleSetupInput(
        client_id="abc.apps.googleusercontent.com",
        client_secret="GOCSPX-test-secret",
        auth_uri="https://accounts.google.com/o/oauth2/auth",
        redirect_uris=("http://localhost",),
        token_uri="https://oauth2.googleapis.com/token",
    )
    auth_setup.apply_google_setup(cfg, data)
    assert read_app_secret(cfg, "google_client_secret") == "GOCSPX-test-secret"
    written = load_config()
    assert written.client_id == "abc.apps.googleusercontent.com"
    assert written.google_auth_uri == "https://accounts.google.com/o/oauth2/auth"
    assert written.google_token_uri == "https://oauth2.googleapis.com/token"
    assert written.google_redirect_uris == ("http://localhost",)


def test_apply_google_setup_rejects_bad_client_id_before_writing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch, provider="google")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    data = auth_setup.GoogleSetupInput(client_id="not-a-google-client-id", client_secret="s3cr3t!!")
    with pytest.raises(ProviderConfigError, match="does not look like a Google OAuth client id"):
        auth_setup.apply_google_setup(cfg, data)
    assert read_app_secret(cfg, "google_client_secret") is None


def test_apply_google_setup_does_not_vault_secret_when_toml_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `set_profile_fields` failure must leave the keychain untouched.

    Regression test for a partial-apply bug: the secret used to be vaulted
    *before* config.toml was written, so a toml failure left a keychain
    secret with no matching config.toml `client_id` (issue #368 review).
    """
    cfg = _load(tmp_path, monkeypatch, provider="google")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise ProviderConfigError("simulated toml write failure")

    monkeypatch.setattr(auth_setup, "set_profile_fields", _boom)
    data = auth_setup.GoogleSetupInput(
        client_id="abc.apps.googleusercontent.com", client_secret="GOCSPX-test-secret"
    )
    with pytest.raises(ProviderConfigError, match="simulated toml write failure"):
        auth_setup.apply_google_setup(cfg, data)
    assert read_app_secret(cfg, "google_client_secret") is None
    assert fake.store == {}


def test_apply_microsoft_setup_vaults_client_id_and_writes_toml_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch, provider="microsoft")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    data = auth_setup.MicrosoftSetupInput(
        account_type="organizational",
        client_id="12345678-1234-1234-1234-123456789012",
        tenant_id="contoso.onmicrosoft.com",
    )
    auth_setup.apply_microsoft_setup(cfg, data)
    assert read_app_secret(cfg, "ms_client_id") == "12345678-1234-1234-1234-123456789012"
    written = load_config()
    assert written.tenant_id == "contoso.onmicrosoft.com"
    assert written.account_type == "organizational"


def test_validate_microsoft_setup_rejects_non_guid_client_id() -> None:
    data = auth_setup.MicrosoftSetupInput(
        account_type="organizational", client_id="not-a-guid", tenant_id="contoso.onmicrosoft.com"
    )
    with pytest.raises(ProviderConfigError, match="does not look like an Entra"):
        auth_setup.validate_microsoft_setup(data)


def test_validate_microsoft_setup_rejects_personal_account_with_org_tenant() -> None:
    data = auth_setup.MicrosoftSetupInput(
        account_type="personal",
        client_id="12345678-1234-1234-1234-123456789012",
        tenant_id="contoso.onmicrosoft.com",
    )
    with pytest.raises(ProviderConfigError, match="needs tenant_id = 'consumers'"):
        auth_setup.validate_microsoft_setup(data)


def test_validate_microsoft_setup_rejects_org_account_with_reserved_tenant() -> None:
    data = auth_setup.MicrosoftSetupInput(
        account_type="organizational",
        client_id="12345678-1234-1234-1234-123456789012",
        tenant_id="consumers",
    )
    with pytest.raises(ProviderConfigError, match="personal-account reserved value"):
        auth_setup.validate_microsoft_setup(data)


def test_validate_microsoft_setup_accepts_personal_with_consumers() -> None:
    data = auth_setup.MicrosoftSetupInput(
        account_type="personal",
        client_id="12345678-1234-1234-1234-123456789012",
        tenant_id="consumers",
    )
    auth_setup.validate_microsoft_setup(data)  # does not raise


def test_validate_microsoft_setup_rejects_personal_with_organizations_tenant() -> None:
    """'organizations' only admits work/school accounts - never valid for personal."""
    data = auth_setup.MicrosoftSetupInput(
        account_type="personal",
        client_id="12345678-1234-1234-1234-123456789012",
        tenant_id="organizations",
    )
    with pytest.raises(ProviderConfigError, match="needs tenant_id = 'consumers'"):
        auth_setup.validate_microsoft_setup(data)


def test_validate_microsoft_setup_accepts_organizational_with_organizations_tenant() -> None:
    data = auth_setup.MicrosoftSetupInput(
        account_type="organizational",
        client_id="12345678-1234-1234-1234-123456789012",
        tenant_id="organizations",
    )
    auth_setup.validate_microsoft_setup(data)  # does not raise


def test_console_steps_covers_both_providers() -> None:
    assert auth_setup.console_steps("google") == auth_setup.GOOGLE_CONSOLE_STEPS
    assert auth_setup.console_steps("microsoft") == auth_setup.MICROSOFT_CONSOLE_STEPS
    with pytest.raises(ProviderConfigError):
        auth_setup.console_steps("bogus")


def test_google_setup_from_client_json_extracts_every_field(tmp_path: Path) -> None:
    path = tmp_path / "desktop-client.json"
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "abc.apps.googleusercontent.com",
                    "client_secret": "GOCSPX-file-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        )
    )
    data = auth_setup.google_setup_from_client_json(path)
    assert data.client_id == "abc.apps.googleusercontent.com"
    assert data.client_secret == "GOCSPX-file-secret"
    assert data.auth_uri == "https://accounts.google.com/o/oauth2/auth"
    assert data.token_uri == "https://oauth2.googleapis.com/token"
    assert data.redirect_uris == ("http://localhost",)


def test_google_setup_from_client_json_rejects_missing_secret(tmp_path: Path) -> None:
    path = tmp_path / "desktop-client.json"
    path.write_text(json.dumps({"installed": {"client_id": "abc.apps.googleusercontent.com"}}))
    with pytest.raises(ProviderConfigError, match="no usable client_secret"):
        auth_setup.google_setup_from_client_json(path)
