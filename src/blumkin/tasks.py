"""``tasks/<name>.md`` - named reusable prompt templates.

One markdown file per template under ``<config-dir>/tasks/`` (and the active
profile's ``profiles/<name>/tasks/``, merged on top). ``tasks.create`` writes a
new file; ``tasks.list`` / ``tasks.show`` surface the templates. The agent
fuzzy-matches the user's request against each template's ``Trigger:`` line,
confirms the pick with the user, then runs the ``Prompt:`` block itself.
blumkin ships no matcher and never calls a model.

File shape::

    # Weekly report            <- optional; else the filename stem is the title
    **Trigger:** "weekly report", "status update"
    **Input:** the latest thread in the Reports folder
    **Output:** a 5-bullet summary, mailed to the team

    **Prompt:**

    > Summarise the input as exactly five bullets, most important first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blumkin.config import BlumkinConfig
from blumkin.output import emit_warning, sanitize_terminal

_NAME_RE = re.compile(r"^[a-z0-9._-]+$")


class TaskAmbiguousError(ValueError):
    """``--name`` prefix matched more than one template."""


class TaskConflictError(ValueError):
    """The named template exists in both the config dir and the profile dir with
    different content - reconcile the files."""


class TaskExistsError(ValueError):
    """``tasks create`` would overwrite an existing file without ``--force``."""


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


def format_tasks_create_human(payload: dict[str, Any]) -> list[str]:
    verb = "overwrote" if payload.get("overwrote") else "created"
    return [sanitize_terminal(f"{verb} {payload.get('path', '')}")]


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


async def tasks_create(
    *,
    config: BlumkinConfig,
    name: str,
    prompt: str,
    force: bool = False,
    input: str = "",
    output: str = "",
    title: str = "",
    trigger: str = "",
) -> dict[str, Any]:
    """Write a new ``tasks/<name>.md`` under the chosen tasks directory."""
    stem = _validate_name(name)
    title_value = _validate_oneline(title, field="--title")
    trigger_value = _validate_oneline(trigger, field="--trigger")
    input_value = _validate_oneline(input, field="--input")
    output_value = _validate_oneline(output, field="--output")
    prompt_value = prompt if isinstance(prompt, str) else ""
    if not prompt_value.strip():
        raise ValueError("--prompt is required (the Prompt: block body)")

    directory = _create_tasks_dir(config)
    path = _create_path(directory, stem)
    overwrote = path.is_file()
    if overwrote and not force:
        raise TaskExistsError(
            f"template {stem!r} already exists at {path}; pass --force to overwrite"
        )

    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _render_markdown(
            title=title_value,
            trigger=trigger_value,
            input_value=input_value,
            output=output_value,
            prompt=prompt_value,
        ),
        encoding="utf-8",
    )
    loaded = load_tasks(config)
    created = next(task for task in loaded if task.name == stem)
    return {
        "ok": True,
        "overwrote": overwrote,
        "path": str(path),
        "task": {
            "conflict": created.conflict,
            "input": created.input,
            "name": created.name,
            "output": created.output,
            "prompt": created.prompt,
            "sources": list(created.sources),
            "title": created.title,
            "trigger": created.trigger,
        },
    }


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


def _create_path(directory: Path, name: str) -> Path:
    """Resolve ``directory/name.md`` and refuse anything outside ``directory``."""
    directory_resolved = directory.resolve()
    path = (directory / f"{name}.md").resolve()
    if not path.is_relative_to(directory_resolved):
        raise ValueError(f"refusing path outside the tasks directory: {path}")
    return path


def _create_tasks_dir(config: BlumkinConfig) -> Path:
    """Directory ``tasks create`` writes into.

    Prefer the active profile's ``tasks/`` when that directory already exists
    (the operator is using profile overlays); otherwise the config-dir ``tasks/``.
    """
    profile_tasks = config.profile_dir / "tasks"
    if profile_tasks.is_dir():
        return profile_tasks
    return config.config_dir / "tasks"


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
        # utf-8-sig strips a BOM (Windows editors add one, and U+FEFF is not
        # whitespace so it would eat the first line); errors="replace" keeps a
        # non-UTF-8 file from aborting, with a warning.
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        emit_warning(f"could not read {path}: {exc}")
        text = ""
    if "�" in text:
        emit_warning(f"{path}: not valid UTF-8; some characters were replaced")
    fields: dict[str, str] = {}
    prompt: list[str] = []
    title = ""
    in_prompt = False
    saw_blockquote = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if in_prompt:
            # The prompt is the `>` blockquote only. Blank lines inside it are
            # kept (paragraph breaks); the first non-blank, non-`>` line ends it.
            if stripped.startswith(">"):
                saw_blockquote = True
                prompt.append(stripped[1:].removeprefix(" "))
            elif not stripped:
                prompt.append("")
            else:
                in_prompt = False
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
    if not saw_blockquote:
        emit_warning(f"{path}: no `**Prompt:**` blockquote")
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


def _render_markdown(
    *,
    title: str,
    trigger: str,
    input_value: str,
    output: str,
    prompt: str,
) -> str:
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
        lines.append("")
    for label, value in (
        ("Trigger", trigger),
        ("Input", input_value),
        ("Output", output),
    ):
        if value:
            lines.append(f"**{label}:** {value}")
    if any((trigger, input_value, output)):
        lines.append("")
    lines.append("**Prompt:**")
    lines.append("")
    for raw in prompt.splitlines() or [""]:
        lines.append(f"> {raw}" if raw else ">")
    lines.append("")
    return "\n".join(lines)


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


def _validate_name(name: str) -> str:
    stem = name.strip()
    if not stem:
        raise ValueError("--name is required (file stem: lowercase letters, digits, ., _, -)")
    if stem in {".", ".."} or "/" in stem or "\\" in stem or "\0" in stem:
        raise ValueError(f"invalid --name {name!r}: path separators and '.' / '..' are refused")
    if not _NAME_RE.fullmatch(stem):
        raise ValueError(
            f"invalid --name {name!r}: use lowercase letters, digits, '.', '_' or '-' only"
        )
    return stem


def _validate_oneline(value: str, *, field: str) -> str:
    text = value if isinstance(value, str) else ""
    if "\n" in text or "\r" in text:
        raise ValueError(f"{field} must be a single line")
    return text.strip()
