"""An operator's physical address on a profile: private to set, hidden from listings, and
readable only by a browser task that was authorized to use it (issue #613).

Every address in these tests is synthetic.
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.config import list_profiles, load_browser_setup, load_config, set_profile_address
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_SUCCESS, EXIT_USAGE

_ADDRESS = "1 Example Street, Springfield, EX 00000"

_CONFIG = """\
default_profile = "work"

[profiles.personal]
provider = "google"

[profiles.work]
provider = "microsoft"
client_id = "abc"
tenant_id = "example.com"
email = "ada@example.com"

[browser.tasks.expense-report]
allowed_hosts = ["erp.example.com"]
use_profile_address = true

[browser.tasks.time-sheet]
allowed_hosts = ["erp.example.com"]
"""


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "config.toml").write_text(_CONFIG)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _run(*args: str, input_text: str | None = None) -> object:
    return CliRunner().invoke(main, ["--json", *args], input=input_text)


def _address_in(config_dir: Path, profile: str) -> object:
    return tomllib.loads((config_dir / "config.toml").read_text())["profiles"][profile].get(
        "address"
    )


def test_a_profile_has_no_address_until_one_is_set(config_dir: Path) -> None:
    assert load_config().address == ""


def test_set_address_writes_only_the_chosen_profile(config_dir: Path) -> None:
    result = _run("--profile", "work", "profiles", "set-address", "--address", _ADDRESS)

    assert result.exit_code == EXIT_SUCCESS, result.output  # type: ignore[attr-defined]
    assert _address_in(config_dir, "work") == _ADDRESS
    assert _address_in(config_dir, "personal") is None
    assert load_config().address == _ADDRESS


def test_set_address_never_echoes_the_address(config_dir: Path) -> None:
    result = _run("--profile", "work", "profiles", "set-address", "--address", _ADDRESS)

    assert "Springfield" not in result.output  # type: ignore[attr-defined]
    payload = json.loads(result.output)  # type: ignore[attr-defined]
    assert payload == {"address_set": True, "ok": True, "profile": "work"}


def test_the_address_can_be_read_from_stdin_so_it_stays_out_of_shell_history(
    config_dir: Path,
) -> None:
    result = _run("profiles", "set-address", "--stdin", input_text=_ADDRESS + "\n")

    assert result.exit_code == EXIT_SUCCESS, result.output  # type: ignore[attr-defined]
    assert _address_in(config_dir, "work") == _ADDRESS


def test_the_address_is_prompted_for_when_no_option_is_given(config_dir: Path) -> None:
    result = CliRunner().invoke(main, ["profiles", "set-address"], input=_ADDRESS + "\n")

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _address_in(config_dir, "work") == _ADDRESS
    assert "Springfield" not in result.output.replace("Address:", "")


def test_clear_removes_the_address(config_dir: Path) -> None:
    _run("profiles", "set-address", "--address", _ADDRESS)

    result = _run("profiles", "set-address", "--clear")

    assert result.exit_code == EXIT_SUCCESS, result.output  # type: ignore[attr-defined]
    assert _address_in(config_dir, "work") is None
    assert load_config().address == ""
    assert json.loads(result.output)["address_set"] is False  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "args",
    [
        ["--address", "a", "--stdin"],
        ["--address", "a", "--clear"],
        ["--stdin", "--clear"],
        ["--address", "  "],
        ["--address", "line one\nline two"],
    ],
)
def test_set_address_refuses_bad_option_combinations_and_values(
    config_dir: Path, args: list[str]
) -> None:
    result = _run("profiles", "set-address", *args)

    assert result.exit_code == EXIT_USAGE  # type: ignore[attr-defined]
    assert _address_in(config_dir, "work") is None


def test_an_unknown_profile_is_not_found(config_dir: Path) -> None:
    result = _run("--profile", "nobody", "profiles", "set-address", "--address", _ADDRESS)

    assert result.exit_code != EXIT_SUCCESS  # type: ignore[attr-defined]
    assert "Springfield" not in result.output  # type: ignore[attr-defined]


def test_a_stored_address_is_not_in_the_profile_listing_only_that_one_exists(
    config_dir: Path,
) -> None:
    _run("--profile", "work", "profiles", "set-address", "--address", _ADDRESS)

    rows = {row["name"]: row for row in list_profiles()}
    listing = _run("profiles", "list").output  # type: ignore[attr-defined]

    assert rows["work"]["has_address"] is True
    assert rows["personal"]["has_address"] is False
    assert "address" not in {key for key in rows["work"] if key != "has_address"}
    assert "Springfield" not in listing and "Springfield" not in json.dumps(rows)


def test_the_address_is_left_out_of_the_config_repr(config_dir: Path) -> None:
    _run("--profile", "work", "profiles", "set-address", "--address", _ADDRESS)

    assert "Springfield" not in repr(load_config())


def test_a_loosely_permissioned_config_file_is_made_private_when_it_gets_an_address(
    config_dir: Path,
) -> None:
    path = config_dir / "config.toml"
    path.chmod(0o644)

    _run("profiles", "set-address", "--address", _ADDRESS)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_multi_line_address_on_stdin_becomes_one_line(config_dir: Path) -> None:
    result = _run(
        "profiles", "set-address", "--stdin", input_text="1 Example Street\n\nSpringfield\n"
    )

    assert result.exit_code == EXIT_SUCCESS, result.output  # type: ignore[attr-defined]
    assert _address_in(config_dir, "work") == "1 Example Street, Springfield"


def test_an_address_that_cannot_be_encoded_is_a_usage_error(config_dir: Path) -> None:
    result = _run("profiles", "set-address", "--address", "bad \udcff address")

    assert result.exit_code == EXIT_USAGE  # type: ignore[attr-defined]
    assert _address_in(config_dir, "work") is None


def test_a_blank_stored_address_does_not_count_as_one(config_dir: Path) -> None:
    (config_dir / "config.toml").write_text(
        _CONFIG.replace('email = "ada@example.com"', 'email = "ada@example.com"\naddress = "   "')
    )

    assert {row["name"]: row["has_address"] for row in list_profiles()}["work"] is False


def test_the_hidden_prompt_goes_to_stderr_so_json_output_stays_clean(config_dir: Path) -> None:
    result = CliRunner().invoke(main, ["--json", "profiles", "set-address"], input=_ADDRESS + "\n")

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert json.loads(result.stdout)["address_set"] is True


def test_comments_and_other_keys_survive_the_write(config_dir: Path) -> None:
    _run("--profile", "work", "profiles", "set-address", "--address", _ADDRESS)

    text = (config_dir / "config.toml").read_text()
    assert 'email = "ada@example.com"' in text and "[browser.tasks.time-sheet]" in text


def test_an_address_with_quotes_and_unicode_round_trips(config_dir: Path) -> None:
    tricky = 'Flat "B", 12 Rue d\'Exemple, Zürich'

    assert set_profile_address(config_dir / "config.toml", profile="work", address=tricky)

    assert load_config().address == tricky


# ---------------------------------------------------------------- task access


def test_an_authorized_task_can_read_the_address(config_dir: Path) -> None:
    _run("--profile", "work", "profiles", "set-address", "--address", _ADDRESS)

    result = _run("--profile", "work", "browser", "address", "expense-report")

    assert result.exit_code == EXIT_SUCCESS, result.output  # type: ignore[attr-defined]
    payload = json.loads(result.output)  # type: ignore[attr-defined]
    assert payload["address"] == _ADDRESS
    assert payload["task"] == "expense-report"
    assert "sensitive" in payload["note"]


def test_a_task_that_was_not_authorized_cannot_read_it(config_dir: Path) -> None:
    _run("--profile", "work", "profiles", "set-address", "--address", _ADDRESS)

    result = _run("--profile", "work", "browser", "address", "time-sheet")

    assert result.exit_code != EXIT_SUCCESS  # type: ignore[attr-defined]
    assert "Springfield" not in result.output  # type: ignore[attr-defined]
    assert "use_profile_address" in result.output  # type: ignore[attr-defined]


def test_an_unknown_task_cannot_read_it(config_dir: Path) -> None:
    _run("--profile", "work", "profiles", "set-address", "--address", _ADDRESS)

    result = _run("--profile", "work", "browser", "address", "no-such-task")

    assert result.exit_code != EXIT_SUCCESS  # type: ignore[attr-defined]
    assert "Springfield" not in result.output  # type: ignore[attr-defined]


def test_an_authorized_task_with_no_address_set_says_so(config_dir: Path) -> None:
    result = _run("--profile", "work", "browser", "address", "expense-report")

    assert result.exit_code == EXIT_NOT_FOUND  # type: ignore[attr-defined]
    assert "set-address" in result.output  # type: ignore[attr-defined]


def test_authorization_is_a_declared_boolean(config_dir: Path) -> None:
    setup = load_browser_setup()

    assert setup.browser.tasks["expense-report"].use_profile_address is True
    assert setup.browser.tasks["time-sheet"].use_profile_address is False


def test_authorization_must_be_a_boolean(config_dir: Path) -> None:
    (config_dir / "config.toml").write_text(
        _CONFIG.replace("use_profile_address = true", 'use_profile_address = "yes"')
    )

    result = _run("browser", "address", "expense-report")

    assert result.exit_code != EXIT_SUCCESS  # type: ignore[attr-defined]


def test_other_browser_commands_never_show_the_address(config_dir: Path) -> None:
    _run("--profile", "work", "profiles", "set-address", "--address", _ADDRESS)

    for args in (["profiles", "list"], ["doctor"]):
        result = _run("--profile", "work", *args)
        assert result.output.strip()  # type: ignore[attr-defined]  # it printed something
        assert "Springfield" not in result.output  # type: ignore[attr-defined]


def test_the_new_content_is_written_to_a_private_temporary_file_then_swapped_in(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config_dir / "config.toml"
    path.chmod(0o644)
    seen: list[tuple[int, str]] = []
    real_replace = os.replace

    def spy(source: object, target: object) -> None:
        seen.append((stat.S_IMODE(Path(str(source)).stat().st_mode), Path(str(source)).read_text()))
        real_replace(source, target)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", spy)

    assert set_profile_address(path, profile="work", address=_ADDRESS)

    assert [mode for mode, _ in seen] == [0o600]
    assert _ADDRESS in seen[0][1]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(config_dir.glob(".config-*"))


def test_a_failed_swap_leaves_the_old_file_whole_and_no_temporary_file(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config_dir / "config.toml"
    before = path.read_text()

    def refuse(source: object, target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)

    assert set_profile_address(path, profile="work", address=_ADDRESS) is False
    assert path.read_text() == before
    assert not list(config_dir.glob(".config-*"))


def test_clearing_an_address_keeps_the_files_existing_mode(config_dir: Path) -> None:
    path = config_dir / "config.toml"
    set_profile_address(path, profile="work", address=_ADDRESS)
    path.chmod(0o640)

    assert set_profile_address(path, profile="work", address="")

    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert _address_in(config_dir, "work") is None


def test_a_symlinked_config_is_updated_at_its_target(
    config_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    real_dir = tmp_path_factory.mktemp("real")
    real = real_dir / "config.toml"
    real.write_text((config_dir / "config.toml").read_text())
    link = config_dir / "linked.toml"
    link.symlink_to(real)

    assert set_profile_address(link, profile="work", address=_ADDRESS)

    assert link.is_symlink()
    assert _ADDRESS in real.read_text()


def test_a_stdin_far_longer_than_an_address_is_refused_not_truncated(config_dir: Path) -> None:
    padded = "1 Example Street\n" + " " * 5000 + "\nSpringfield\n"

    result = _run("profiles", "set-address", "--stdin", input_text=padded)

    assert result.exit_code == EXIT_USAGE  # type: ignore[attr-defined]
    assert _address_in(config_dir, "work") is None
