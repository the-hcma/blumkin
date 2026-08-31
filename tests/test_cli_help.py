"""Help-text coverage: every command has usable --help, groups carry examples."""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.exit_codes import EXIT_SUCCESS


def _command_paths(command: click.Command, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every invocable path in the CLI tree, groups and leaves alike."""
    here = prefix if prefix else (command.name or "blumkin",)
    paths = [here]
    if isinstance(command, click.Group):
        for name, sub in command.commands.items():
            paths.extend(_command_paths(sub, (*here, name)))
    return paths


ALL_PATHS = _command_paths(main)
# Drop the bare "blumkin" root; --help on it is covered explicitly below.
SUBCOMMAND_PATHS = [p[1:] for p in ALL_PATHS if len(p) > 1]

GROUP_PATHS = [
    ("auth",),
    ("calendar",),
    ("chat",),
    ("mail",),
    ("meeting",),
    ("people",),
    ("profiles",),
    ("skills",),
]


def test_help_tree_has_expected_size() -> None:
    """Guards the walk below against silently shrinking if commands are renamed."""
    assert len(SUBCOMMAND_PATHS) >= 30


@pytest.mark.parametrize("path", SUBCOMMAND_PATHS, ids=lambda p: " ".join(p))
def test_every_command_help_is_clean(path: list[str]) -> None:
    result = CliRunner().invoke(main, [*path, "--help"])
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "Traceback" not in result.output
    # A real one-or-more-sentence description, not just the usage line.
    assert "Options:" in result.output
    body = result.output.split("Options:", 1)[0]
    assert len(body.strip().splitlines()) >= 3


def test_root_help_lists_workflows_and_notes() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == EXIT_SUCCESS
    out = result.output
    assert "Getting started:" in out
    assert "blumkin auth login" in out
    assert "--yes" in out
    assert "Exit codes:" in out
    assert "blumkin COMMAND --help" in out


@pytest.mark.parametrize("group", GROUP_PATHS, ids=lambda p: p[0])
def test_group_help_carries_examples(group: tuple[str, ...]) -> None:
    result = CliRunner().invoke(main, [*group, "--help"])
    assert result.exit_code == EXIT_SUCCESS
    out = result.output
    assert "blumkin " + group[0] in out
    assert "Example" in out or "Common workflows:" in out


@pytest.mark.parametrize(
    ("path", "needle"),
    [
        (["calendar", "create"], 'blumkin calendar create --subject "1:1 sync"'),
        (["calendar", "suggest"], "--duration 45m"),
        (["calendar", "view"], "half-open"),
        (["mail", "draft"], "blumkin mail draft --to sam@example.com"),
        (["mail", "reply"], "send with `mail send-draft"),
        (["mail", "inbox"], "--search"),
        (["chat", "send"], "blumkin chat send --with"),
        (["people", "resolve"], "ambiguous"),
        (["meeting", "transcription"], "--enable --yes"),
    ],
    ids=lambda v: v if isinstance(v, str) else " ".join(v),
)
def test_representative_examples_present(path: list[str], needle: str) -> None:
    result = CliRunner().invoke(main, [*path, "--help"])
    assert result.exit_code == EXIT_SUCCESS
    assert needle in result.output


def test_help_text_uses_ascii_hyphens_only() -> None:
    """Authoring style: no em/en dashes in help output (.cursor/skills/blumkin)."""
    for path in [[], *([p] for p in ("auth", "calendar", "chat", "mail", "meeting"))]:
        result = CliRunner().invoke(main, [*path, "--help"])
        assert "—" not in result.output  # em dash
        assert "–" not in result.output  # en dash
