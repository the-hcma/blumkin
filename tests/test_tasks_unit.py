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
from blumkin.skills.errors import ConsentRequiredError, classify_exception
from blumkin.tasks import (
    TaskAmbiguousError,
    TaskConflictError,
    TaskExistsError,
    TaskNotFoundError,
    TaskPromptError,
    TaskValidationError,
    format_tasks_create_human,
    format_tasks_delete_human,
    format_tasks_list_human,
    format_tasks_show_human,
    format_tasks_update_human,
    load_tasks,
    tasks_create,
    tasks_delete,
    tasks_list,
    tasks_show,
    tasks_update,
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
    assert listed["tasks"][0]["problems"] == ["missing_prompt"]
    assert "no `**Prompt:**` blockquote" in capsys.readouterr().err


def test_prompt_header_with_no_blockquote_warns(tmp_path, monkeypatch, capsys) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    (tmp_path / "tasks" / "wk.md").write_text(
        "**Prompt:**\n\nplain prose, no blockquote\n", encoding="utf-8"
    )
    with pytest.raises(TaskPromptError):
        asyncio.run(tasks_show(config=cfg, name="wk"))
    assert "no `**Prompt:**` blockquote" in capsys.readouterr().err


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
    assert classify_exception(TaskExistsError("x")).exit_code == EXIT_USAGE
    # operator text ("Missing", "client_id" in a template name / path) must not
    # be re-read as auth_required by the ValueError message heuristics.
    for msg in ("'Missing' matches 2 templates: Missing-a, Missing-b", "client_id.md vs x.md"):
        assert classify_exception(TaskAmbiguousError(msg)).slug == "usage_error"
        assert classify_exception(TaskConflictError(msg)).slug == "usage_error"


def test_create_round_trips_via_load_tasks(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    payload = asyncio.run(
        tasks_create(
            config=cfg,
            name="weekly-report",
            prompt="Summarise in five bullets.\n\nKeep each short.",
            title="Weekly status report",
            trigger='"weekly report"',
            input="the Reports folder",
            output="five bullets",
        )
    )
    assert payload["ok"] is True
    assert payload["overwrote"] is False
    assert Path(payload["path"]) == tmp_path / "tasks" / "weekly-report.md"
    (task,) = load_tasks(cfg)
    assert task.name == "weekly-report"
    assert task.title == "Weekly status report"
    assert task.trigger == '"weekly report"'
    assert task.input == "the Reports folder"
    assert task.output == "five bullets"
    assert task.prompt == "Summarise in five bullets.\n\nKeep each short."


def test_create_refuses_overwrite_without_force(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    asyncio.run(tasks_create(config=cfg, name="wk", prompt="first"))
    with pytest.raises(TaskExistsError):
        asyncio.run(tasks_create(config=cfg, name="wk", prompt="second"))
    payload = asyncio.run(tasks_create(config=cfg, name="wk", prompt="second", force=True))
    assert payload["overwrote"] is True
    assert asyncio.run(tasks_show(config=cfg, name="wk"))["task"]["prompt"] == "second"


def test_create_rejects_bad_names_and_multiline_fields(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    for bad in ("Weekly", "a/b", "../x", "has space", ""):
        with pytest.raises(ValueError):
            asyncio.run(tasks_create(config=cfg, name=bad, prompt="body"))
    with pytest.raises(ValueError, match="single line"):
        asyncio.run(tasks_create(config=cfg, name="wk", prompt="body", title="a\nb"))
    with pytest.raises(ValueError, match="--prompt"):
        asyncio.run(tasks_create(config=cfg, name="wk", prompt="   "))


def test_create_uses_profile_tasks_when_that_directory_exists(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    prof = tmp_path / "profiles" / "work" / "tasks"
    prof.mkdir(parents=True)
    payload = asyncio.run(tasks_create(config=cfg, name="wk", prompt="body"))
    assert Path(payload["path"]) == prof / "wk.md"
    assert not (tmp_path / "tasks" / "wk.md").exists()


def test_create_refuses_same_name_in_the_other_tasks_dir(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    (tmp_path / "tasks" / "wk.md").write_text("# Wk\n\n**Prompt:**\n\n> body\n", encoding="utf-8")
    prof = tmp_path / "profiles" / "work" / "tasks"
    prof.mkdir(parents=True)
    with pytest.raises(TaskExistsError, match="already exists"):
        asyncio.run(tasks_create(config=cfg, name="wk", prompt="other", force=True))
    assert not (prof / "wk.md").exists()


def test_cli_tasks_create_exit_codes(tmp_path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    runner = CliRunner()
    created = runner.invoke(
        main,
        ["tasks", "create", "--name", "wk", "--prompt", "do the thing", "--json"],
        obj={},
    )
    assert created.exit_code == 0, created.output
    assert json.loads(created.stdout)["task"]["prompt"] == "do the thing"
    again = runner.invoke(
        main,
        ["tasks", "create", "--name", "wk", "--prompt", "other", "--json"],
        obj={},
    )
    assert again.exit_code == EXIT_USAGE
    bad = runner.invoke(
        main,
        ["tasks", "create", "--name", "Bad Name", "--prompt", "x", "--json"],
        obj={},
    )
    assert bad.exit_code == EXIT_USAGE


def test_human_create_formatter() -> None:
    assert format_tasks_create_human({"path": "/t/a.md", "overwrote": False}) == ["created /t/a.md"]
    assert format_tasks_create_human({"path": "/t/a.md", "overwrote": True}) == [
        "overwrote /t/a.md"
    ]


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


def test_update_preserves_unspecified_fields(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    asyncio.run(
        tasks_create(
            config=cfg,
            name="weekly-report",
            prompt="five bullets",
            title="Weekly",
            trigger="wk",
            input="Reports",
            output="mail",
        )
    )
    payload = asyncio.run(tasks_update(config=cfg, name="weekly-report", title="Weekly status"))
    task = payload["task"]
    assert task["title"] == "Weekly status"
    assert task["trigger"] == "wk"
    assert task["input"] == "Reports"
    assert task["output"] == "mail"
    assert task["prompt"] == "five bullets"


def test_update_and_delete_refuse_conflict_and_missing(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    (tmp_path / "tasks" / "wk.md").write_text("# A\n**Prompt:**\n\n> body\n", encoding="utf-8")
    prof = tmp_path / "profiles" / "work" / "tasks"
    prof.mkdir(parents=True)
    (prof / "wk.md").write_text("# B\n**Prompt:**\n\n> body\n", encoding="utf-8")
    with pytest.raises(TaskConflictError):
        asyncio.run(tasks_update(config=cfg, name="wk", title="C"))
    with pytest.raises(TaskConflictError):
        asyncio.run(tasks_delete(config=cfg, name="wk", yes=True))
    with pytest.raises(TaskNotFoundError):
        asyncio.run(tasks_update(config=cfg, name="nope", title="x"))
    with pytest.raises(TaskNotFoundError):
        asyncio.run(tasks_delete(config=cfg, name="nope", yes=True))


def test_delete_requires_yes_unless_dry_run(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    asyncio.run(tasks_create(config=cfg, name="wk", prompt="body"))
    dry = asyncio.run(tasks_delete(config=cfg, name="wk", dry_run=True))
    assert dry["dry_run"] is True
    assert (tmp_path / "tasks" / "wk.md").is_file()
    with pytest.raises(ConsentRequiredError):
        asyncio.run(tasks_delete(config=cfg, name="wk"))
    removed = asyncio.run(tasks_delete(config=cfg, name="wk", yes=True))
    assert removed["dry_run"] is False
    assert not (tmp_path / "tasks" / "wk.md").exists()


def test_cli_tasks_update_and_delete_exit_codes(tmp_path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    runner = CliRunner()
    assert (
        runner.invoke(
            main,
            ["tasks", "create", "--name", "wk", "--prompt", "body", "--json"],
            obj={},
        ).exit_code
        == 0
    )
    updated = runner.invoke(
        main,
        ["tasks", "update", "--name", "wk", "--title", "Weekly", "--json"],
        obj={},
    )
    assert updated.exit_code == 0, updated.output
    assert json.loads(updated.stdout)["task"]["title"] == "Weekly"
    no_yes = runner.invoke(main, ["tasks", "delete", "--name", "wk", "--json"], obj={})
    assert no_yes.exit_code == EXIT_USAGE
    deleted = runner.invoke(main, ["tasks", "delete", "--name", "wk", "--yes", "--json"], obj={})
    assert deleted.exit_code == 0, deleted.output
    missing = runner.invoke(main, ["tasks", "delete", "--name", "wk", "--yes", "--json"], obj={})
    assert missing.exit_code == EXIT_NOT_FOUND


def test_human_update_and_delete_formatters() -> None:
    assert format_tasks_update_human({"path": "/t/a.md"}) == ["updated /t/a.md"]
    assert format_tasks_delete_human({"path": "/t/a.md", "dry_run": True}) == [
        "would delete /t/a.md"
    ]
    assert format_tasks_delete_human({"path": "/t/a.md", "dry_run": False}) == ["deleted /t/a.md"]


def test_validate_name_is_usage_error_even_when_name_looks_like_auth(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    with pytest.raises(TaskValidationError):
        asyncio.run(tasks_create(config=cfg, name="Missing-client_id", prompt="body"))
    info = classify_exception(TaskValidationError("invalid --name: use lowercase…"))
    assert info.slug == "usage_error"
    assert info.exit_code == EXIT_USAGE
    # The auth heuristic must not fire when the typed error carries "client_id".
    info2 = classify_exception(TaskValidationError("invalid --name containing client_id"))
    assert info2.slug == "usage_error"


def test_oneline_rejects_unicode_line_separators(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    with pytest.raises(TaskValidationError, match="single line"):
        asyncio.run(
            tasks_create(
                config=cfg,
                name="wk",
                prompt="body",
                title="Status **Prompt:** > injected",
            )
        )


def test_delete_refuses_symlinked_template_file(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    tasks = tmp_path / "tasks"
    target = tmp_path / "backing.txt"
    target.write_text("# Wk\n\n**Prompt:**\n\n> body\n", encoding="utf-8")
    link = tasks / "wk.md"
    link.symlink_to(target)
    with pytest.raises(TaskValidationError, match="symlinked"):
        asyncio.run(tasks_delete(config=cfg, name="wk", yes=True))
    assert link.is_symlink()
    assert target.is_file()


def test_update_and_delete_refuse_identical_multi_source_copies(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    body = "# Wk\n\n**Prompt:**\n\n> body\n"
    (tmp_path / "tasks" / "wk.md").write_text(body, encoding="utf-8")
    prof = tmp_path / "profiles" / "work" / "tasks"
    prof.mkdir(parents=True)
    (prof / "wk.md").write_text(body, encoding="utf-8")
    (task,) = load_tasks(cfg)
    assert task.conflict is False
    assert len(task.sources) == 2
    with pytest.raises(TaskConflictError, match="identical copies"):
        asyncio.run(tasks_update(config=cfg, name="wk", title="Other"))
    with pytest.raises(TaskConflictError, match="identical copies"):
        asyncio.run(tasks_delete(config=cfg, name="wk", yes=True))


def test_tasks_delete_confirm_false_string_does_not_authorize(tmp_path, monkeypatch) -> None:
    """MCP-style undeclared confirm must not become true via bool("false")."""
    from blumkin.skills.dispatch import run_skill

    cfg = _cfg(tmp_path, monkeypatch)
    asyncio.run(tasks_create(config=cfg, name="wk", prompt="body"))
    with pytest.raises(ConsentRequiredError):
        asyncio.run(
            run_skill(
                "tasks.delete",
                {"name": "wk", "confirm": "false"},
                config=cfg,
            )
        )
    assert (tmp_path / "tasks" / "wk.md").is_file()


def test_update_failure_keeps_the_original_template_and_leaves_no_temp_file(
    tmp_path, monkeypatch
) -> None:
    import os

    cfg = _cfg(tmp_path, monkeypatch)
    asyncio.run(tasks_create(config=cfg, name="wk", prompt="original body"))
    path = tmp_path / "tasks" / "wk.md"
    before = path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(tasks_update(config=cfg, name="wk", prompt="new body"))
    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in (tmp_path / "tasks").iterdir()] == ["wk.md"]


def test_create_force_failure_keeps_the_original_template(tmp_path, monkeypatch) -> None:
    import os

    cfg = _cfg(tmp_path, monkeypatch)
    asyncio.run(tasks_create(config=cfg, name="wk", prompt="original body"))
    path = tmp_path / "tasks" / "wk.md"
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError, match="full"):
        asyncio.run(tasks_create(config=cfg, name="wk", prompt="new", force=True))
    assert path.read_text(encoding="utf-8") == before


def test_update_refuses_to_drop_text_it_does_not_model(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    body = (
        "# Wk\n**Trigger:** wk\n\n**Prompt:**\n\n> body\n\n"
        "## Examples\n\nSome hand-written notes.\n"
    )
    path = tmp_path / "tasks" / "wk.md"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(TaskValidationError, match="Expected"):
        asyncio.run(tasks_update(config=cfg, name="wk", trigger="new"))
    assert path.read_text(encoding="utf-8") == body


def test_update_accepts_a_hand_written_template_that_uses_only_modeled_fields(
    tmp_path, monkeypatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    path = tmp_path / "tasks" / "wk.md"
    path.write_text(
        "#  Weekly  \n**trigger:**   wk  \n**Input:** reports\n\n\n"
        "**Prompt:**\n>body\n>  indented\n",
        encoding="utf-8",
    )
    payload = asyncio.run(tasks_update(config=cfg, name="wk", trigger="weekly"))
    assert payload["task"]["trigger"] == "weekly"
    assert payload["task"]["input"] == "reports"
    assert payload["task"]["prompt"] == "body\n indented"


def test_update_keeps_a_title_that_equals_the_file_name(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    path = tmp_path / "tasks" / "wk.md"
    path.write_text("# wk\n\n**Prompt:**\n\n> body\n", encoding="utf-8")
    asyncio.run(tasks_update(config=cfg, name="wk", trigger="t"))
    assert path.read_text(encoding="utf-8").startswith("# wk\n")


def test_create_force_works_when_the_tasks_directory_is_a_symlink(tmp_path, monkeypatch) -> None:
    real = tmp_path / "dotfiles-tasks"
    real.mkdir()
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "tasks").symlink_to(real)
    (cfg_dir / "config.toml").write_text(
        '[profiles.work]\nprovider = "microsoft"\ntenant_id = "x"\nclient_id = "y"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("BLUMKIN_PROFILE", "work")
    cfg = load_config(profile="work")
    asyncio.run(tasks_create(config=cfg, name="wk", prompt="one"))
    payload = asyncio.run(tasks_create(config=cfg, name="wk", prompt="two", force=True))
    assert payload["overwrote"] is True
    assert payload["task"]["prompt"] == "two"


def test_created_files_and_a_new_tasks_directory_are_private(tmp_path, monkeypatch) -> None:
    import stat

    cfg = _cfg(tmp_path, monkeypatch)
    (tmp_path / "tasks").rmdir()
    asyncio.run(tasks_create(config=cfg, name="wk", prompt="body"))
    assert stat.S_IMODE((tmp_path / "tasks").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "tasks" / "wk.md").stat().st_mode) == 0o600


def test_updating_keeps_the_file_private(tmp_path, monkeypatch) -> None:
    import stat

    cfg = _cfg(tmp_path, monkeypatch)
    asyncio.run(tasks_create(config=cfg, name="wk", prompt="body"))
    asyncio.run(tasks_update(config=cfg, name="wk", trigger="t"))
    assert stat.S_IMODE((tmp_path / "tasks" / "wk.md").stat().st_mode) == 0o600


def test_delete_needs_the_exact_name_but_a_dry_run_may_use_a_prefix(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    asyncio.run(tasks_create(config=cfg, name="deploy-prod", prompt="body"))
    preview = asyncio.run(tasks_delete(config=cfg, name="dep", dry_run=True))
    assert preview["name"] == "deploy-prod"
    with pytest.raises(TaskValidationError, match="exact"):
        asyncio.run(tasks_delete(config=cfg, name="dep", yes=True))
    assert (tmp_path / "tasks" / "deploy-prod.md").is_file()
    done = asyncio.run(tasks_delete(config=cfg, name="deploy-prod", yes=True))
    assert done["dry_run"] is False
    assert not (tmp_path / "tasks" / "deploy-prod.md").exists()


def test_a_name_longer_than_a_file_name_allows_is_a_usage_error(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    with pytest.raises(TaskValidationError, match="Expected"):
        asyncio.run(tasks_create(config=cfg, name="a" * 300, prompt="body"))


def test_create_force_refuses_when_identical_copies_exist_in_both_directories(
    tmp_path, monkeypatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    body = "# Wk\n\n**Prompt:**\n\n> body\n"
    config_copy = tmp_path / "tasks" / "wk.md"
    config_copy.write_text(body, encoding="utf-8")
    profile_tasks = tmp_path / "profiles" / "work" / "tasks"
    profile_tasks.mkdir(parents=True)
    profile_copy = profile_tasks / "wk.md"
    profile_copy.write_text(body, encoding="utf-8")
    with pytest.raises(TaskExistsError, match="remove or rename"):
        asyncio.run(tasks_create(config=cfg, name="wk", prompt="other", force=True))
    assert config_copy.read_text(encoding="utf-8") == body
    assert profile_copy.read_text(encoding="utf-8") == body


def test_update_refuses_a_template_that_is_not_valid_utf8(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    path = tmp_path / "tasks" / "wk.md"
    original = b"# Wk\n\n**Prompt:**\n\n> caf\xe9 body\n"
    path.write_bytes(original)
    with pytest.raises(TaskValidationError, match="UTF-8"):
        asyncio.run(tasks_update(config=cfg, name="wk", trigger="t"))
    assert path.read_bytes() == original


# --------------------------------------------------------------------------- unusable prompt (#615)

_HAND_WRITTEN = """\
# Mileage claim

## Workflow
1. Open the expense form.
2. Add one item per trip.

## Site map
- Overview -> Create Report

Safety: never press Submit.
"""


def _write_tasks(tmp_path: Path, **files: str) -> None:
    for name, text in files.items():
        (tmp_path / "tasks" / f"{name}.md").write_text(text, encoding="utf-8")


def test_showing_a_task_with_no_prompt_block_is_an_error_not_an_empty_success(
    tmp_path, monkeypatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    _write_tasks(tmp_path, mileage=_HAND_WRITTEN)

    with pytest.raises(TaskPromptError) as caught:
        asyncio.run(tasks_show(config=cfg, name="mileage"))

    message = str(caught.value)
    assert str(tmp_path / "tasks" / "mileage.md") in message
    assert "Expected template 'mileage'" in message and "got missing_prompt" in message
    assert "is not read" in message  # same-line text and `**Prompt**:` are called out


def test_a_prompt_header_followed_by_an_empty_blockquote_is_unusable_too(
    tmp_path, monkeypatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    _write_tasks(tmp_path, blank="**Prompt:**\n\n>\n>\n")

    with pytest.raises(TaskPromptError, match="empty_prompt"):
        asyncio.run(tasks_show(config=cfg, name="blank"))
    (entry,) = asyncio.run(tasks_list(config=cfg))["tasks"]
    assert entry["problems"] == ["empty_prompt"]


def test_the_unusable_prompt_error_is_a_usage_error_with_a_structured_exit(
    tmp_path, monkeypatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    _write_tasks(tmp_path, mileage=_HAND_WRITTEN)

    info = classify_exception(TaskPromptError("x"))

    assert (info.slug, info.exit_code) == ("usage_error", EXIT_USAGE)
    result = CliRunner().invoke(main, ["tasks", "show", "--name", "mileage", "--json"], obj={})
    assert result.exit_code == EXIT_USAGE
    # The stderr warning about the file comes first; the error envelope is the JSON line.
    payload = json.loads(next(line for line in result.output.splitlines() if line.startswith("{")))
    assert payload["error"] == "usage_error"
    assert "mileage.md" in payload["message"] and "got missing_prompt" in payload["message"]
    del cfg


def test_listing_keeps_working_when_one_task_is_malformed(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    _write_tasks(tmp_path, mileage=_HAND_WRITTEN, **{"weekly-report": _REPORT})

    tasks = {t["name"]: t for t in asyncio.run(tasks_list(config=cfg))["tasks"]}

    assert set(tasks) == {"mileage", "weekly-report"}
    assert tasks["mileage"]["problems"] == ["missing_prompt"]
    assert tasks["weekly-report"]["problems"] == []
    assert asyncio.run(tasks_show(config=cfg, name="weekly-report"))["ok"] is True


def test_the_human_listing_flags_a_task_with_a_problem() -> None:
    lines = format_tasks_list_human(
        {
            "tasks": [
                {"name": "mileage", "title": "Mileage", "problems": ["missing_prompt"]},
                {"name": "ok", "title": "Fine", "problems": []},
            ]
        }
    )

    assert any(
        line.startswith("  ! no usable prompt") and "mileage" in " ".join(lines) for line in lines
    )
    assert not any("ok" in line and "!" in line for line in lines)


def test_showing_or_updating_a_hand_written_task_never_rewrites_the_file(
    tmp_path, monkeypatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    _write_tasks(tmp_path, mileage=_HAND_WRITTEN)
    path = tmp_path / "tasks" / "mileage.md"
    path.chmod(0o640)
    before = (path.read_bytes(), path.stat().st_mode)

    with pytest.raises(TaskPromptError):
        asyncio.run(tasks_show(config=cfg, name="mileage"))
    with pytest.raises(TaskValidationError):
        asyncio.run(tasks_update(config=cfg, name="mileage", prompt="Do the thing"))

    assert (path.read_bytes(), path.stat().st_mode) == before


@pytest.mark.parametrize(
    ("text", "prompt", "problems"),
    [
        ("**Prompt:**\r\n\r\n> Do it.\r\n> Twice.\r\n", "Do it.\nTwice.", []),
        ("\ufeff**Prompt:**\n\n> Do it.\n", "Do it.", []),
        ("**Prompt:**\n>\n> Do it.\n", "Do it.", []),
        ("**Prompt:**\n\n>   \n>\n", "", ["empty_prompt"]),
        ("**Prompt:**\n\n> Run:\n> ```\n> ls\n> ```\n", "Run:\n```\nls\n```", []),
        ("**Prompt:**\n\n> Outer\n> > nested\n", "Outer\n> nested", []),
        ("**Prompt:** do the thing on the same line\n", "", ["missing_prompt"]),
        ("**Prompt**: colon outside the bold\n\n> text\n", "", ["missing_prompt"]),
    ],
)
def test_prompt_parsing_edge_cases_are_flagged_or_accepted_as_documented(
    tmp_path, monkeypatch, text: str, prompt: str, problems: list[str]
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    (tmp_path / "tasks" / "t.md").write_bytes(text.encode("utf-8"))

    (entry,) = asyncio.run(tasks_list(config=cfg))["tasks"]

    assert entry["problems"] == problems
    if not problems:
        assert asyncio.run(tasks_show(config=cfg, name="t"))["task"]["prompt"] == prompt


def test_updating_a_hand_written_file_without_a_prompt_explains_why_it_is_refused(
    tmp_path, monkeypatch
) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    _write_tasks(tmp_path, mileage=_HAND_WRITTEN)

    with pytest.raises(TaskValidationError, match="usable prompt before it is updated"):
        asyncio.run(tasks_update(config=cfg, name="mileage", prompt="Do the thing"))
