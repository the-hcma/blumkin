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


def test_doctor_json_reports_account_type(tmp_path: Path, monkeypatch) -> None:
    """`doctor --json` builds its own `account_type` key from `cfg` directly
    (cli.py), independently of `status_dict()` - must not drift/rename
    silently (issue #297 review)."""
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "abc"\ntenant_id = "consumers"\n'
        'default_tz = "UTC"\naccount_type = "personal"\n'
    )
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "macos_keychain_missing", lambda cfg: False)
    with patch("blumkin.cli._workspace", return_value=_provider()):
        result = CliRunner().invoke(main, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    assert payload["account_type"] == "personal"

    with patch("blumkin.cli._workspace", return_value=_provider()):
        result = CliRunner().invoke(main, ["doctor"])
    assert "account_type: personal" in result.stdout


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


def test_doctor_treats_a_vaulted_ms_client_id_as_configured(tmp_path: Path, monkeypatch) -> None:
    """`client_id_configured: False` (the field `status_dict` derives straight
    from `config.toml`, never touching the keychain - see the one-touch
    guarantee in `test_status_dict_touches_one_keychain_item_for_both_bundled_kinds`)
    must not fail `doctor` when a `ms_client_id` is vaulted instead (issue #368
    review): a fully working, vaulted-only profile must not be reported as
    broken."""
    from blumkin import secret_store
    from blumkin.app_secrets import write_app_secret

    (tmp_path / "config.toml").write_text(
        '[profiles.default]\ntenant_id = "example.com"\ndefault_tz = "UTC"\n'
    )
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "macos_keychain_missing", lambda cfg: False)

    class _FakeKeyring:
        class errors:
            class PasswordDeleteError(Exception):
                pass

        def __init__(self) -> None:
            self.store: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, username: str) -> str | None:
            return self.store.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            self.store[(service, username)] = password

    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    from blumkin.config import load_config

    write_app_secret(load_config(), "ms_client_id", "vaulted-client-id")

    provider = _provider()
    provider.auth_status.return_value = {
        "client_id_configured": False,
        "token_cache": True,
        "auth_record": True,
        "requested_scopes": [],
    }
    with patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(main, ["doctor", "--json"])
    payload = json.loads(result.output)
    assert "client_id missing in config.toml" not in payload.get("problems", [])


def test_doctor_still_flags_a_genuinely_missing_ms_client_id(tmp_path: Path, monkeypatch) -> None:
    """The `client_id missing` problem must still fire when no vaulted
    `ms_client_id` exists either - `doctor`'s vault check must not mask a
    real misconfiguration."""
    from blumkin import secret_store

    (tmp_path / "config.toml").write_text(
        '[profiles.default]\ntenant_id = "example.com"\ndefault_tz = "UTC"\n'
    )
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "macos_keychain_missing", lambda cfg: False)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)

    provider = _provider()
    provider.auth_status.return_value = {
        "client_id_configured": False,
        "token_cache": True,
        "auth_record": True,
        "requested_scopes": [],
    }
    with patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(main, ["doctor", "--json"])
    payload = json.loads(result.output)
    assert "client_id missing in config.toml" in payload.get("problems", [])
