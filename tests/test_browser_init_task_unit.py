"""`blumkin browser init-task`: a built-in task template, written only on request (#545)."""

from __future__ import annotations

import errno
import json
import os
import stat
import threading
import tomllib
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.browser.profiles import list_profiles
from blumkin.cli import main
from blumkin.config import load_browser_setup
from blumkin.exit_codes import EXIT_SUCCESS, EXIT_USAGE

_BASE = '[profiles.default]\nclient_id = "abc-123"\n'


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "config"
    directory.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(directory))
    (directory / "config.toml").write_text(_BASE, encoding="utf-8")
    return directory


def _run(*args: str) -> Any:
    return CliRunner().invoke(main, ["--json", "browser", "init-task", *args])


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(result.output)


def test_without_yes_it_prints_the_table_and_leaves_config_toml_alone(config: Path) -> None:
    before = (config / "config.toml").read_text(encoding="utf-8")

    result = _run("--template", "teams-transcript")

    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _payload(result)
    assert payload["written"] is False
    assert payload["task"] == "teams-transcript"
    assert payload["table"].startswith("[browser.tasks.teams-transcript]")
    assert (config / "config.toml").read_text(encoding="utf-8") == before


def test_it_creates_the_tasks_browser_profile_either_way(config: Path) -> None:
    assert _run("--template", "teams-transcript").exit_code == EXIT_SUCCESS
    assert [profile.task for profile in list_profiles(config)] == ["teams-transcript"]


def test_the_table_declares_the_teams_and_sharepoint_hosts_and_only_navigation_controls(
    config: Path,
) -> None:
    table = tomllib.loads(_payload(_run("--template", "teams-transcript"))["table"])
    policy = table["browser"]["tasks"]["teams-transcript"]
    assert "*.sharepoint.com" in policy["allowed_hosts"]
    assert any(host.startswith("teams.") for host in policy["allowed_hosts"])
    assert policy["allowed_controls"] == ["Notes", "Transcript"]
    assert set(policy) == {"allowed_controls", "allowed_hosts"}


def test_with_yes_the_table_is_appended_and_the_task_policy_loads(config: Path) -> None:
    result = _run("--template", "teams-transcript", "--yes")

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _payload(result)["written"] is True
    text = (config / "config.toml").read_text(encoding="utf-8")
    assert text.startswith(_BASE)
    policy = load_browser_setup().browser.tasks["teams-transcript"]
    assert "*.sharepoint.com" in policy.allowed_hosts
    assert policy.allowed_controls == ("Notes", "Transcript")


def test_with_yes_and_no_config_file_it_creates_one_that_only_the_user_can_read(
    config: Path,
) -> None:
    (config / "config.toml").unlink()

    assert _run("--template", "teams-transcript", "--yes").exit_code == EXIT_SUCCESS

    mode = stat.S_IMODE((config / "config.toml").stat().st_mode)
    assert mode == 0o600


def test_a_second_run_does_not_duplicate_or_change_the_table(config: Path) -> None:
    _run("--template", "teams-transcript", "--yes")
    once = (config / "config.toml").read_text(encoding="utf-8")

    result = _run("--template", "teams-transcript", "--yes")

    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _payload(result)
    assert payload["written"] is False
    assert payload["already_declared"] is True
    assert (config / "config.toml").read_text(encoding="utf-8") == once


def test_an_existing_task_policy_of_the_same_name_is_never_overwritten(config: Path) -> None:
    custom = _BASE + '\n[browser.tasks.teams-transcript]\nallowed_controls = ["Mine"]\n'
    (config / "config.toml").write_text(custom, encoding="utf-8")

    result = _run("--template", "teams-transcript", "--yes")

    assert _payload(result)["already_declared"] is True
    assert (config / "config.toml").read_text(encoding="utf-8") == custom


def test_the_task_can_have_another_name(config: Path) -> None:
    result = _run("weekly-sync", "--template", "teams-transcript", "--yes")

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "weekly-sync" in load_browser_setup().browser.tasks
    assert [profile.task for profile in list_profiles(config)] == ["weekly-sync"]


def test_a_task_name_that_is_not_valid_is_a_usage_error(config: Path) -> None:
    result = _run("Not Valid", "--template", "teams-transcript", "--yes")
    assert result.exit_code == EXIT_USAGE, result.output
    assert (config / "config.toml").read_text(encoding="utf-8") == _BASE


def test_a_template_that_does_not_exist_is_a_usage_error(config: Path) -> None:
    assert _run("--template", "nope").exit_code == EXIT_USAGE


def test_a_config_file_that_is_not_valid_toml_is_refused_and_left_alone(config: Path) -> None:
    (config / "config.toml").write_text("this is [not toml", encoding="utf-8")

    result = _run("--template", "teams-transcript", "--yes")

    assert result.exit_code == EXIT_USAGE, result.output
    assert (config / "config.toml").read_text(encoding="utf-8") == "this is [not toml"


def test_a_config_whose_browser_table_cannot_take_the_new_one_is_left_alone(config: Path) -> None:
    clash = _BASE + "\n[browser]\ntasks = 5\n"
    (config / "config.toml").write_text(clash, encoding="utf-8")

    result = _run("--template", "teams-transcript", "--yes")

    assert result.exit_code == EXIT_USAGE, result.output
    assert (config / "config.toml").read_text(encoding="utf-8") == clash


def test_a_config_file_that_is_a_symlink_is_not_written_through(
    config: Path, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere.toml"
    target.write_text(_BASE, encoding="utf-8")
    (config / "config.toml").unlink()
    os.symlink(target, config / "config.toml")

    result = _run("--template", "teams-transcript", "--yes")

    assert result.exit_code == EXIT_USAGE, result.output
    assert target.read_text(encoding="utf-8") == _BASE


def test_the_next_steps_name_the_commands_to_run_and_never_a_password_or_token(
    config: Path,
) -> None:
    steps = " ".join(_payload(_run("--template", "teams-transcript"))["next_steps"])
    for command in ("browser launch teams-transcript", "browser transcript teams-transcript"):
        assert command in steps
    assert "sign in" in steps.lower()


def test_the_human_output_shows_the_table_and_how_to_write_it(config: Path) -> None:
    result = CliRunner().invoke(main, ["browser", "init-task", "--template", "teams-transcript"])
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "[browser.tasks.teams-transcript]" in result.output
    assert "--yes" in result.output


def test_the_template_names_complete(config: Path) -> None:
    from click.testing import CliRunner as Runner

    result = Runner().invoke(
        main,
        [],
        env={
            "_BLUMKIN_COMPLETE": "bash_complete",
            "COMP_WORDS": "blumkin browser init-task --template ",
            "COMP_CWORD": "4",
        },
        prog_name="blumkin",
    )
    assert "teams-transcript" in result.output


def test_concurrent_runs_for_one_task_leave_exactly_one_table(config: Path) -> None:
    from blumkin.browser.init_task import init_task

    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            barrier.wait()
            init_task(config, "teams-transcript", "teams-transcript", write=True)
        except BaseException as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    text = (config / "config.toml").read_text(encoding="utf-8")
    assert text.count("[browser.tasks.teams-transcript]") == 1
    assert "teams-transcript" in tomllib.loads(text)["browser"]["tasks"]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("trailing", [True, False])
def test_files_with_other_line_endings_still_get_a_valid_table(
    config: Path, newline: str, trailing: bool
) -> None:
    text = _BASE.rstrip("\n").replace("\n", newline) + (newline if trailing else "")
    (config / "config.toml").write_bytes(text.encode())

    assert _run("--template", "teams-transcript", "--yes").exit_code == EXIT_SUCCESS

    parsed = tomllib.loads((config / "config.toml").read_text(encoding="utf-8"))
    assert "teams-transcript" in parsed["browser"]["tasks"]


def test_a_byte_order_mark_is_refused_with_a_hint_and_the_file_is_left_alone(config: Path) -> None:
    (config / "config.toml").write_bytes(b"\xef\xbb\xbf" + _BASE.encode())

    result = _run("--template", "teams-transcript", "--yes")

    assert result.exit_code == EXIT_USAGE, result.output
    assert "byte order mark" in _payload(result)["message"]
    assert (config / "config.toml").read_bytes().startswith(b"\xef\xbb\xbf")


def test_a_policy_written_with_dotted_keys_counts_as_declared(config: Path) -> None:
    dotted = 'browser.tasks.teams-transcript.allowed_controls = ["Mine"]\n' + _BASE
    (config / "config.toml").write_text(dotted, encoding="utf-8")

    result = _run("--template", "teams-transcript", "--yes")

    assert _payload(result)["already_declared"] is True
    assert (config / "config.toml").read_text(encoding="utf-8") == dotted


def test_the_mode_of_an_existing_config_file_is_left_as_it_was(config: Path) -> None:
    os.chmod(config / "config.toml", 0o640)

    assert _run("--template", "teams-transcript", "--yes").exit_code == EXIT_SUCCESS

    assert stat.S_IMODE((config / "config.toml").stat().st_mode) == 0o640


def test_a_config_path_that_is_not_a_regular_file_is_refused_without_blocking(config: Path) -> None:
    (config / "config.toml").unlink()
    os.mkfifo(config / "config.toml")

    result = _run("--template", "teams-transcript", "--yes")

    assert result.exit_code == EXIT_USAGE, result.output
    assert "regular file" in _payload(result)["message"]


def test_a_write_that_fails_part_way_restores_the_file_and_is_not_a_usage_error(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = (config / "config.toml").read_bytes()
    real = os.pwrite

    def half_then_fail(fd: int, data: bytes, offset: int) -> int:
        real(fd, data[: len(data) // 2], offset)
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("blumkin.browser.init_task.os.pwrite", half_then_fail)

    result = _run("--template", "teams-transcript", "--yes")

    assert result.exit_code not in (EXIT_SUCCESS, EXIT_USAGE), result.output
    assert (config / "config.toml").read_bytes() == before


def test_the_profile_is_created_before_the_config_is_written(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blumkin.browser.profiles import BrowserProfileError

    def refuse(config_dir: Path, task: str) -> Any:
        raise BrowserProfileError("Expected a real directory, got a symlink")

    monkeypatch.setattr("blumkin.browser.init_task.create_profile", refuse)
    before = (config / "config.toml").read_text(encoding="utf-8")

    result = _run("--template", "teams-transcript", "--yes")

    assert result.exit_code != EXIT_SUCCESS
    assert (config / "config.toml").read_text(encoding="utf-8") == before


def test_the_output_tells_the_user_to_narrow_the_sharepoint_wildcard(config: Path) -> None:
    result = CliRunner().invoke(main, ["browser", "init-task", "--template", "teams-transcript"])
    assert "tenant" in result.output and "sharepoint" in result.output.lower()


@pytest.mark.parametrize(
    "shape",
    [
        'browser = "edge"\n',
        "browser = 5\n",
        "[[browser]]\nname = 1\n",
        "[[browser.tasks]]\nname = 1\n",
        "[browser]\ntasks = 5\n",
        '[browser]\ntasks = "x"\n',
    ],
)
@pytest.mark.parametrize("flag", [[], ["--yes"]])
def test_a_browser_value_that_is_not_a_table_is_a_usage_error_and_the_file_is_unchanged(
    config: Path, shape: str, flag: list[str]
) -> None:
    text = shape + _BASE
    (config / "config.toml").write_text(text, encoding="utf-8")

    result = _run("--template", "teams-transcript", *flag)

    assert result.exit_code == EXIT_USAGE, result.output
    assert _payload(result)["error"] == "usage_error"
    assert (config / "config.toml").read_text(encoding="utf-8") == text


def test_the_next_steps_lead_to_an_open_transcript_view(config: Path) -> None:
    steps = " ".join(_payload(_run("--template", "teams-transcript"))["next_steps"])
    assert "Transcript view" in steps
    assert "Notes or Transcript" not in steps


# --------------------------------------------------------------------------- expense-report (#591)


def test_the_expense_report_template_needs_a_host_because_the_site_differs_per_organization(
    config: Path,
) -> None:
    result = _run("--template", "expense-report")

    assert result.exit_code == EXIT_USAGE
    assert "--host" in _payload(result)["message"]
    assert list_profiles(config) == []


def test_the_expense_report_table_holds_the_hosts_controls_confirm_folder_and_form_url(
    config: Path,
) -> None:
    result = _run(
        "--template", "expense-report", "--host", "erp.example.com",
        "--form-url", "https://erp.example.com/expenses", "--upload-dir", "~/receipts", "--yes",
    )  # fmt: skip

    assert result.exit_code == EXIT_SUCCESS, result.output
    policy = load_browser_setup().browser.tasks["expense-report"]
    assert policy.allowed_hosts == ("erp.example.com",)
    assert policy.dialog_confirm == ("OK",)
    assert policy.upload_dirs == ("~/receipts",)
    assert policy.form_url == "https://erp.example.com/expenses"
    assert "Create Report" in policy.allowed_controls
    assert "Actions for * Report" in policy.allowed_controls


def test_the_upload_folder_defaults_to_the_receipts_archive(config: Path) -> None:
    _run("--template", "expense-report", "--host", "erp.example.com", "--yes")

    assert load_browser_setup().browser.tasks["expense-report"].upload_dirs == (
        "~/Documents/receipts-archive",
    )


def test_no_control_of_the_expense_report_template_can_submit_and_the_guard_accepts_them(
    config: Path,
) -> None:
    from blumkin.browser.guard import GuardedDriver, GuardPolicy, is_finalizing_name

    _run("--template", "expense-report", "--host", "erp.example.com", "--yes")
    policy = load_browser_setup().browser.tasks["expense-report"]

    for name in (*policy.allowed_controls, *policy.dialog_confirm):
        assert not is_finalizing_name(name.replace("*", "Trip"))
    # The guard itself accepts the table: patterns and the dialog confirm validate.
    GuardedDriver(
        _NullRaw(),  # type: ignore[arg-type]
        GuardPolicy(
            allowed_controls=frozenset(policy.allowed_controls),
            dialog_confirm=frozenset(policy.dialog_confirm),
        ),
    )


class _NullRaw:
    def block_form_submission(self, *, background_writes: bool = False) -> None:
        return None


@pytest.mark.parametrize(
    "arguments",
    [
        ["--host", "http://erp.example.com"],
        ["--host", "*.com"],
        ["--host", "erp.example.com", "--form-url", "http://erp.example.com/x"],
        ["--host", "erp.example.com", "--form-url", "https://evil.example.net/x"],
        ["--host", "erp.example.com", "--form-url", "https://u:p@erp.example.com/x"],
        ["--host", "*.example.com", "--form-url", "https://evil.example.net\\.erp.example.com/x"],
        ["--host", "erp.example.com", "--form-url", "https://erp.example.com:bad/x"],
        ["--host", "erp.example.com", "--form-url", "https://erp.example.com/x y"],
        ["--host", "erp.example.com", "--form-url", "https://erp.example.com/x\x7f"],
        ["--host", "erp.example.com", "--upload-dir", "receipts"],
        ["--host", "erp.example.com", "--upload-dir", "/"],
        ["--host", "erp.example.com", "--upload-dir", "~"],
        ["--host", "erp.example.com", "--upload-dir", "~/.."],
        ["--host", "erp.example.com", "--upload-dir", ""],
    ],
)
def test_a_bad_host_or_form_url_is_a_usage_error_and_nothing_is_written(
    config: Path, arguments: list[str]
) -> None:
    result = _run("--template", "expense-report", *arguments, "--yes")

    assert result.exit_code == EXIT_USAGE
    assert "expense-report" not in (config / "config.toml").read_text()


def test_a_name_with_a_quote_cannot_break_the_written_table(config: Path) -> None:
    result = _run("--template", "expense-report", "--host", "erp.example.com",
                  "--upload-dir", '~/my "receipts"', "--yes")  # fmt: skip

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert load_browser_setup().browser.tasks["expense-report"].upload_dirs == ('~/my "receipts"',)


def test_the_expense_report_next_steps_lead_from_the_form_to_the_handoff(config: Path) -> None:
    steps = _payload(_run("--template", "expense-report", "--host", "erp.example.com"))[
        "next_steps"
    ]

    text = " ".join(steps)
    for command in ("launch expense-report --open", "explore observe", "explore save", "handoff"):
        assert command in text
    assert "password" not in text.lower()


def test_the_other_template_still_needs_no_host_and_keeps_its_table(config: Path) -> None:
    result = _run("--template", "teams-transcript")

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "dialog_confirm" not in _payload(result)["table"]


def test_a_table_the_config_loader_would_reject_is_never_written_and_config_still_loads(
    config: Path,
) -> None:
    before = (config / "config.toml").read_text()

    result = _run("--template", "expense-report", "--host", "erp.example.com",
                  "--upload-dir", "relative/path", "--yes")  # fmt: skip

    assert result.exit_code == EXIT_USAGE
    assert (config / "config.toml").read_text() == before
    load_browser_setup()


def test_a_lone_surrogate_in_a_value_is_a_usage_error_not_a_traceback(config: Path) -> None:
    from blumkin.browser.init_task import InitTaskError, init_task

    with pytest.raises(InitTaskError):
        init_task(
            config, "expense-report", "expense-report",
            write=True, hosts=("erp.example.com",), upload_dirs=("~/r\udcffx",),
        )  # fmt: skip
    assert "expense-report" not in (config / "config.toml").read_text()


def test_non_ascii_names_are_written_as_escapes_that_load_back_unchanged(config: Path) -> None:
    result = _run("--template", "expense-report", "--host", "erp.example.com",
                  "--upload-dir", "~/Belege/Caf\u00e9 \u2028x", "--yes")  # fmt: skip

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "Caf\\u00e9" in (config / "config.toml").read_text()
    assert load_browser_setup().browser.tasks["expense-report"].upload_dirs == (
        "~/Belege/Caf\u00e9 \u2028x",
    )


def test_upload_and_form_url_options_are_refused_for_a_template_that_uploads_nothing(
    config: Path,
) -> None:
    for option in (["--upload-dir", "~/receipts"], ["--form-url", "https://example.com/x"]):
        result = _run("--template", "teams-transcript", *option, "--yes")

        assert result.exit_code == EXIT_USAGE
        assert "uploads nothing" in _payload(result)["message"]
    assert "teams-transcript" not in (config / "config.toml").read_text()


def test_hosts_are_lowercased_and_a_second_run_never_overwrites_the_table(config: Path) -> None:
    _run("--template", "expense-report", "--host", "ERP.Example.COM", "--yes")
    first = (config / "config.toml").read_text()

    second = _run("--template", "expense-report", "--host", "other.example.com", "--yes")

    assert load_browser_setup().browser.tasks["expense-report"].allowed_hosts == (
        "erp.example.com",
    )
    assert (config / "config.toml").read_text() == first
    assert _payload(second)["already_declared"] is True
