"""Unit tests for blumkin.auth_setup (issue #368 guided setup)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blumkin import auth_setup, secret_store
from blumkin.app_secrets import read_app_secret
from blumkin.config import load_config, set_profile_fields
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
    # The secret must never land in plaintext config.toml - only the keychain.
    assert "GOCSPX-test-secret" not in (tmp_path / "config.toml").read_text()


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
    text = (tmp_path / "config.toml").read_text()
    assert "not-a-google-client-id" not in text
    assert "s3cr3t!!" not in text


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
    assert written.client_id == "12345678-1234-1234-1234-123456789012"


def test_apply_microsoft_setup_is_best_effort_when_no_stale_value_shadows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keychain write failure with nothing already vaulted just means the
    toml copy is the only source - `read_app_secret` will fall through to it,
    so `False` (not raising) is correct here (issue #368 review)."""
    cfg = _load(tmp_path, monkeypatch, provider="microsoft")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    data = auth_setup.MicrosoftSetupInput(
        account_type="organizational",
        client_id="12345678-1234-1234-1234-123456789012",
        tenant_id="contoso.onmicrosoft.com",
    )
    assert auth_setup.apply_microsoft_setup(cfg, data) is False
    assert load_config().client_id == "12345678-1234-1234-1234-123456789012"


def test_apply_microsoft_setup_raises_when_a_different_vaulted_id_would_shadow_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write failure that leaves an *older, different* vaulted client_id in
    place must not be swallowed - `auth._resolved_client_id` prefers the
    vaulted value over config.toml, so silently returning `False` here would
    let `auth login` keep using the stale id after the operator explicitly
    supplied a new one (issue #368 review)."""
    cfg = _load(tmp_path, monkeypatch, provider="microsoft")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    old_data = auth_setup.MicrosoftSetupInput(
        account_type="organizational",
        client_id="11111111-1111-1111-1111-111111111111",
        tenant_id="contoso.onmicrosoft.com",
    )
    auth_setup.apply_microsoft_setup(cfg, old_data)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise secret_store.SecretWriteError("simulated keychain failure")

    monkeypatch.setattr(auth_setup, "write_app_secret", _boom)
    new_data = auth_setup.MicrosoftSetupInput(
        account_type="organizational",
        client_id="22222222-2222-2222-2222-222222222222",
        tenant_id="contoso.onmicrosoft.com",
    )
    with pytest.raises(secret_store.SecretWriteError):
        auth_setup.apply_microsoft_setup(load_config(), new_data)
    # config.toml still gets the new id (written before the vault attempt);
    # it is the stale keychain value shadowing it that must be surfaced.
    assert load_config().client_id == "22222222-2222-2222-2222-222222222222"
    assert read_app_secret(load_config(), "ms_client_id") == "11111111-1111-1111-1111-111111111111"


def test_apply_microsoft_setup_raises_when_the_stale_value_probe_read_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #384: when the write *and* the follow-up "is anything stale
    vaulted?" read both fail, the stale vaulted client_id is indistinguishable
    from "nothing vaulted" - swallowing the read failure the same way
    `read_app_secret` does elsewhere would report success (`False`, not
    raising) while an older `ms_client_id` still shadows the new one once the
    backend becomes readable again."""
    cfg = _load(tmp_path, monkeypatch, provider="microsoft")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    old_data = auth_setup.MicrosoftSetupInput(
        account_type="organizational",
        client_id="11111111-1111-1111-1111-111111111111",
        tenant_id="contoso.onmicrosoft.com",
    )
    auth_setup.apply_microsoft_setup(cfg, old_data)

    def _write_boom(*_args: object, **_kwargs: object) -> None:
        raise secret_store.SecretWriteError("simulated keychain write failure")

    def _read_boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated keychain read failure")

    monkeypatch.setattr(auth_setup, "write_app_secret", _write_boom)
    monkeypatch.setattr(auth_setup, "_read_app_secret_or_raise", _read_boom)
    new_data = auth_setup.MicrosoftSetupInput(
        account_type="organizational",
        client_id="22222222-2222-2222-2222-222222222222",
        tenant_id="contoso.onmicrosoft.com",
    )
    with pytest.raises(secret_store.SecretWriteError):
        auth_setup.apply_microsoft_setup(load_config(), new_data)
    # config.toml still gets the new id (written before the vault attempt);
    # it is the unconfirmable stale-shadow risk that must be surfaced.
    assert load_config().client_id == "22222222-2222-2222-2222-222222222222"


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
    with pytest.raises(ProviderConfigError, match="personal-account or multi-tenant"):
        auth_setup.validate_microsoft_setup(data)


def test_validate_microsoft_setup_accepts_personal_with_consumers() -> None:
    data = auth_setup.MicrosoftSetupInput(
        account_type="personal",
        client_id="12345678-1234-1234-1234-123456789012",
        tenant_id="consumers",
    )
    auth_setup.validate_microsoft_setup(data)  # does not raise


def test_validate_microsoft_setup_accepts_personal_with_common() -> None:
    """`common` is the other half of the personal-account acceptance set
    (auth_setup.py's `lowered not in {"common", "consumers"}` guard) - it is
    also the interactive default `_collect_microsoft_setup` keeps for a
    personal profile that already has `tenant_id = "common"` (issue #368
    review)."""
    data = auth_setup.MicrosoftSetupInput(
        account_type="personal",
        client_id="12345678-1234-1234-1234-123456789012",
        tenant_id="common",
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


@pytest.mark.parametrize("tenant_id", ["organizations", "common", "consumers"])
def test_validate_microsoft_setup_rejects_organizational_with_multi_tenant_value(
    tenant_id: str,
) -> None:
    """docs/SECURITY-AT-A-GLANCE.md's single-tenant hardening rule says an
    organizational profile must never use `common` / `organizations` /
    `consumers` - only the tenant's own GUID or verified domain bounds
    sign-in to that tenant."""
    data = auth_setup.MicrosoftSetupInput(
        account_type="organizational",
        client_id="12345678-1234-1234-1234-123456789012",
        tenant_id=tenant_id,
    )
    with pytest.raises(ProviderConfigError, match="personal-account or multi-tenant"):
        auth_setup.validate_microsoft_setup(data)


def test_validate_microsoft_setup_accepts_organizational_with_verified_domain() -> None:
    data = auth_setup.MicrosoftSetupInput(
        account_type="organizational",
        client_id="12345678-1234-1234-1234-123456789012",
        tenant_id="contoso.onmicrosoft.com",
    )
    auth_setup.validate_microsoft_setup(data)  # does not raise


def test_console_steps_covers_both_providers() -> None:
    assert auth_setup.console_steps("google") == auth_setup.GOOGLE_CONSOLE_STEPS
    assert auth_setup.console_steps("microsoft") == auth_setup.MICROSOFT_CONSOLE_STEPS
    with pytest.raises(ProviderConfigError):
        auth_setup.console_steps("bogus")


def test_set_profile_fields_raises_on_missing_profile(tmp_path: Path) -> None:
    """Unlike ``set_profile_email``'s best-effort ``False``, a missing profile
    must raise - a silent no-op here would let `auth setup` claim success
    while nothing was actually persisted (issue #368 review)."""
    path = tmp_path / "config.toml"
    path.write_text('[profiles.other]\nprovider = "google"\n')
    with pytest.raises(ProviderConfigError, match="not found"):
        set_profile_fields(path, profile="default", fields={"client_id": "abc"})


def test_set_profile_fields_always_overwrites_an_existing_value(tmp_path: Path) -> None:
    """Unlike ``set_profile_email``'s fill-in-a-blank semantics, `auth setup`
    replacing a stale/incorrect `client_id` must actually land - a regression
    to fill-in-a-blank would silently keep the old value paired with the
    newly vaulted secret (issue #368 review)."""
    path = tmp_path / "config.toml"
    path.write_text('[profiles.default]\nprovider = "google"\nclient_id = "old-client-id"\n')
    set_profile_fields(path, profile="default", fields={"client_id": "new-client-id"})
    text = path.read_text()
    assert "new-client-id" in text
    assert "old-client-id" not in text


def test_set_profile_fields_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ProviderConfigError, match="does not exist"):
        set_profile_fields(tmp_path / "absent.toml", profile="default", fields={"client_id": "x"})


def test_set_profile_fields_raises_on_unparseable_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = "[profiles.default\nclient_id = abc\n"
    path.write_text(original)
    with pytest.raises(ProviderConfigError, match="could not parse"):
        set_profile_fields(path, profile="default", fields={"client_id": "abc"})
    assert path.read_text() == original


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


def test_google_setup_from_client_json_rejects_missing_client_id(tmp_path: Path) -> None:
    path = tmp_path / "desktop-client.json"
    path.write_text(json.dumps({"installed": {"client_secret": "GOCSPX-file-secret"}}))
    with pytest.raises(ProviderConfigError, match="no usable client_id"):
        auth_setup.google_setup_from_client_json(path)


def test_validate_google_setup_rejects_empty_client_id() -> None:
    data = auth_setup.GoogleSetupInput(client_id="", client_secret="s3cr3t!!!")
    with pytest.raises(ProviderConfigError, match="client_id is required"):
        auth_setup.validate_google_setup(data)


def test_validate_google_setup_rejects_short_client_secret() -> None:
    data = auth_setup.GoogleSetupInput(
        client_id="abc.apps.googleusercontent.com", client_secret="short"
    )
    with pytest.raises(ProviderConfigError, match="implausibly short"):
        auth_setup.validate_google_setup(data)


def test_validate_google_setup_rejects_non_https_auth_uri() -> None:
    data = auth_setup.GoogleSetupInput(
        client_id="abc.apps.googleusercontent.com",
        client_secret="GOCSPX-test-secret",
        auth_uri="http://accounts.google.com/o/oauth2/auth",
    )
    with pytest.raises(ProviderConfigError, match="auth_uri .* must start with https://"):
        auth_setup.validate_google_setup(data)


def test_validate_google_setup_rejects_non_https_token_uri() -> None:
    data = auth_setup.GoogleSetupInput(
        client_id="abc.apps.googleusercontent.com",
        client_secret="GOCSPX-test-secret",
        token_uri="http://oauth2.googleapis.com/token",
    )
    with pytest.raises(ProviderConfigError, match="token_uri .* must start with https://"):
        auth_setup.validate_google_setup(data)


def test_validate_google_setup_rejects_empty_redirect_uri() -> None:
    data = auth_setup.GoogleSetupInput(
        client_id="abc.apps.googleusercontent.com",
        client_secret="GOCSPX-test-secret",
        redirect_uris=("  ",),
    )
    with pytest.raises(ProviderConfigError, match="redirect_uris must not contain an empty value"):
        auth_setup.validate_google_setup(data)


def test_validate_google_setup_accepts_a_well_formed_input() -> None:
    data = auth_setup.GoogleSetupInput(
        client_id="abc.apps.googleusercontent.com",
        client_secret="GOCSPX-test-secret",
        auth_uri="https://accounts.google.com/o/oauth2/auth",
        token_uri="https://oauth2.googleapis.com/token",
        redirect_uris=("http://localhost",),
    )
    auth_setup.validate_google_setup(data)  # does not raise
