"""`[browser] default` in config.toml (issue #468)."""

from __future__ import annotations

from pathlib import Path

import pytest

from blumkin.config import load_browser_setup, load_config
from blumkin.providers.kind import ProviderConfigError

_PROFILE = '[profiles.default]\nclient_id = "abc-123"\ntenant_id = "contoso.com"\n'


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(extra + _PROFILE)


def test_browser_default_is_unset_when_the_table_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path, monkeypatch, "")
    assert load_config().browser.default is None


@pytest.mark.parametrize("name", ["chrome", "edge", "chromium"])
def test_browser_default_accepts_the_three_supported_browsers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _config(tmp_path, monkeypatch, f'[browser]\ndefault = "{name}"\n\n')
    assert load_config().browser.default == name


def test_browser_default_is_case_insensitive_and_stored_lowercase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path, monkeypatch, '[browser]\ndefault = " Edge "\n\n')
    assert load_config().browser.default == "edge"


def test_an_unknown_browser_is_a_config_error_naming_the_accepted_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path, monkeypatch, '[browser]\ndefault = "firefox"\n\n')
    with pytest.raises(ProviderConfigError) as excinfo:
        load_config()
    message = str(excinfo.value)
    assert "browser.default" in message
    assert "chrome, chromium, edge" in message
    assert "'firefox'" in message


def test_a_non_string_browser_default_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path, monkeypatch, "[browser]\ndefault = 3\n\n")
    with pytest.raises(ProviderConfigError) as excinfo:
        load_config()
    assert "browser.default" in str(excinfo.value)


def test_browser_must_be_a_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config(tmp_path, monkeypatch, 'browser = "edge"\n')
    with pytest.raises(ProviderConfigError) as excinfo:
        load_config()
    assert "[browser]" in str(excinfo.value)


def test_a_blank_browser_default_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path, monkeypatch, '[browser]\ndefault = ""\n\n')
    with pytest.raises(ProviderConfigError) as excinfo:
        load_config()
    assert "browser.default" in str(excinfo.value)


def test_the_browser_setup_is_read_without_any_account_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[browser]\ndefault = "edge"\n')
    setup = load_browser_setup()
    assert setup.browser.default == "edge"
    assert setup.config_dir == tmp_path


def test_the_browser_setup_is_empty_without_a_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    setup = load_browser_setup()
    assert setup.browser.default is None


def test_a_broken_toml_file_is_a_config_error_for_the_browser_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text("[browser\ndefault = ")
    with pytest.raises(ProviderConfigError) as excinfo:
        load_browser_setup()
    assert "config.toml" in str(excinfo.value)
