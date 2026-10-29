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


def test_task_policies_are_parsed_from_browser_tasks_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(
        tmp_path,
        monkeypatch,
        '[browser.tasks.expense-report]\nallowed_controls = ["Add line", "Create report"]\n'
        'extra_deny_names = ["release"]\n\n',
    )
    policy = load_config().browser.tasks["expense-report"]
    assert policy.allowed_controls == ("Add line", "Create report")
    assert policy.extra_deny_names == ("release",)


def test_a_task_policy_defaults_to_nothing_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path, monkeypatch, "[browser.tasks.time-sheet]\n\n")
    policy = load_config().browser.tasks["time-sheet"]
    assert policy.allowed_controls == ()
    assert policy.extra_deny_names == ()


def test_no_task_policies_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config(tmp_path, monkeypatch, '[browser]\ndefault = "edge"\n\n')
    assert load_config().browser.tasks == {}


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ('[browser.tasks.expense-report]\nallowed_controls = "Add line"\n\n', "allowed_controls"),
        ("[browser.tasks.expense-report]\nallowed_controls = [1]\n\n", "allowed_controls"),
        ('[browser.tasks.expense-report]\nextra_deny_names = "x"\n\n', "extra_deny_names"),
        ('[browser.tasks."Bad Name"]\n\n', "task name"),
        ('[browser]\ntasks = "x"\n\n', "browser.tasks"),
        ('[browser.tasks]\nexpense-report = "x"\n\n', "browser.tasks.expense-report"),
    ],
)
def test_malformed_task_policies_are_config_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, needle: str
) -> None:
    _config(tmp_path, monkeypatch, body)
    with pytest.raises(ProviderConfigError) as excinfo:
        load_config()
    assert needle in str(excinfo.value)


def test_upload_dirs_are_parsed_and_default_to_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(
        tmp_path,
        monkeypatch,
        '[browser.tasks.expense-report]\nupload_dirs = ["~/receipts", "/srv/receipts"]\n\n'
        "[browser.tasks.time-sheet]\n\n",
    )
    tasks = load_config().browser.tasks
    assert tasks["expense-report"].upload_dirs == ("~/receipts", "/srv/receipts")
    assert tasks["time-sheet"].upload_dirs == ()


@pytest.mark.parametrize("entry", ["receipts", "./receipts", "../x", "~other/receipts", "~"])
def test_an_upload_dir_must_be_absolute_or_start_with_tilde_slash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    _config(
        tmp_path,
        monkeypatch,
        f'[browser.tasks.expense-report]\nupload_dirs = ["{entry}"]\n\n',
    )
    with pytest.raises(ProviderConfigError) as excinfo:
        load_config()
    assert "upload_dirs" in str(excinfo.value)


def test_an_unknown_key_in_a_task_table_is_an_error_not_a_silent_empty_allow_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(
        tmp_path,
        monkeypatch,
        '[browser.tasks.expense-report]\nallowed_control = ["Add line"]\n\n',
    )
    with pytest.raises(ProviderConfigError) as excinfo:
        load_config()
    message = str(excinfo.value)
    assert "allowed_control" in message
    assert "allowed_controls" in message


def test_an_unknown_key_under_browser_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path, monkeypatch, '[browser]\ndefualt = "edge"\n\n')
    with pytest.raises(ProviderConfigError) as excinfo:
        load_config()
    assert "defualt" in str(excinfo.value)


def test_allowed_hosts_are_parsed_lower_cased_and_default_to_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(
        tmp_path,
        monkeypatch,
        '[browser.tasks.expense-report]\nallowed_hosts = ["ERP.Example.com", "*.corp.example.com"]'
        "\n\n[browser.tasks.time-sheet]\n\n",
    )
    tasks = load_config().browser.tasks
    assert tasks["expense-report"].allowed_hosts == ("erp.example.com", "*.corp.example.com")
    assert tasks["time-sheet"].allowed_hosts == ()


@pytest.mark.parametrize(
    "entry",
    [
        "https://erp.example.com",
        "erp.example.com/path",
        "*",
        "*.",
        "a b",
        "erp.example.com:8443",
        "",
    ],
)
def test_an_allowed_host_must_be_a_bare_host_name_or_a_subdomain_wildcard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    _config(
        tmp_path,
        monkeypatch,
        f'[browser.tasks.expense-report]\nallowed_hosts = ["{entry}"]\n\n',
    )
    with pytest.raises(ProviderConfigError) as excinfo:
        load_config()
    assert "allowed_hosts" in str(excinfo.value)


@pytest.mark.parametrize("entry", ["/", "//", "/."])
def test_an_upload_dir_that_is_the_filesystem_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    _config(tmp_path, monkeypatch, f'[browser.tasks.expense-report]\nupload_dirs = ["{entry}"]\n\n')
    with pytest.raises(ProviderConfigError) as excinfo:
        load_config()
    assert "upload_dirs" in str(excinfo.value)
    assert "root" in str(excinfo.value)


@pytest.mark.parametrize("host", ["*.com", "*.co", "*.localhost"])
def test_a_wildcard_host_needs_a_registrable_domain_after_the_star(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    _config(
        tmp_path, monkeypatch, f'[browser.tasks.expense-report]\nallowed_hosts = ["{host}"]\n\n'
    )
    with pytest.raises(ProviderConfigError) as excinfo:
        load_config()
    assert "allowed_hosts" in str(excinfo.value)


@pytest.mark.parametrize(
    "entry",
    [
        "~/",
        "~/.",
        "/a/..",
        "/a/../..",
        "~/..",
        "~/x/../..",
        "~/../shared",
        "~/x/../../y",
        "~//../shared",
        "~///..",
        "~//",
    ],
)
def test_an_upload_dir_that_resolves_to_the_root_or_the_whole_home_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    _config(tmp_path, monkeypatch, f'[browser.tasks.expense-report]\nupload_dirs = ["{entry}"]\n\n')
    with pytest.raises(ProviderConfigError) as excinfo:
        load_config()
    assert "upload_dirs" in str(excinfo.value)


def test_a_folder_below_the_home_directory_is_still_a_valid_upload_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(
        tmp_path,
        monkeypatch,
        '[browser.tasks.expense-report]\nupload_dirs = ["~/receipts", "/srv/receipts/inbox"]\n\n',
    )
    load_config()


def test_download_dirs_are_parsed_and_default_to_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(
        tmp_path,
        monkeypatch,
        '[browser.tasks.meeting]\ndownload_dirs = ["~/transcripts", "/srv/transcripts"]\n\n'
        "[browser.tasks.other]\n\n",
    )
    tasks = load_config().browser.tasks
    assert tasks["meeting"].download_dirs == ("~/transcripts", "/srv/transcripts")
    assert tasks["other"].download_dirs == ()


@pytest.mark.parametrize(
    "entry", ["transcripts", "./t", "../x", "~other/t", "~", "~/", "~/..", "/", "//"]
)
def test_a_download_dir_follows_the_same_rules_as_an_upload_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    _config(tmp_path, monkeypatch, f'[browser.tasks.meeting]\ndownload_dirs = ["{entry}"]\n\n')
    with pytest.raises(ProviderConfigError) as excinfo:
        load_config()
    assert "download_dirs" in str(excinfo.value)


def test_strict_mode_is_off_by_default_and_read_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(
        tmp_path,
        monkeypatch,
        "[browser.tasks.plain]\n\n[browser.tasks.simple-form]\nblock_background_writes = true\n\n",
    )
    tasks = load_config().browser.tasks
    assert tasks["plain"].block_background_writes is False
    assert tasks["simple-form"].block_background_writes is True


@pytest.mark.parametrize("value", ['"yes"', "1", "[true]"])
def test_strict_mode_must_be_a_real_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _config(
        tmp_path, monkeypatch, f"[browser.tasks.simple-form]\nblock_background_writes = {value}\n\n"
    )
    with pytest.raises(ProviderConfigError, match="block_background_writes"):
        load_config()


def test_there_is_no_form_get_setting_and_the_error_lists_what_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path, monkeypatch, "[browser.tasks.simple-form]\nblock_form_gets = true\n\n")
    with pytest.raises(ProviderConfigError, match="block_form_gets") as excinfo:
        load_config()
    assert "block_background_writes" in str(excinfo.value)
