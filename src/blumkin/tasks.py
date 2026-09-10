"""``tasks/<name>.md`` - named reusable prompt templates.

One markdown file per template under ``<config-dir>/tasks/`` (and the active
profile's ``profiles/<name>/tasks/``, merged on top). blumkin never writes them.
``tasks.list`` / ``tasks.show`` surface the templates; the agent fuzzy-matches
the user's request against each template's ``Trigger:`` line, confirms the pick
with the user, then runs the ``Prompt:`` block itself. blumkin ships no matcher
and never calls a model.

File shape::

    # Weekly report            <- optional; else the filename stem is the title
    **Trigger:** "weekly report", "status update"
    **Input:** the latest thread in the Reports folder
    **Output:** a 5-bullet summary, mailed to the team

    **Prompt:**

    > Summarise the input as exactly five bullets, most important first.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blumkin.config import BlumkinConfig
from blumkin.output import emit_warning, sanitize_terminal


class TaskAmbiguousError(ValueError):
    """``--name`` prefix matched more than one template."""


class TaskConflictError(ValueError):
    """The named template exists in both the config dir and the profile dir with
    different content - reconcile the files."""


class TaskNotFoundError(LookupError):
    """No template matched ``--name``."""


@dataclass(frozen=True, slots=True)
class Task:
    conflict: bool
    input: str
    name: str
    output: str
    prompt: str
    sources: tuple[str, ...]
    title: str
    trigger: str


def format_tasks_list_human(payload: dict[str, Any]) -> list[str]:
    tasks = payload.get("tasks") or []
    if not tasks:
        return ["(no templates - no tasks/ directory, or it is empty)"]
    lines: list[str] = []
    for task in tasks:
        lines.append(sanitize_terminal(f"{task['name']} - {task['title']}"))
        for label in ("trigger", "input", "output"):
            if task.get(label):
                lines.append(sanitize_terminal(f"  {label}: {task[label]}"))
        if task.get("conflict"):
            lines.append(f"  ! conflicting copies across: {', '.join(task.get('sources', []))}")
    return lines


def format_tasks_show_human(payload: dict[str, Any]) -> list[str]:
    task = payload.get("task") or {}
    lines = [sanitize_terminal(f"{task.get('name')} - {task.get('title')}")]
    for label in ("trigger", "input", "output"):
        if task.get(label):
            lines.append(sanitize_terminal(f"{label}: {task[label]}"))
    prompt = task.get("prompt") or ""
    if prompt:
        lines.append("")
        lines.extend(sanitize_terminal(line) for line in prompt.splitlines())
    else:
        lines.append("(no Prompt: block)")
    return lines


def load_tasks(config: BlumkinConfig) -> list[Task]:
    """Every template, config-dir first then the profile dir merged on top."""
    merged: dict[str, Task] = {}
    for directory in _task_dirs(config):
        for path in sorted(directory.glob("*.md")):
            _merge(merged, _parse(path))
    return sorted(merged.values(), key=lambda task: task.name)


async def tasks_list(*, config: BlumkinConfig) -> dict[str, Any]:
    return {
        "ok": True,
        "tasks": [
            {
                "conflict": task.conflict,
                "input": task.input,
                "name": task.name,
                "output": task.output,
                "sources": list(task.sources),
                "title": task.title,
                "trigger": task.trigger,
            }
            for task in load_tasks(config)
        ],
    }


async def tasks_show(*, config: BlumkinConfig, name: str) -> dict[str, Any]:
    task = _select(load_tasks(config), name)
    return {
        "ok": True,
        "task": {
            "conflict": task.conflict,
            "input": task.input,
            "name": task.name,
            "output": task.output,
            "prompt": task.prompt,
            "sources": list(task.sources),
            "title": task.title,
            "trigger": task.trigger,
        },
    }


def _merge(merged: dict[str, Task], parsed: Task) -> None:
    existing = merged.get(parsed.name)
    if existing is None:
        merged[parsed.name] = parsed
        return
    same = (existing.title, existing.trigger, existing.input, existing.output, existing.prompt) == (
        parsed.title,
        parsed.trigger,
        parsed.input,
        parsed.output,
        parsed.prompt,
    )
    merged[parsed.name] = Task(
        conflict=existing.conflict or not same,
        input=parsed.input,
        name=parsed.name,
        output=parsed.output,
        prompt=parsed.prompt,
        sources=(*existing.sources, *parsed.sources),
        title=parsed.title,
        trigger=parsed.trigger,
    )


def _parse(path: Path) -> Task:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        emit_warning(f"could not read {path}: {exc}")
        text = ""
    fields: dict[str, str] = {}
    prompt: list[str] = []
    title = ""
    in_prompt = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if in_prompt:
            if stripped.startswith(">"):
                prompt.append(stripped[1:].removeprefix(" "))
            elif stripped:
                prompt.append(stripped)
            continue
        if not title and stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            continue
        lowered = stripped.lower()
        for label in ("trigger", "input", "output"):
            if lowered.startswith(f"**{label}:**"):
                fields[label] = stripped.split(":**", 1)[1].strip()
                break
        else:
            if lowered.startswith("**prompt:**"):
                in_prompt = True
    name = path.stem
    if not prompt:
        emit_warning(f"{path}: no `**Prompt:**` block")
    return Task(
        conflict=False,
        input=fields.get("input", ""),
        name=name,
        output=fields.get("output", ""),
        prompt="\n".join(prompt).strip(),
        sources=(str(path),),
        title=title or name,
        trigger=fields.get("trigger", ""),
    )


def _select(tasks: list[Task], name: str) -> Task:
    needle = name.strip()
    if not needle:
        raise TaskNotFoundError("--name is required (a template name or a unique prefix)")
    exact = [task for task in tasks if task.name == needle]
    matches = exact or [task for task in tasks if task.name.startswith(needle)]
    if not matches:
        raise TaskNotFoundError(f"no task template named {needle!r}; try `blumkin tasks list`")
    if len(matches) > 1:
        raise TaskAmbiguousError(
            f"{needle!r} matches {len(matches)} templates: {', '.join(t.name for t in matches)}"
        )
    task = matches[0]
    if task.conflict:
        raise TaskConflictError(
            f"template {task.name!r} has different content across {', '.join(task.sources)} "
            "- remove or reconcile one copy"
        )
    return task


def _task_dirs(config: BlumkinConfig) -> list[Path]:
    # Mirrors blumkin.contacts.locate_operator_files: config dir first (the base),
    # then the active profile's dir merged on top.
    dirs: list[Path] = []
    for base in (config.config_dir, config.profile_dir):
        directory = base / "tasks"
        if directory.is_dir() and directory not in dirs:
            dirs.append(directory)
    return dirs
