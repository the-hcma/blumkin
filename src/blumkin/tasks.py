"""``tasks/<name>.md`` - named reusable prompt templates.

One markdown file per template under ``<config-dir>/tasks/`` (and the active
profile's ``profiles/<name>/tasks/``, merged on top). ``tasks.create`` /
``tasks.update`` / ``tasks.delete`` mutate files; ``tasks.list`` /
``tasks.show`` surface the templates. The agent fuzzy-matches the user's
request against each template's ``Trigger:`` line, confirms the pick with the
user, then runs the ``Prompt:`` block itself. blumkin ships no matcher and
never calls a model.

File shape::

    # Weekly report            <- optional; else the filename stem is the title
    **Trigger:** "weekly report", "status update"
    **Input:** the latest thread in the Reports folder
    **Output:** a 5-bullet summary, mailed to the team

    **Prompt:**

    > Summarise the input as exactly five bullets, most important first.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blumkin.config import BlumkinConfig
from blumkin.output import emit_warning, sanitize_terminal

_MAX_NAME_LENGTH = 64
_NAME_RE = re.compile(r"^[a-z0-9._-]+$")
# Characters ``str.splitlines()`` treats as breaks (not only CR/LF).
_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


class TaskAmbiguousError(ValueError):
    """``--name`` prefix matched more than one template."""


class TaskConflictError(ValueError):
    """The named template exists in both the config dir and the profile dir with
    different content - reconcile the files."""


class TaskExistsError(ValueError):
    """``tasks create`` would overwrite an existing file without ``--force``."""


class TaskValidationError(ValueError):
    """Bad ``--name`` / oneline field / prompt input for create or update.

    Typed so ``classify_exception`` maps it to ``usage_error`` before the
    auth-layer ValueError heuristics can re-read operator text (a rejected
    name containing ``client_id``, or the word ``Missing``) as ``auth_required``.
    """


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


def format_tasks_delete_human(payload: dict[str, Any]) -> list[str]:
    if payload.get("dry_run"):
        return [sanitize_terminal(f"would delete {payload.get('path', '')}")]
    return [sanitize_terminal(f"deleted {payload.get('path', '')}")]


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


def format_tasks_update_human(payload: dict[str, Any]) -> list[str]:
    return [sanitize_terminal(f"updated {payload.get('path', '')}")]


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
        raise TaskValidationError("--prompt is required (the Prompt: block body)")

    directory = _create_tasks_dir(config)
    path = _create_path(directory, stem)
    overwrote = path.is_file()
    # A same-named template in the *other* tasks dir would merge into a
    # conflict / multi-source task that update/delete then refuse. Block that
    # before --force (force only overwrites the destination file).
    existing = next((task for task in load_tasks(config) if task.name == stem), None)
    if existing is not None and any(Path(source).resolve() != path for source in existing.sources):
        raise TaskExistsError(
            f"template {stem!r} already exists at {', '.join(existing.sources)}; "
            "remove or rename that copy before creating another"
        )
    if overwrote and not force:
        raise TaskExistsError(
            f"template {stem!r} already exists at {path}; pass --force to overwrite"
        )

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(
        path,
        _render_markdown(
            title=title_value,
            trigger=trigger_value,
            input_value=input_value,
            output=output_value,
            prompt=prompt_value,
        ),
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


async def tasks_delete(
    *,
    config: BlumkinConfig,
    name: str,
    dry_run: bool = False,
    yes: bool = False,
) -> dict[str, Any]:
    """Remove a template file. Requires ``yes`` unless ``dry_run``."""
    # Imported here: blumkin.skills.errors imports this module (circular otherwise).
    from blumkin.skills.errors import ConsentRequiredError

    task = _select(load_tasks(config), name)
    _require_single_source(task)
    path = _mutable_path(config, task)
    if dry_run:
        return {"ok": True, "dry_run": True, "name": task.name, "path": str(path)}
    if task.name != name.strip():
        raise TaskValidationError(
            f"Expected the exact template name to delete, got the prefix {name.strip()!r} "
            f"of {task.name!r}; re-run with the full name (a --dry-run accepts a prefix)"
        )
    if not yes:
        raise ConsentRequiredError(
            "--yes is required to delete a template",
            hint="Re-run with --yes, or pass --dry-run to show the path only.",
        )
    path.unlink()
    return {"ok": True, "dry_run": False, "name": task.name, "path": str(path)}


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


async def tasks_update(
    *,
    config: BlumkinConfig,
    name: str,
    input: str | None = None,
    output: str | None = None,
    prompt: str | None = None,
    title: str | None = None,
    trigger: str | None = None,
) -> dict[str, Any]:
    """Rewrite one template, preserving fields the caller did not pass."""
    if all(value is None for value in (input, output, prompt, title, trigger)):
        raise TaskValidationError(
            "pass at least one of --title, --trigger, --input, --output, --prompt to update"
        )
    task = _select(load_tasks(config), name)
    _require_single_source(task)
    path = _mutable_path(config, task)
    title_value = task.title if title is None else _validate_oneline(title, field="--title")
    trigger_value = (
        task.trigger if trigger is None else _validate_oneline(trigger, field="--trigger")
    )
    input_value = task.input if input is None else _validate_oneline(input, field="--input")
    output_value = task.output if output is None else _validate_oneline(output, field="--output")
    if prompt is None:
        prompt_value = task.prompt
    else:
        prompt_value = prompt if isinstance(prompt, str) else ""
        if not prompt_value.strip():
            raise TaskValidationError("--prompt must be non-empty when provided")
    # Rewriting renders only the modeled fields. Refuse when that would drop
    # anything else in the file (notes, extra headings, a second blockquote).
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TaskValidationError(
            f"Expected template {task.name!r} to be valid UTF-8, got bytes that are not; "
            "re-save the file as UTF-8 first"
        ) from exc
    original = _signature(text)
    unchanged = {
        "trigger": task.trigger,
        "input_value": task.input,
        "output": task.output,
        "prompt": task.prompt,
    }
    # The stored title may be the stem default (no # line) or an explicit # line
    # that happens to equal the name; keep whichever form the file has.
    for kept_title in ("", task.title):
        if _signature(_render_markdown(title=kept_title, **unchanged)) == original:
            break
    else:
        raise TaskValidationError(
            f"Expected template {task.name!r} to hold only a title, Trigger/Input/Output "
            "lines and a Prompt blockquote, got other content that an update would drop; "
            "edit the file by hand to change it"
        )
    write_title = kept_title if title is None else title_value
    _write_private(
        path,
        _render_markdown(
            title=write_title,
            trigger=trigger_value,
            input_value=input_value,
            output=output_value,
            prompt=prompt_value,
        ),
    )
    updated = _select(load_tasks(config), task.name)
    return {
        "ok": True,
        "path": str(path),
        "task": {
            "conflict": updated.conflict,
            "input": updated.input,
            "name": updated.name,
            "output": updated.output,
            "prompt": updated.prompt,
            "sources": list(updated.sources),
            "title": updated.title,
            "trigger": updated.trigger,
        },
    }


def _create_path(directory: Path, name: str) -> Path:
    """Resolve ``directory/name.md`` and refuse anything outside ``directory``."""
    directory_resolved = directory.resolve()
    path = (directory / f"{name}.md").resolve()
    if not path.is_relative_to(directory_resolved):
        raise TaskValidationError(f"refusing path outside the tasks directory: {path}")
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


def _mutable_path(config: BlumkinConfig, task: Task) -> Path:
    """Resolved file path for update/delete, confined to a known tasks directory.

    Profile ``tasks/`` directories may themselves be symlinks into a separate
    config repo - that is allowed. A *file* symlink is refused: unlinking the
    resolved target would leave a dangling ``*.md`` that ``load_tasks`` still
    lists. A file whose resolved path escapes the resolved tasks directory
    that listed it is refused.
    """
    raw = Path(task.sources[-1])
    if raw.is_symlink():
        raise TaskValidationError(
            f"refusing to modify symlinked template file {raw}; replace the "
            "link with a real file (or delete the link yourself) before update/delete"
        )
    allowed_parents = {
        (base / "tasks").resolve() for base in (config.config_dir, config.profile_dir)
    }
    parent = raw.parent.resolve()
    if parent not in allowed_parents:
        raise TaskValidationError(f"refusing to modify path outside a tasks directory: {raw}")
    resolved = raw.resolve()
    if not resolved.is_relative_to(parent):
        raise TaskValidationError(f"refusing path escape from the tasks directory: {raw}")
    return resolved


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


def _require_single_source(task: Task) -> None:
    """Refuse update/delete when identical copies exist in more than one tasks dir.

    ``load_tasks`` merges identical config/profile copies into one non-conflicting
    task. Mutating only ``sources[-1]`` would leave the other copy visible (delete)
    or create a conflict (update). Ask the operator to remove the duplicate first.
    """
    distinct = tuple(dict.fromkeys(task.sources))
    if len(distinct) > 1:
        raise TaskConflictError(
            f"template {task.name!r} has identical copies across {', '.join(distinct)} "
            "- remove one copy before update or delete"
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


def _signature(text: str) -> list[str]:
    """The non-blank lines of a template, normalized so layout differences do not matter."""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == ">":
            continue
        if line.startswith(">"):
            lines.append(">" + line[1:].removeprefix(" "))
        elif line.startswith("#"):
            lines.append("# " + line.lstrip("#").strip())
        else:
            lowered = line.lower()
            for label in ("trigger", "input", "output", "prompt"):
                if lowered.startswith(f"**{label}:**"):
                    lines.append(f"**{label}:** " + line.split(":**", 1)[1].strip())
                    break
            else:
                lines.append(line)
    return lines


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
        raise TaskValidationError(
            "--name is required (file stem: lowercase letters, digits, ., _, -)"
        )
    if len(stem) > _MAX_NAME_LENGTH:
        raise TaskValidationError(
            f"Expected a --name of at most {_MAX_NAME_LENGTH} characters, got {len(stem)}"
        )
    if stem in {".", ".."} or "/" in stem or "\\" in stem or "\0" in stem:
        raise TaskValidationError("invalid --name: path separators and '.' / '..' are refused")
    if not _NAME_RE.fullmatch(stem):
        raise TaskValidationError(
            "invalid --name: use lowercase letters, digits, '.', '_' or '-' only"
        )
    return stem


def _validate_oneline(value: str, *, field: str) -> str:
    text = value if isinstance(value, str) else ""
    if any(ch in _LINE_BREAKS for ch in text):
        raise TaskValidationError(f"{field} must be a single line")
    return text.strip()


def _write_private(path: Path, text: str) -> None:
    """Replace ``path`` atomically with ``text`` (mode 0600); the old file survives a failure."""
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
