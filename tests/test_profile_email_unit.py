"""Hermetic tests for the per-profile account email (config.toml + profiles list)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from blumkin.cli import main
from blumkin.config import list_profiles, load_config, set_profile_email

_TWO_PROFILES = """\
default_profile = "work"

# Personal Google account.
[profiles.personal]
provider = "google"
default_tz = "America/New_York"
tags = ["@personal"]

[profiles.work]
provider = "microsoft"
client_id = "abc"
tenant_id = "example.com"
default_tz = "America/New_York"

[profiles.work.mail.signature]
enabled = true
name = "Ada Lovelace"
"""


def _flat_config(email: str) -> str:
    return f'client_id = "abc"\ntenant_id = "example.com"\ndefault_tz = "UTC"\nemail = "{email}"\n'


def test_set_profile_email_inserts_into_the_right_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_TWO_PROFILES)
    assert set_profile_email(path, profile="work", email="ada@example.com", legacy_flat=False)
    text = path.read_text()
    # Landed in [profiles.work], above its signature sub-table, and left the rest alone.
    work = text.split("[profiles.work]")[1].split("[profiles.work.mail.signature]")[0]
    assert 'email = "ada@example.com"' in work
    assert 'email = "ada@example.com"' not in text.split("[profiles.work]")[0]
    assert "# Personal Google account." in text
    assert 'name = "Ada Lovelace"' in text


def test_set_profile_email_never_overwrites_an_existing_value(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        _TWO_PROFILES.replace(
            'tenant_id = "example.com"', 'tenant_id = "example.com"\nemail = "first@example.com"'
        )
    )
    assert not set_profile_email(
        path, profile="work", email="second@example.com", legacy_flat=False
    )
    assert 'email = "first@example.com"' in path.read_text()
    assert "second@example.com" not in path.read_text()


def test_set_profile_email_handles_legacy_flat_and_missing_section(tmp_path: Path) -> None:
    flat = tmp_path / "flat.toml"
    flat.write_text('client_id = "abc"\ndefault_tz = "UTC"\n')
    assert set_profile_email(flat, profile="default", email="solo@example.com", legacy_flat=True)
    assert 'email = "solo@example.com"' in flat.read_text()

    missing = tmp_path / "config.toml"
    missing.write_text(_TWO_PROFILES)
    assert not set_profile_email(missing, profile="nope", email="x@example.com", legacy_flat=False)
    assert not set_profile_email(
        tmp_path / "absent.toml", profile="work", email="x@e.com", legacy_flat=False
    )


def test_profiles_list_reports_email(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(
        _TWO_PROFILES.replace(
            'tenant_id = "example.com"', 'tenant_id = "example.com"\nemail = "ada@example.com"'
        )
    )
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    by_name = {item["name"]: item for item in list_profiles()}
    assert by_name["work"]["email"] == "ada@example.com"
    assert by_name["personal"]["email"] == ""

    result = CliRunner().invoke(main, ["profiles", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    emails = {p["name"]: p["email"] for p in payload["profiles"]}
    assert emails == {"personal": "", "work": "ada@example.com"}

    human = CliRunner().invoke(main, ["profiles", "list"])
    assert "email=ada@example.com" in human.stdout
    assert "email=(unset)" in human.stdout


def test_doctor_warns_when_config_email_and_signed_in_account_disagree(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "config.toml").write_text(_flat_config("old@example.com"))
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    provider = MagicMock()
    provider.auth_status.return_value = {
        "client_id_configured": True,
        "token_cache": True,
        "auth_record": True,
        "requested_scopes": [],
    }
    provider.account_email.return_value = "new@example.com"
    with patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(main, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    # Drift is a warning, not a problem: ok stays true and the exit stays 0.
    assert payload["ok"] is True
    assert result.exit_code == 0
    assert payload["problems"] == []
    assert len(payload["warnings"]) == 1
    assert "old@example.com" in payload["warnings"][0]
    assert "new@example.com" in payload["warnings"][0]


def test_doctor_is_quiet_when_the_account_matches(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_flat_config("Ada@Example.com"))
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    provider = MagicMock()
    provider.auth_status.return_value = {
        "client_id_configured": True,
        "token_cache": True,
        "auth_record": True,
        "requested_scopes": [],
    }
    # Same address, different casing — email is case-insensitive, so not drift.
    provider.account_email.return_value = "ada@example.com"
    with patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(main, ["doctor", "--json"])
    assert json.loads(result.stdout)["warnings"] == []


def test_microsoft_account_email_reads_the_auth_record(tmp_path: Path, monkeypatch) -> None:
    from blumkin.providers.microsoft import MicrosoftWorkspaceProvider

    (tmp_path / "config.toml").write_text('client_id = "abc"\ntenant_id = "example.com"\n')
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    config = load_config()
    record = tmp_path / "auth_record.json"

    record.write_text(json.dumps({"username": "ada@example.com"}))
    assert MicrosoftWorkspaceProvider(config).account_email() == "ada@example.com"

    # Unreadable or absent record degrades to "unknown", never raises: the callers
    # are an onboarding nicety and a doctor warning.
    record.write_text("not json")
    assert MicrosoftWorkspaceProvider(config).account_email() == ""
    record.unlink()
    assert MicrosoftWorkspaceProvider(config).account_email() == ""
