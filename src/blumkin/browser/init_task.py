"""`blumkin browser init-task`: a built-in task template, created and written only on request.

A browser task needs a profile and a `[browser.tasks.<task>]` policy in `config.toml`. For a
known site the policy is the same for everyone (the hosts it uses and the navigation controls
it needs), so it ships here as a template. The profile is always created (it is empty and
harmless); the policy is only printed unless the caller asks for it to be written, and a policy
of the same name that already exists is never changed.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blumkin.browser.profiles import BrowserProfileError, create_profile, validate_task_name


@dataclass(frozen=True, slots=True)
class TaskTemplate:
    """The policy and the first steps for one known site."""

    allowed_controls: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    name: str
    #: Format with `task=`; the commands to run once the policy is in place.
    next_steps: tuple[str, ...]
    summary: str


class InitTaskError(ValueError):
    """The template or the task name is unknown, or `config.toml` cannot take the policy."""


TEMPLATES: dict[str, TaskTemplate] = {
    "teams-transcript": TaskTemplate(
        allowed_controls=("Notes", "Transcript"),
        allowed_hosts=("teams.cloud.microsoft", "teams.microsoft.com", "*.sharepoint.com"),
        name="teams-transcript",
        next_steps=(
            "Launch the browser: `blumkin browser launch {task}`, then sign in to Teams by hand "
            "(SSO and MFA included) and open the meeting's Notes or Transcript view.",
            "Check what the page shows: `blumkin browser frames {task}`.",
            "Capture it: `blumkin browser transcript {task} --duration 34m06s --out "
            "transcript.txt` (use the meeting's length so the coverage report can check the tail).",
        ),
        summary="Capture a Teams meeting transcript through the task browser (read-only).",
    ),
}


def init_task(config_dir: Path, task: str, template_name: str, *, write: bool) -> dict[str, Any]:
    """Create the task's profile; add its policy to `config.toml` when `write` is true."""
    template = TEMPLATES.get(template_name)
    if template is None:
        raise InitTaskError(
            f"Expected one of {', '.join(sorted(TEMPLATES))}, got template {template_name!r}"
        )
    try:
        validate_task_name(task)
    except BrowserProfileError as exc:
        raise InitTaskError(str(exc)) from exc
    path = config_dir / "config.toml"
    existing = _read_config(path)
    table = render_table(task, template)
    declared = task in _declared_tasks(existing)
    written = False
    if write and not declared:
        _append(path, existing, table, task)
        written = True
    create_profile(config_dir, task)
    return {
        "action": "init-task",
        "already_declared": declared,
        "config_path": str(path),
        "next_steps": [step.format(task=task) for step in template.next_steps],
        "ok": True,
        "summary": template.summary,
        "table": table,
        "task": task,
        "template": template.name,
        "written": written,
    }


def render_table(task: str, template: TaskTemplate) -> str:
    """The `[browser.tasks.<task>]` table for `template`, as TOML text."""
    controls = ", ".join(f'"{name}"' for name in template.allowed_controls)
    hosts = ", ".join(f'"{host}"' for host in template.allowed_hosts)
    return f"[browser.tasks.{task}]\nallowed_hosts = [{hosts}]\nallowed_controls = [{controls}]\n"


def _append(path: Path, existing: str | None, table: str, task: str) -> None:
    """Append `table` to `path` (creating it 0600), after proving the result is valid TOML."""
    prefix = existing or ""
    separator = "" if not prefix else ("\n" if prefix.endswith("\n") else "\n\n")
    addition = f"{separator}{table}" if not prefix else f"{separator}\n{table}"
    combined = prefix + addition
    try:
        tomllib.loads(combined)
    except tomllib.TOMLDecodeError as exc:
        raise InitTaskError(
            f"Expected {path} to take a [browser.tasks.{task}] table, got: {exc}"
        ) from exc
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise InitTaskError(f"Expected to write {path}, got {type(exc).__name__}") from exc
    with contextlib.closing(os.fdopen(fd, "a", encoding="utf-8")) as handle:
        if stat.S_IMODE(os.fstat(handle.fileno()).st_mode) & 0o077 and not prefix:
            os.fchmod(handle.fileno(), 0o600)
        handle.write(addition)
        handle.flush()
        os.fsync(handle.fileno())


def _declared_tasks(existing: str | None) -> set[str]:
    if not existing:
        return set()
    tasks = tomllib.loads(existing).get("browser", {}).get("tasks", {})
    return set(tasks) if isinstance(tasks, dict) else set()


def _read_config(path: Path) -> str | None:
    """The text of `config.toml` (None when absent), checked to be valid TOML."""
    if path.is_symlink():
        raise InitTaskError(f"Expected {path} to be a regular file, got a symlink")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InitTaskError(f"Expected to read {path}, got {type(exc).__name__}") from exc
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InitTaskError(f"Expected {path} to be valid TOML, got: {exc}") from exc
    return text
