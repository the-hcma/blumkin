"""`[browser] models_dir`: a configurable, validated location for the site models (#621)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from blumkin.browser.model import (
    Control,
    ControlKind,
    ControlSafety,
    Locator,
    LocatorStrategy,
    Page,
    SiteModel,
    SiteModelError,
    default_store,
    list_versions,
    load_model,
    resolve_store,
    save_model,
)
from blumkin.cli import main
from blumkin.config import BrowserConfig, BrowserSetup, BrowserTaskPolicy, ProviderConfigError


def _model(task: str = "expense-report", **overrides: Any) -> SiteModel:
    fields: dict[str, Any] = {
        "blumkin_version": "1.2.3",
        "flows": (),
        "hosts": ("erp.example.com",),
        "pages": (
            Page(
                page_id="list",
                url_template="/expenses/{id}",
                fingerprint=("button:Add line",),
                controls=(
                    Control(
                        name="expense.add",
                        kind=ControlKind.BUTTON,
                        safety=ControlSafety.SAFE,
                        locators=(
                            Locator(strategy=LocatorStrategy.ROLE_NAME, value="button:Add line"),
                        ),
                    ),
                ),
            ),
        ),
        "recorded_at": "2026-10-24T12:00:00Z",
        "task": task,
        "version": 0,
    }
    fields.update(overrides)
    return SiteModel(**fields)


def _setup(tmp_path: Path, models_dir: str | None, **policy: Any) -> BrowserSetup:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    tasks = {"expense-report": BrowserTaskPolicy(**policy)} if policy else {}
    return BrowserSetup(
        browser=BrowserConfig(models_dir=models_dir, tasks=tasks), config_dir=config
    )


def test_without_a_setting_the_store_is_under_the_config_directory(tmp_path: Path) -> None:
    setup = _setup(tmp_path, None)

    assert resolve_store(setup) == default_store(setup.config_dir)
    assert resolve_store(setup).name == "browser-models"


def test_a_relocated_store_holds_every_version_where_it_was_told(tmp_path: Path) -> None:
    repo = tmp_path / "repo-models"
    setup = _setup(tmp_path, str(repo))
    store = resolve_store(setup)

    save_model(store, _model())
    save_model(store, _model(recorded_at="2026-10-25T12:00:00Z"))

    assert store == repo.resolve()
    assert (repo / "expense-report" / "model.json").is_file()
    assert list_versions(store, "expense-report") == [1, 2]
    assert not (setup.config_dir / "browser-models").exists()
    assert oct(repo.stat().st_mode & 0o777) == "0o700"


def test_a_symlinked_location_is_followed_to_a_private_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)

    assert resolve_store(_setup(tmp_path, str(link))) == real.resolve()


@pytest.mark.parametrize("text", ["models", "../models", ""])
def test_a_location_must_be_absolute_or_start_with_a_tilde_slash(tmp_path: Path, text: str) -> None:
    with pytest.raises(SiteModelError, match="absolute"):
        resolve_store(_setup(tmp_path, text))


def test_the_filesystem_root_and_the_home_directory_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    for text in ("/", "~/"):
        with pytest.raises(SiteModelError, match="folder of its own"):
            resolve_store(_setup(tmp_path, text))


@pytest.mark.parametrize("kind", ["upload_dirs", "download_dirs"])
def test_a_location_inside_an_upload_or_download_folder_is_refused(
    tmp_path: Path, kind: str
) -> None:
    folder = tmp_path / "receipts"
    folder.mkdir()

    for where in (folder, folder / "models"):
        setup = _setup(tmp_path, str(where), **{kind: (str(folder),)})
        with pytest.raises(SiteModelError, match="apart from every"):
            resolve_store(setup)


def test_a_location_whose_parent_is_missing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SiteModelError, match="parent"):
        resolve_store(_setup(tmp_path, str(tmp_path / "nope" / "models")))


def test_a_location_that_is_a_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "models").write_text("x")

    with pytest.raises(SiteModelError, match="directory"):
        resolve_store(_setup(tmp_path, str(tmp_path / "models")))


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o707])
def test_a_location_others_can_reach_is_refused_not_tightened(tmp_path: Path, mode: int) -> None:
    where = tmp_path / "models"
    where.mkdir()
    where.chmod(mode)

    with pytest.raises(SiteModelError, match="private"):
        resolve_store(_setup(tmp_path, str(where)))

    assert (where.stat().st_mode & 0o777) == mode


def test_a_location_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    where = tmp_path / "models"
    where.mkdir(mode=0o700)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(where).st_uid + 1)

    with pytest.raises(SiteModelError, match="owned by you"):
        resolve_store(_setup(tmp_path, str(where)))


def test_the_store_protections_hold_inside_a_relocated_store(tmp_path: Path) -> None:
    store = resolve_store(_setup(tmp_path, str(tmp_path / "models")))
    save_model(store, _model())
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "model.json").write_text((store / "expense-report" / "model.json").read_text())

    shutil.rmtree(store / "expense-report")
    (store / "expense-report").symlink_to(outside)
    with pytest.raises(SiteModelError, match="symlink"):
        load_model(store, "expense-report")

    (store / "expense-report").unlink()
    save_model(store, _model())
    current = store / "expense-report" / "model.json"
    current.unlink()
    current.symlink_to(outside / "model.json")
    with pytest.raises(SiteModelError):
        load_model(store, "expense-report")


def test_a_copy_that_stopped_after_the_history_still_loads_the_newest_version(
    tmp_path: Path,
) -> None:
    old = default_store(tmp_path / "old")
    save_model(old, _model())
    save_model(old, _model(recorded_at="2026-10-25T12:00:00Z"))
    new = resolve_store(_setup(tmp_path, str(tmp_path / "models")))
    (new / "expense-report").mkdir(parents=True, mode=0o700)
    shutil.copytree(old / "expense-report" / "history", new / "expense-report" / "history")
    (new / "expense-report" / ".model.json.tmp-123").write_text("half a file")

    assert list_versions(new, "expense-report") == [1, 2]
    loaded = load_model(new, "expense-report")
    assert loaded is not None and loaded.version == 2
    assert save_model(new, _model()).version == 3


def test_the_config_accepts_models_dir_and_refuses_a_malformed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blumkin.config import load_browser_setup

    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[browser]\nmodels_dir = "~/versioned/models"\n')
    assert load_browser_setup().browser.models_dir == "~/versioned/models"

    (tmp_path / "config.toml").write_text('[browser]\nmodels_dir = "models"\n')
    with pytest.raises(ProviderConfigError, match="models_dir"):
        load_browser_setup()
    (tmp_path / "config.toml").write_text("[browser]\nmodels_dir = 3\n")
    with pytest.raises(ProviderConfigError, match="models_dir"):
        load_browser_setup()


def test_model_versions_shows_where_the_store_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = tmp_path / "models"
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.toml").write_text(f'[browser]\nmodels_dir = "{models}"\n')
    save_model(resolve_store(_setup(tmp_path, str(models))), _model())

    result = CliRunner().invoke(main, ["--json", "browser", "model", "versions", "expense-report"])

    payload = json.loads(result.output)
    assert payload["store"] == str(models.resolve()) and payload["versions"] == [1]


def _doctor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> dict[str, Any]:
    from blumkin import cli

    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "abc"\ntenant_id = "example.com"\ndefault_tz = "UTC"\n'
        + body
    )
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "macos_keychain_missing", lambda cfg: False)
    provider = MagicMock()
    provider.auth_status.return_value = {
        "auth_record": True,
        "client_id_configured": True,
        "granted_scopes": [],
        "token_cache": True,
    }
    with patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(main, ["doctor", "--json"])
    return json.loads(result.stdout)


def test_doctor_shows_the_default_and_a_configured_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = _doctor(tmp_path, monkeypatch, "")["browser_models"]
    assert default["source"] == "default" and default["dir"].endswith("browser-models")

    models = tmp_path / "versioned"
    models.mkdir(mode=0o700)
    custom = _doctor(tmp_path, monkeypatch, f'\n[browser]\nmodels_dir = "{models}"\n')

    assert custom["browser_models"] == {
        "dir": str(models.resolve()),
        "source": "models_dir",
        "usable": True,
    }


def test_doctor_warns_about_a_location_it_would_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = tmp_path / "open"
    models.mkdir()
    models.chmod(0o755)

    payload = _doctor(tmp_path, monkeypatch, f'\n[browser]\nmodels_dir = "{models}"\n')

    assert payload["browser_models"]["usable"] is False
    assert any("models_dir cannot be used" in w and "0700" in w for w in payload["warnings"])


def test_the_loop_handler_finds_a_configured_store_and_ignores_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blumkin.loop.expense_handler import _models_store

    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text("")
    assert _models_store() is None

    models = tmp_path / "versioned"
    (tmp_path / "config.toml").write_text(f'[browser]\nmodels_dir = "{models}"\n')
    assert _models_store() == models.resolve()


def test_a_location_spelled_in_another_case_is_still_inside_an_upload_folder(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "Receipts"
    folder.mkdir()
    setup = _setup(tmp_path, str(tmp_path / "receipts" / "models"), upload_dirs=(str(folder),))

    with pytest.raises(SiteModelError, match="apart from every"):
        resolve_store(setup)


def test_a_location_around_an_upload_folder_is_refused(tmp_path: Path) -> None:
    around = tmp_path / "documents"
    inside = around / "receipts"
    inside.mkdir(parents=True)
    setup = _setup(tmp_path, str(around), download_dirs=(str(inside),))

    with pytest.raises(SiteModelError, match="not around one"):
        resolve_store(setup)


def test_the_config_directory_and_its_parents_are_not_a_models_location(tmp_path: Path) -> None:
    setup = _setup(tmp_path, str(tmp_path / "config"))
    with pytest.raises(SiteModelError, match="apart from the config"):
        resolve_store(setup)

    with pytest.raises(SiteModelError, match="apart from the config"):
        resolve_store(_setup(tmp_path, str(tmp_path)))


def test_the_chmod_advice_is_safe_to_paste(tmp_path: Path) -> None:
    where = tmp_path / "my models; rm -rf x"
    where.mkdir()
    where.chmod(0o755)

    with pytest.raises(SiteModelError) as caught:
        resolve_store(_setup(tmp_path, str(where)))

    assert f"chmod 700 '{where}'" in str(caught.value)


def test_a_refused_location_stops_the_loop_instead_of_using_the_default_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blumkin.loop.expense_handler import _models_store

    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    open_dir.chmod(0o755)
    (tmp_path / "config.toml").write_text(f'[browser]\nmodels_dir = "{open_dir}"\n')

    with pytest.raises(SiteModelError):
        _models_store()


def test_the_config_directory_is_refused_in_any_letter_case(tmp_path: Path) -> None:
    (tmp_path / "Config").mkdir()
    setup = BrowserSetup(
        browser=BrowserConfig(models_dir=str(tmp_path / "CONFIG")), config_dir=tmp_path / "Config"
    )

    with pytest.raises(SiteModelError, match="apart from the config"):
        resolve_store(setup)


def test_a_refused_location_stops_explore_click_before_the_browser_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blumkin.browser import explore_commands

    setup = _setup(tmp_path, str(tmp_path / "config"), allowed_hosts=("erp.example.com",))
    monkeypatch.setattr(explore_commands, "require_profile", lambda *a, **k: object())

    def attached(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the browser must not be attached")

    monkeypatch.setattr(explore_commands, "attach_exploring", attached)

    with pytest.raises(SiteModelError):
        explore_commands.explore_click(setup, "expense-report", "Add", page_id="list", role="link")
