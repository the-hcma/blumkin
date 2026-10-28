"""Tab completion of values: waiting items, loop triggers, task templates (issue #538)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.loop.agent_task import agent_task, pending_items
from blumkin.loop.mail_intake import intake_dir
from blumkin.loop.types import RunContext, TriggerKind, TriggerSpec

_MESSAGE = "AAMkAGI2-message-one"
_TEMPLATE = (
    "# Weekly status report\n"
    '**Trigger:** "weekly"\n'
    "**Input:** x\n"
    "**Output:** y\n\n"
    "**Prompt:**\n\n"
    "> Do it.\n"
)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_PROFILE", raising=False)
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                "[profiles.default]",
                'client_id = "abc-123"',
                "",
                "[loop.triggers.requests]",
                'kind = "loop"',
                'task = "agent"',
                'instance = "requests"',
                'allowlist = ["me@example.com"]',
                'attachment_types = [".jpg"]',
                "",
                "[loop.triggers.requests.templates]",
                'report = "weekly-report"',
                "",
                "[cron.eod]",
                'task = "time-sheet"',
                'instance = "current"',
                'schedule = "0 18 * * 1-5"',
                'timezone = "UTC"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "weekly-report.md").write_text(_TEMPLATE, encoding="utf-8")
    (tmp_path / "tasks" / "daily-digest.md").write_text(_TEMPLATE, encoding="utf-8")
    (tmp_path / "tasks" / "broken-note.md").write_text("no prompt here\n", encoding="utf-8")
    return tmp_path


def _complete(words: str) -> list[str]:
    result = CliRunner().invoke(
        main,
        [],
        env={
            "_BLUMKIN_COMPLETE": "bash_complete",
            "COMP_WORDS": words,
            "COMP_CWORD": str(len(words.split()) - (0 if words.endswith(" ") else 1)),
        },
        prog_name="blumkin",
    )
    assert result.exit_code == 0, result.output
    return [line.split(",", 1)[-1] for line in result.output.splitlines() if line]


def _queue(config: Path) -> str:
    trigger = TriggerSpec(
        allowlist=("me@example.com",),
        attachment_types=(".jpg",),
        instance="requests",
        kind=TriggerKind.LOOP,
        name="requests",
        task="agent",
        templates=(("report", "weekly-report"),),
    )
    directory = intake_dir(config, trigger="requests", message_id=_MESSAGE)
    (directory / "scan.jpg").write_bytes(b"x")
    agent_task(
        trigger,
        context=RunContext(
            config_dir=config,
            instance="requests",
            message_id=_MESSAGE,
            profile="personal",
            subject="Report's ready",
        ),
    )
    (item,) = pending_items(config)
    return item.id


def test_the_loop_subcommands_complete_including_agent(config: Path) -> None:
    commands = _complete("blumkin loop ")
    for name in ("agent", "finish", "pending"):
        assert name in commands


def test_loop_agent_completes_its_flag(config: Path) -> None:
    assert "--json" in _complete("blumkin loop agent --")


def test_loop_finish_completes_waiting_item_ids(config: Path) -> None:
    item = _queue(config)
    assert item in _complete("blumkin loop finish ")


def test_loop_finish_completes_only_matching_prefixes(config: Path) -> None:
    item = _queue(config)
    assert _complete(f"blumkin loop finish {item[:3]}") == [item]
    assert _complete("blumkin loop finish zzz") == []


@pytest.mark.parametrize("command", ["pause", "resume", "run-once"])
def test_loop_commands_complete_trigger_names(config: Path, command: str) -> None:
    names = _complete(f"blumkin loop {command} ")
    assert {"eod", "requests"} <= set(names)


@pytest.mark.parametrize("command", ["show", "update", "delete"])
def test_task_commands_complete_template_names(config: Path, command: str) -> None:
    names = _complete(f"blumkin tasks {command} --name ")
    assert {"daily-digest", "weekly-report"} <= set(names)


def test_a_template_warning_never_reaches_the_completion_output(config: Path) -> None:
    names = _complete("blumkin tasks show --name ")
    assert "broken-note" in names
    assert all(" " not in name for name in names)


def test_task_names_filter_by_prefix(config: Path) -> None:
    assert _complete("blumkin tasks show --name wee") == ["weekly-report"]


def test_auth_setup_completes_providers(config: Path) -> None:
    assert {"google", "microsoft"} <= set(_complete("blumkin auth setup --provider "))


def test_broken_config_completes_nothing_and_does_not_fail(config: Path) -> None:
    (config / "config.toml").write_text("this is [not toml", encoding="utf-8")
    assert _complete("blumkin loop pause ") == []
    assert _complete("blumkin tasks show --name ") == []
    assert _complete("blumkin loop finish ") == []


def test_no_config_dir_completes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path / "missing"))
    assert _complete("blumkin loop finish ") == []
    assert _complete("blumkin loop resume ") == []


def test_task_completion_follows_the_selected_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_PROFILE", raising=False)
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                "[profiles.personal]",
                'client_id = "abc-123"',
                "",
                "[profiles.work]",
                'client_id = "def-456"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "shared-note.md").write_text(_TEMPLATE, encoding="utf-8")
    (tmp_path / "profiles" / "work" / "tasks").mkdir(parents=True)
    (tmp_path / "profiles" / "work" / "tasks" / "work-only.md").write_text(
        _TEMPLATE, encoding="utf-8"
    )
    work = _complete("blumkin --profile work tasks show --name ")
    personal = _complete("blumkin --profile personal tasks show --name ")
    assert {"shared-note", "work-only"} <= set(work)
    assert "work-only" not in personal
    assert "shared-note" in personal
