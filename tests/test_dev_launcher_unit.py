"""`scripts/dev/blumkin`: run the checkout with no install step (issue #524).

These run the real script with `uv run --frozen`, so they need `uv` and the project
environment, which every contributor already has. The terminal cases drive a real
pseudo-terminal with `pexpect`, the same way the end-to-end agent tests will.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pexpect
import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "dev" / "blumkin"

pytestmark = [
    pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required"),
    pytest.mark.skipif(not (_ROOT / ".git").exists(), reason="needs a git checkout"),
]


def _env(config: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BLUMKIN_", "XDG_"))}
    env["BLUMKIN_CONFIG_DIR"] = str(config)
    return env


def _run(args: list[str], config: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_SCRIPT), *args],
        capture_output=True,
        check=False,
        cwd=cwd,
        env=_env(config),
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=180,
    )


def test_the_script_is_a_checked_in_executable_bash_script() -> None:
    assert _SCRIPT.is_file() and os.access(_SCRIPT, os.X_OK)
    head = _SCRIPT.read_text().splitlines()
    assert head[0] == "#!/usr/bin/env bash"
    assert "set -euo pipefail" in head[:12]
    assert "." not in _SCRIPT.name


def test_the_version_comes_from_the_checkout_and_needs_no_config(tmp_path: Path) -> None:
    result = _run(["--version"], tmp_path / "empty", tmp_path)
    assert result.returncode == 0, result.stderr
    assert re.search(r"\+dev\.[0-9a-f]{7}\.\d{8}", result.stdout.splitlines()[0])
    assert not (tmp_path / "empty").exists()


def test_it_runs_from_any_directory_and_passes_exit_codes_through(tmp_path: Path) -> None:
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "config.toml").write_text('[profiles.default]\nprovider = "microsoft"\n')
    elsewhere = tmp_path / "somewhere" / "else"
    elsewhere.mkdir(parents=True)
    ok = _run(["skills", "list", "--json"], config, elsewhere)
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["skills"]
    bad = _run(["no-such-command"], config, elsewhere)
    assert bad.returncode == 2


def test_without_a_config_and_without_a_terminal_it_fails_with_a_hint(tmp_path: Path) -> None:
    config = tmp_path / "empty"
    result = _run(["mail", "inbox"], config, tmp_path)
    assert result.returncode == 2
    assert str(config) in result.stderr
    assert "auth setup" in result.stderr and "--provider" in result.stderr
    assert not config.exists()


def test_help_and_version_do_not_trigger_the_first_run_check(tmp_path: Path) -> None:
    config = tmp_path / "empty"
    helped = _run(["--help"], config, tmp_path)
    assert helped.returncode == 0 and "Usage" in helped.stdout
    assert not config.exists()


def test_profiles_list_runs_without_a_config_for_the_skill_cold_start(tmp_path: Path) -> None:
    """The `blumkin` skill's first step reads `count: 0` from this command (#564)."""
    config = tmp_path / "empty"
    result = _run(["profiles", "list", "--json"], config, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["count"] == 0
    assert "no config.toml" not in result.stderr
    assert not config.exists()


def test_a_short_h_is_not_help_because_blumkin_does_not_accept_it(tmp_path: Path) -> None:
    config = tmp_path / "empty"
    result = _run(["mail", "-h"], config, tmp_path)
    assert result.returncode == 2
    assert "no config.toml" in result.stderr


def test_commands_that_need_no_profile_run_without_a_config(tmp_path: Path) -> None:
    """Only provider-bound commands are gated; the rest answer for themselves (#565)."""
    config = tmp_path / "empty"
    completion = _run(["completion", "zsh"], config, tmp_path)
    assert completion.returncode == 0, completion.stderr
    assert "no config.toml" not in completion.stderr
    agent = _run(["agent", "status"], config, tmp_path)
    assert agent.returncode in (0, 1), agent.stderr
    assert "no config.toml" not in agent.stderr
    assert not config.exists()


def test_mcp_install_runs_without_a_config_because_registering_needs_no_credentials(
    tmp_path: Path,
) -> None:
    config = tmp_path / "empty"
    project = tmp_path / "project"
    project.mkdir()
    done = _run(
        # Cursor's config is merged as a file, so the test needs no external client CLI.
        ["mcp", "install", "--client", "cursor", "--scope", "project", "--yes", "--json"],
        config,
        project,
    )
    assert "no config.toml" not in done.stderr
    assert done.returncode == 0, done.stderr
    assert (project / ".cursor" / "mcp.json").is_file()


def _needs_config(*args: str) -> bool:
    """Run the launcher's own `needs_config` function on `args` (exit 0 means it needs one)."""
    text = _SCRIPT.read_text()
    function = text[
        text.index("needs_config() {") : text.index("\n}\n", text.index("needs_config() {")) + 3
    ]
    done = subprocess.run(
        ["bash", "-c", function + '\nneeds_config "$@"', "needs-config", *args],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert done.returncode in (0, 1), done.stderr
    return done.returncode == 0


@pytest.mark.parametrize(
    "args",
    [
        ["mail", "inbox"],
        ["calendar", "today"],
        ["chat", "find"],
        ["drive", "list"],
        ["docs", "list"],
        ["people", "resolve"],
        ["meeting", "find"],
        ["tasks", "list"],
        ["capabilities"],
        ["doctor"],
        ["auth", "status"],
        ["auth", "login"],
        ["mcp", "serve"],
        # These create state or act on a profile, so they keep the friendly setup hint.
        ["browser", "launch"],
        ["loop", "status"],
        ["receipts", "find"],
        # A global option's value is not the command.
        ["--profile", "work", "mail", "inbox"],
        ["--profile=work", "mail", "inbox"],
        ["--tz", "UTC", "mail", "inbox"],
    ],
)
def test_the_launcher_gates_every_provider_bound_command(args: list[str]) -> None:
    assert _needs_config(*args) is True


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--help"],
        ["--version"],
        ["mail", "--help"],
        ["auth", "setup"],
        ["profiles", "list"],
        ["completion", "bash"],
        ["agent", "status"],
        ["mcp", "install"],
        ["mcp", "status"],
        ["skills", "list"],
        ["upgrade"],
        ["--profile", "work", "completion", "bash"],
        ["--tz", "UTC", "completion", "bash"],
    ],
)
def test_the_launcher_does_not_gate_commands_that_answer_for_themselves(args: list[str]) -> None:
    assert _needs_config(*args) is False


def test_provider_bound_commands_are_still_refused_with_the_setup_hint(tmp_path: Path) -> None:
    for args in (["mail", "inbox"], ["doctor"], ["auth", "status"], ["capabilities"]):
        result = _run(args, tmp_path / "empty", tmp_path)
        assert result.returncode == 2, args
        assert "no config.toml" in result.stderr, args


def _session(args: list[str], config: Path, cwd: Path) -> pexpect.spawn:
    return pexpect.spawn(
        str(_SCRIPT),
        args,
        cwd=str(cwd),
        env=_env(config),
        encoding="utf-8",
        timeout=120,
        dimensions=(40, 120),
    )


def test_on_a_terminal_it_offers_the_guided_setup_and_a_no_leaves_everything_alone(
    tmp_path: Path,
) -> None:
    config = tmp_path / "empty"
    child = _session(["mail", "inbox"], config, tmp_path)
    child.expect("Run the guided setup now")
    child.sendline("n")
    child.expect(pexpect.EOF)
    child.close()
    assert child.exitstatus == 2
    assert "auth setup" in str(child.before)
    assert not config.exists()


def test_on_a_terminal_a_yes_starts_auth_setup_which_asks_for_the_provider(
    tmp_path: Path,
) -> None:
    config = tmp_path / "empty"
    child = _session(["mail", "inbox"], config, tmp_path)
    child.expect("Run the guided setup now")
    child.sendline("y")
    child.expect("Which provider will this profile use")
    # Nothing is written until the provider is chosen and the walkthrough completes.
    assert not config.exists()
    child.terminate(force=True)
    child.close()


def test_end_of_input_at_the_prompt_is_a_clean_decline(tmp_path: Path) -> None:
    config = tmp_path / "empty"
    child = _session(["mail", "inbox"], config, tmp_path)
    child.expect("Run the guided setup now")
    child.sendeof()
    child.expect(pexpect.EOF)
    child.close()
    assert child.exitstatus == 2
    assert "auth setup" in str(child.before)
    assert not config.exists()


def test_the_profile_option_in_its_equals_form_reaches_the_guided_setup(tmp_path: Path) -> None:
    config = tmp_path / "empty"
    child = _session(["--profile=work", "mail", "inbox"], config, tmp_path)
    child.expect("Run the guided setup now")
    child.sendline("y")
    child.expect("Which provider will this profile use")
    child.sendline("microsoft")
    child.expect("Console steps for microsoft")
    child.terminate(force=True)
    child.close()
    assert not config.exists() or "profiles.work" in (config / "config.toml").read_text()


def test_a_blank_config_dir_variable_counts_as_unset_and_a_tilde_is_expanded(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home & more"
    home.mkdir()
    env = _env(tmp_path / "unused")
    env["HOME"] = str(home)
    env["BLUMKIN_CONFIG_DIR"] = "   "
    blank = subprocess.run(
        [str(_SCRIPT), "mail", "inbox"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=180,
    )
    assert str(home / ".config" / "blumkin") in blank.stderr
    env["BLUMKIN_CONFIG_DIR"] = "~/chosen"
    tilde = subprocess.run(
        [str(_SCRIPT), "mail", "inbox"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=180,
    )
    assert str(home / "chosen") in tilde.stderr


def test_a_global_option_value_is_not_mistaken_for_the_subcommand(tmp_path: Path) -> None:
    config = tmp_path / "empty"
    exempt = _run(["--tz", "America/New_York", "skills", "list", "--json"], config, tmp_path)
    assert exempt.returncode == 0, exempt.stderr
    blocked = _run(["--tz", "UTC", "mail", "inbox"], config, tmp_path)
    assert blocked.returncode == 2 and "auth setup" in blocked.stderr
    assert not config.exists()


def test_the_config_dir_value_is_trimmed_like_the_python_side(tmp_path: Path) -> None:
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    (chosen / "config.toml").write_text('[profiles.default]\nprovider = "microsoft"\n')
    env = _env(chosen)
    env["BLUMKIN_CONFIG_DIR"] = f"{chosen}  "
    result = subprocess.run(
        [str(_SCRIPT), "mail", "inbox"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=180,
    )
    assert "no config.toml" not in result.stderr


def test_the_launcher_tells_blumkin_where_it_is_so_mcp_install_can_register_it() -> None:
    text = _SCRIPT.read_text()
    assert "export BLUMKIN_DEV=1" in text
    assert "export BLUMKIN_DEV_LAUNCHER=" in text
