"""Unit tests for effective MSAL scopes."""

from __future__ import annotations

from pathlib import Path

import pytest

from blumkin.auth import (
    BASE_SCOPES,
    DOCS_SCOPES,
    FILES_SCOPES,
    WO1162425_SCOPES,
    effective_scopes,
)
from blumkin.config import BlumkinConfig, MailSignatureConfig, PreferencesConfig, load_config
from blumkin.providers.kind import ProviderConfigError, ProviderKind


def test_docs_scopes_from_toml_and_off_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.default]\nclient_id = "abc"\n')
    assert load_config().docs_scopes is False
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "abc"\ndocs_scopes = true\n'
    )
    assert load_config().docs_scopes is True


def test_effective_scopes_default_excludes_phase4() -> None:
    assert effective_scopes(_cfg(wo1162425_scopes=False)) == BASE_SCOPES


def test_effective_scopes_docs_opt_in() -> None:
    assert effective_scopes(_cfg(wo1162425_scopes=False, docs_scopes=True)) == [
        *BASE_SCOPES,
        *DOCS_SCOPES,
    ]
    assert DOCS_SCOPES == ["Files.ReadWrite"]


def test_effective_scopes_enabled_includes_phase4() -> None:
    assert effective_scopes(_cfg(wo1162425_scopes=True)) == [*BASE_SCOPES, *WO1162425_SCOPES]
    assert "People.Read" in WO1162425_SCOPES
    assert "People.Read" not in BASE_SCOPES


def test_effective_scopes_files_opt_in() -> None:
    assert effective_scopes(_cfg(wo1162425_scopes=False, files_scopes=True)) == [
        *BASE_SCOPES,
        *FILES_SCOPES,
    ]


def test_files_scopes_from_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "abc"\nfiles_scopes = true\n'
    )
    assert load_config().files_scopes is True


def test_files_scopes_off_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.default]\nclient_id = "abc"\n')
    assert load_config().files_scopes is False


def test_wo1162425_scopes_from_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "abc"\nwo1162425_scopes = true\n'
    )
    assert load_config().wo1162425_scopes is True


def test_wo1162425_scopes_from_toml_int(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "abc"\nwo1162425_scopes = 1\n'
    )
    assert load_config().wo1162425_scopes is True


def test_scope_env_vars_do_not_override_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BLUMKIN_FILES_SCOPES", "1")
    monkeypatch.setenv("BLUMKIN_WO1162425_SCOPES", "0")
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "abc"\nfiles_scopes = false\nwo1162425_scopes = true\n'
    )
    cfg = load_config()
    assert cfg.files_scopes is False
    assert cfg.wo1162425_scopes is True


def test_effective_scopes_personal_account_excludes_chat_read() -> None:
    scopes = effective_scopes(_cfg(wo1162425_scopes=False, account_type="personal"))
    assert "Chat.Read" not in scopes
    assert scopes == [s for s in BASE_SCOPES if s != "Chat.Read"]


def test_effective_scopes_personal_account_excludes_teams_addons() -> None:
    scopes = effective_scopes(_cfg(wo1162425_scopes=True, account_type="personal"))
    assert "Chat.ReadWrite" not in scopes
    assert "OnlineMeetings.ReadWrite" not in scopes
    assert "People.Read" not in scopes
    # MailboxSettings.ReadWrite is a plain mailbox setting, not Teams-only -
    # still requested for a personal account with the add-on toggle on.
    assert "MailboxSettings.ReadWrite" in scopes


def test_effective_scopes_organizational_default_keeps_chat_read() -> None:
    assert "Chat.Read" in effective_scopes(_cfg(wo1162425_scopes=False))


def test_personal_account_unsupported_skills_match_the_unsupported_scopes() -> None:
    """PERSONAL_ACCOUNT_UNSUPPORTED_SKILLS (skills/__init__.py) and
    PERSONAL_ACCOUNT_UNSUPPORTED_SCOPES (auth.py) are two hand-maintained lists
    that must stay in lockstep: a skill is in the former iff its declared
    scopes intersect the latter. Mirrors
    test_drive_catalog_scopes_match_the_docs_scopes_gate for the docs_scopes
    gate."""
    from blumkin.auth import PERSONAL_ACCOUNT_UNSUPPORTED_SCOPES
    from blumkin.skills import PERSONAL_ACCOUNT_UNSUPPORTED_SKILLS, skills_catalog

    for spec in skills_catalog()["skills"]:
        needs_gate = bool(PERSONAL_ACCOUNT_UNSUPPORTED_SCOPES & set(spec["scopes"]))
        assert needs_gate == (spec["id"] in PERSONAL_ACCOUNT_UNSUPPORTED_SKILLS), spec["id"]


def test_account_type_from_toml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "abc"\naccount_type = "personal"\n'
    )
    assert load_config().account_type == "personal"


def test_account_type_defaults_to_organizational(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.default]\nclient_id = "abc"\n')
    assert load_config().account_type == "organizational"


def test_account_type_rejects_unknown_value(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "abc"\naccount_type = "consumer"\n'
    )
    with pytest.raises(ProviderConfigError, match="account_type"):
        load_config()


def _cfg(
    *,
    wo1162425_scopes: bool,
    files_scopes: bool = False,
    docs_scopes: bool = False,
    account_type: str = "organizational",
) -> BlumkinConfig:
    return BlumkinConfig(
        account_type=account_type,
        client_id="abc",
        config_dir=Path("unused"),
        default_tz="UTC",
        docs_scopes=docs_scopes,
        email="",
        files_scopes=files_scopes,
        google_oauth_client_file=None,
        graph_timeout_seconds=60.0,
        mail_signature=MailSignatureConfig(),
        preferences=PreferencesConfig(),
        profile="default",
        provider=ProviderKind.MICROSOFT,
        tags=(),
        tenant_id="brk.tech",
        wo1162425_scopes=wo1162425_scopes,
    )
