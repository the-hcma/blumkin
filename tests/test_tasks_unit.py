"""Unit coverage for ``blumkin.tasks`` - ``tasks/<name>.md`` (#209)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.config import load_config
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_USAGE
from blumkin.skills.errors import classify_exception
from blumkin.tasks import (
    TaskAmbiguousError,
    TaskConflictError,
    TaskNotFoundError,
    format_tasks_list_human,
    format_tasks_show_human,
    load_tasks,
    tasks_list,
    tasks_show,
)

_REPORT = """\
# Weekly status report
**Trigger:** "weekly report", "status update"
**Input:** the latest thread in Reports
**Output:** five bullets, mailed to the team

**Prompt:**

> Summarise the input as exactly five bullets.
>
> Keep each under 20 words.
"""


def _cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, profile: str = "work"):
    (tmp_path / "config.toml").write_text(
        '[profiles.work]\nprovider = "microsoft"\ntenant_id = "x"\nclient_id = "y"\n'
        '[profiles.personal]\nprovider = "google"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BLUMKIN_PROFILE", profile)
    (tmp_path / "tasks").mkdir()
    return load_config(profile=profile)


def test_parse_fields_title_and_prompt_blockquote(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    (tmp_path / "tasks" / "weekly-report.md").write_text(_REPORT, encoding="utf-8")
    (task,) = load_tasks(cfg)
    assert task.name == "weekly-report"
    assert task.title == "Weekly status report"
    assert task.trigger == '"weekly report", "status update"'
    assert task.output == "five bullets, mailed to the team"
    assert task.prompt == (
        "Summarise the input as exactly five bullets.\n\nKeep each under 20 words."
    )


def test_title_defaults_to_the_filename_stem(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    (tmp_path / "tasks" / "weekly-report.md").write_text(
        "**Trigger:** wk\n\n**Prompt:**\n\n> body\n", encoding="utf-8"
    )
    assert asyncio.run(tasks_list(config=cfg))["tasks"][0]["title"] == "weekly-report"


def test_a_utf8_bom_first_line_is_not_eaten(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    (tmp_path / "tasks" / "wk.md").write_bytes(
        "# Weekly report\n**Prompt:**\n\n> body\n".encode("utf-8-sig")
    )
    (task,) = asyncio.run(tasks_list(config=cfg))["tasks"]
    assert task["title"] == "Weekly report"


def test_list_omits_prompt_show_includes_it(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    (tmp_path / "tasks" / "weekly-report.md").write_text(_REPORT, encoding="utf-8")
    listed = asyncio.run(tasks_list(config=cfg))
    assert "prompt" not in listed["tasks"][0]
    shown = asyncio.run(tasks_show(config=cfg, name="weekly-report"))
    assert shown["task"]["prompt"].startswith("Summarise")


def test_show_name_matching_exact_prefix_ambiguous_missing(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    for stem in ("weekly-report", "weekly-digest", "onboarding"):
        (tmp_path / "tasks" / f"{stem}.md").write_text(
            "**Prompt:**\n\n> do the thing\n", encoding="utf-8"
        )
    assert asyncio.run(tasks_show(config=cfg, name="onboarding"))["task"]["name"] == "onboarding"
    assert asyncio.run(tasks_show(config=cfg, name="onb"))["task"]["name"] == "onboarding"
    with pytest.raises(TaskAmbiguousError):
        asyncio.run(tasks_show(config=cfg, name="weekly"))
    with pytest.raises(TaskNotFoundError):
        asyncio.run(tasks_show(config=cfg, name="nope"))
    for empty in ("", "   "):
        with pytest.raises(TaskNotFoundError):
            asyncio.run(tasks_show(config=cfg, name=empty))


def test_an_exact_name_wins_over_a_longer_one_it_prefixes(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    for stem in ("weekly", "weekly-report"):
        (tmp_path / "tasks" / f"{stem}.md").write_text(
            f"**Prompt:**\n\n> {stem}\n", encoding="utf-8"
        )
    assert asyncio.run(tasks_show(config=cfg, name="weekly"))["task"]["name"] == "weekly"


def test_prompt_stops_at_the_first_non_blockquote_line(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    (tmp_path / "tasks" / "wk.md").write_text(
        "**Prompt:**\n\n> do the thing\n>\n> and this\n\nNotes: not part of the prompt.\n",
        encoding="utf-8",
    )
    assert asyncio.run(tasks_show(config=cfg, name="wk"))["task"]["prompt"] == (
        "do the thing\n\nand this"
    )


def test_missing_prompt_block_warns_but_still_lists(tmp_path, monkeypatch, capsys) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    (tmp_path / "tasks" / "broken.md").write_text("**Trigger:** x\n", encoding="utf-8")
    listed = asyncio.run(tasks_list(config=cfg))
    assert [t["name"] for t in listed["tasks"]] == ["broken"]
    assert asyncio.run(tasks_show(config=cfg, name="broken"))["task"]["prompt"] == ""
    assert "no `**Prompt:**` block" in capsys.readouterr().err


def test_missing_tasks_dir_is_empty_not_an_error(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    (tmp_path / "tasks").rmdir()
    assert asyncio.run(tasks_list(config=cfg)) == {"ok": True, "tasks": []}


def test_profile_dir_merges_and_conflicting_copy_fails_closed(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    (tmp_path / "tasks" / "weekly-report.md").write_text(_REPORT, encoding="utf-8")
    (tmp_path / "tasks" / "shared.md").write_text("**Prompt:**\n\n> base\n", encoding="utf-8")
    prof = tmp_path / "profiles" / "work" / "tasks"
    prof.mkdir(parents=True)
    (prof / "weekly-report.md").write_text("**Prompt:**\n\n> DIFFERENT\n", encoding="utf-8")
    (prof / "shared.md").write_text("**Prompt:**\n\n> base\n", encoding="utf-8")
    by_name = {t["name"]: t for t in asyncio.run(tasks_list(config=cfg))["tasks"]}
    assert by_name["weekly-report"]["conflict"] is True
    assert len(by_name["weekly-report"]["sources"]) == 2
    assert by_name["shared"]["conflict"] is False
    with pytest.raises(TaskConflictError):
        asyncio.run(tasks_show(config=cfg, name="weekly-report"))
    assert asyncio.run(tasks_show(config=cfg, name="shared"))["task"]["prompt"] == "base"


def test_a_title_only_difference_is_a_conflict(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    (tmp_path / "tasks" / "wk.md").write_text(
        "# Weekly status report\n**Prompt:**\n\n> body\n", encoding="utf-8"
    )
    prof = tmp_path / "profiles" / "work" / "tasks"
    prof.mkdir(parents=True)
    (prof / "wk.md").write_text("# Weekly\n**Prompt:**\n\n> body\n", encoding="utf-8")
    (task,) = asyncio.run(tasks_list(config=cfg))["tasks"]
    assert task["conflict"] is True
    with pytest.raises(TaskConflictError):
        asyncio.run(tasks_show(config=cfg, name="wk"))


def test_errors_classify_to_the_documented_exit_codes() -> None:
    assert classify_exception(TaskNotFoundError("x")).exit_code == EXIT_NOT_FOUND
    assert classify_exception(TaskAmbiguousError("x")).exit_code == EXIT_USAGE
    assert classify_exception(TaskConflictError("x")).exit_code == EXIT_USAGE
    # operator text ("Missing", "client_id" in a template name / path) must not
    # be re-read as auth_required by the ValueError message heuristics.
    for msg in ("'Missing' matches 2 templates: Missing-a, Missing-b", "client_id.md vs x.md"):
        assert classify_exception(TaskAmbiguousError(msg)).slug == "usage_error"
        assert classify_exception(TaskConflictError(msg)).slug == "usage_error"


def test_cli_tasks_runs_with_no_auth_or_provider(tmp_path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    (tmp_path / "tasks" / "weekly-report.md").write_text(_REPORT, encoding="utf-8")
    runner = CliRunner()
    listed = runner.invoke(main, ["tasks", "list", "--json"], obj={})
    assert listed.exit_code == 0, listed.output
    assert [t["name"] for t in json.loads(listed.stdout)["tasks"]] == ["weekly-report"]
    shown = runner.invoke(main, ["tasks", "show", "--name", "weekly", "--json"], obj={})
    assert shown.exit_code == 0
    assert json.loads(shown.stdout)["task"]["prompt"].startswith("Summarise")
    missing = runner.invoke(main, ["tasks", "show", "--name", "nope", "--json"], obj={})
    assert missing.exit_code == EXIT_NOT_FOUND
    no_name = runner.invoke(main, ["tasks", "show", "--json"], obj={})
    assert no_name.exit_code == EXIT_USAGE


def test_human_list_formatter() -> None:
    lines = format_tasks_list_human(
        {
            "tasks": [
                {"name": "wr", "title": "Weekly", "trigger": "t", "input": "", "output": "o"},
                {"name": "x", "title": "X", "conflict": True, "sources": ["/a/x.md", "/b/x.md"]},
            ]
        }
    )
    assert lines[0] == "wr - Weekly"
    assert "  trigger: t" in lines
    assert "  input:" not in "\n".join(lines)
    assert "  ! conflicting copies across: /a/x.md, /b/x.md" in lines
    assert format_tasks_list_human({"tasks": []})[0].startswith("(no templates")


def test_human_show_formatter() -> None:
    lines = format_tasks_show_human({"task": {"name": "wr", "title": "Weekly", "prompt": "A\n\nB"}})
    assert lines == ["wr - Weekly", "", "A", "", "B"]
    assert format_tasks_show_human({"task": {"name": "wr", "title": "W", "prompt": ""}})[-1] == (
        "(no Prompt: block)"
    )
