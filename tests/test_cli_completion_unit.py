"""`blumkin completion <shell>` prints a usable script (issue #98)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.exit_codes import EXIT_SUCCESS, EXIT_USAGE


@pytest.mark.parametrize(
    ("shell", "needle"),
    [
        ("bash", "_BLUMKIN_COMPLETE=bash_complete"),
        ("zsh", "compdef _blumkin_completion blumkin"),
        ("fish", "_BLUMKIN_COMPLETE=fish_complete"),
    ],
)
def test_completion_emits_a_script_per_shell(shell: str, needle: str) -> None:
    result = CliRunner().invoke(main, ["completion", shell])
    assert result.exit_code == EXIT_SUCCESS
    assert needle in result.output
    assert "blumkin" in result.output
    assert "Traceback" not in result.output


def test_completion_rejects_unknown_shell() -> None:
    result = CliRunner().invoke(main, ["completion", "tcsh"])
    assert result.exit_code == EXIT_USAGE
    # Click writes the Choice usage error to stderr on newer Click; combine.
    assert "tcsh" in (result.output or "") + (result.stderr or "")


def test_completion_requires_a_shell_argument() -> None:
    result = CliRunner().invoke(main, ["completion"])
    assert result.exit_code == EXIT_USAGE


def test_completion_help_lists_all_three_shells() -> None:
    out = CliRunner().invoke(main, ["completion", "--help"]).output
    for shell in ("bash", "zsh", "fish"):
        assert shell in out
