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

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required")


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


def test_help_and_doctor_do_not_trigger_the_first_run_check(tmp_path: Path) -> None:
    config = tmp_path / "empty"
    helped = _run(["--help"], config, tmp_path)
    assert helped.returncode == 0 and "Usage" in helped.stdout
    assert not config.exists()


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
