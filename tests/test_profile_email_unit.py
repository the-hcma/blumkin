"""Hermetic tests for the per-profile account email (config.toml + profiles list)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
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


def test_auth_refresh_backfills_a_missing_email(tmp_path: Path, monkeypatch) -> None:
    """No-browser backfill path for installs that predate the field."""
    (tmp_path / "config.toml").write_text(_flat_config(""))
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    with patch("blumkin.cli._workspace", return_value=_authed_provider("ada@example.com")):
        result = CliRunner().invoke(main, ["auth", "refresh", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["email_written"] == "ada@example.com"
    assert 'email = "ada@example.com"' in (tmp_path / "config.toml").read_text()


def test_auth_refresh_backfills_a_sectioned_profile(tmp_path: Path, monkeypatch) -> None:
    """The real-world layout: profiles in [profiles.*] sections, selected with --profile."""
    (tmp_path / "config.toml").write_text(_TWO_PROFILES)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    with patch("blumkin.cli._workspace", return_value=_authed_provider("ada@example.com")):
        result = CliRunner().invoke(main, ["--profile", "work", "auth", "refresh", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["email_written"] == "ada@example.com"
    text = (tmp_path / "config.toml").read_text()
    # Landed inside [profiles.work], not the personal table or the signature block.
    work = text.split("[profiles.work]")[1].split("[profiles.work.mail.signature]")[0]
    assert 'email = "ada@example.com"' in work
    assert "email" not in text.split("[profiles.work]")[0]


def test_auth_refresh_leaves_an_existing_email_alone(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_flat_config("kept@example.com"))
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    with patch("blumkin.cli._workspace", return_value=_authed_provider("other@example.com")):
        result = CliRunner().invoke(main, ["auth", "refresh", "--json"])
    assert json.loads(result.stdout)["email_written"] is None
    assert 'email = "kept@example.com"' in (tmp_path / "config.toml").read_text()


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


def test_profiles_set_email_accepts_an_explicit_address(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_flat_config("stale@example.com"))
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    provider = _authed_provider("resolved@example.com")
    with patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(
            main, ["profiles", "set-email", "--email", "chosen@example.com", "--json"]
        )
    assert json.loads(result.stdout)["email"] == "chosen@example.com"
    # An explicit address short-circuits the live lookup entirely.
    provider.account_email.assert_not_called()
    assert 'email = "chosen@example.com"' in (tmp_path / "config.toml").read_text()


def test_profiles_set_email_backfills_a_sectioned_profile(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_TWO_PROFILES)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    with patch("blumkin.cli._workspace", return_value=_authed_provider("solo@example.com")):
        result = CliRunner().invoke(
            main, ["--profile", "personal", "profiles", "set-email", "--json"]
        )
    assert result.exit_code == 0
    text = (tmp_path / "config.toml").read_text()
    personal = text.split("[profiles.personal]")[1].split("[profiles.work]")[0]
    assert 'email = "solo@example.com"' in personal


def test_profiles_set_email_backfills_an_already_authenticated_profile(
    tmp_path: Path, monkeypatch
) -> None:
    """The gap onboarding-only population left: profiles signed in before the field existed."""
    (tmp_path / "config.toml").write_text(_flat_config(""))
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    with patch("blumkin.cli._workspace", return_value=_authed_provider("ada@example.com")):
        result = CliRunner().invoke(main, ["profiles", "set-email", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["email"] == "ada@example.com"
    assert 'email = "ada@example.com"' in (tmp_path / "config.toml").read_text()


def test_profiles_set_email_reports_a_bad_address_as_usage(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_flat_config(""))
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    with patch("blumkin.cli._workspace", return_value=_authed_provider("ada@example.com")):
        result = CliRunner().invoke(
            main, ["profiles", "set-email", "--email", "a\nb@example.com", "--json"]
        )
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"] == "usage_error"


def test_profiles_set_email_reports_when_it_cannot_resolve(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_flat_config(""))
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    with patch("blumkin.cli._workspace", return_value=_authed_provider("")):
        result = CliRunner().invoke(main, ["profiles", "set-email", "--json"])
    assert result.exit_code == 5
    assert json.loads(result.stderr)["error"] == "not_found"


def test_set_profile_email_does_not_glue_onto_a_file_without_a_trailing_newline(
    tmp_path: Path,
) -> None:
    """No trailing newline at EOF must not fuse two keys into one invalid line."""
    path = tmp_path / "config.toml"
    path.write_text('client_id = "abc"\ntenant_id = "example.com"')  # no trailing \n
    assert set_profile_email(path, profile="default", email="ada@example.com", legacy_flat=True)
    text = path.read_text()
    assert 'tenant_id = "example.com"\nemail = "ada@example.com"' in text
    # The whole point: it still parses.
    assert tomllib.loads(text)["email"] == "ada@example.com"


def test_set_profile_email_fills_a_whitespace_only_value(tmp_path: Path) -> None:
    """load_config strips, so `email = "  "` is unset to every other caller too."""
    path = tmp_path / "config.toml"
    path.write_text('client_id = "abc"\nemail = "  "\n')
    assert set_profile_email(path, profile="default", email="ada@example.com", legacy_flat=True)
    assert tomllib.loads(path.read_text())["email"] == "ada@example.com"


def test_set_profile_email_fills_an_existing_empty_value(tmp_path: Path) -> None:
    """`email = ""` is a blank to fill, not a label to protect."""
    path = tmp_path / "config.toml"
    path.write_text(
        _TWO_PROFILES.replace('tenant_id = "example.com"', 'tenant_id = "example.com"\nemail = ""')
    )
    assert set_profile_email(path, profile="work", email="ada@example.com", legacy_flat=False)
    text = path.read_text()
    assert 'email = "ada@example.com"' in text
    assert text.count("email = ") == 1


def test_set_profile_email_finds_a_header_with_a_comment_or_quotes(tmp_path: Path) -> None:
    """tomllib accepts these headers, so a plain string compare must not miss them."""
    for header in ("[profiles.work]  # main", '[profiles."work"]'):
        path = tmp_path / "config.toml"
        path.write_text(f'{header}\nclient_id = "abc"\n')
        assert set_profile_email(path, profile="work", email="ada@example.com", legacy_flat=False)
        assert 'email = "ada@example.com"' in path.read_text()
        assert tomllib.loads(path.read_text())["profiles"]["work"]["email"] == "ada@example.com"


def test_set_profile_email_handles_a_quoted_dotted_profile_name(tmp_path: Path) -> None:
    """[profiles."a.b"] is one profile named a.b, not a nested table."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[profiles."a.b"]\nclient_id = "abc"\n\n[profiles."a.b".mail.signature]\nenabled = true\n'
    )
    assert set_profile_email(path, profile="a.b", email="ada@example.com", legacy_flat=False)
    parsed = tomllib.loads(path.read_text())
    assert parsed["profiles"]["a.b"]["email"] == "ada@example.com"
    # The sub-table must not have been mistaken for the profile table.
    assert "email" not in parsed["profiles"]["a.b"]["mail"]["signature"]


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


def test_set_profile_email_overwrites_only_when_asked(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        _TWO_PROFILES.replace(
            'tenant_id = "example.com"', 'tenant_id = "example.com"\nemail = "old@example.com"'
        )
    )
    # Automatic paths leave an existing label alone...
    assert not set_profile_email(path, profile="work", email="new@example.com", legacy_flat=False)
    assert 'email = "old@example.com"' in path.read_text()
    # ...but the explicit command replaces it in place, without duplicating the key.
    assert set_profile_email(
        path, profile="work", email="new@example.com", legacy_flat=False, overwrite=True
    )
    text = path.read_text()
    assert 'email = "new@example.com"' in text
    assert "old@example.com" not in text
    assert text.count("email = ") == 1


def test_set_profile_email_rejects_control_characters(tmp_path: Path) -> None:
    """A newline in the value would break the file, or inject a table header."""
    path = tmp_path / "config.toml"
    path.write_text('client_id = "abc"\n')
    with pytest.raises(ValueError, match="control characters or newlines"):
        set_profile_email(
            path, profile="default", email='a\n[profiles.evil]\nx = "1', legacy_flat=True
        )
    assert "evil" not in path.read_text()


def test_toml_value_of_reads_an_empty_value_with_a_trailing_comment(tmp_path: Path) -> None:
    """`email = ""  # not yet known` is blank, so the automatic backfill must fill it."""
    path = tmp_path / "config.toml"
    path.write_text('client_id = "abc"\nemail = ""  # not yet known\n')
    assert set_profile_email(path, profile="default", email="ada@example.com", legacy_flat=True)
    text = path.read_text()
    assert 'email = "ada@example.com"' in text
    assert tomllib.loads(text)["email"] == "ada@example.com"


def _authed_provider(address: str) -> MagicMock:
    provider = MagicMock()
    provider.account_email.return_value = address
    provider.auth_refresh.return_value = {"access_token_expires_at": "2099-01-01T00:00:00Z"}
    return provider


def _flat_config(email: str) -> str:
    return f'client_id = "abc"\ntenant_id = "example.com"\ndefault_tz = "UTC"\nemail = "{email}"\n'
