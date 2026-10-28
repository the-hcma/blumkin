"""`blumkin browser init-task`: a built-in task template, created and written only on request.

A browser task needs a profile and a `[browser.tasks.<task>]` policy in `config.toml`. For a
known site the policy is the same for everyone (the hosts it uses and the navigation controls
it needs), so it ships here as a template. The profile is always created first (it is empty and
harmless); the policy is only printed unless the caller asks for it to be written, and a policy
of the same name that already exists is never changed.

`config.toml` holds the user's only settings, so it is written with care: the file is opened
once (never through a symlink, never blocking on a special file), locked, read and checked
under the lock, and extended only if the result is still valid TOML. A write that fails part
way is cut back to the original size.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blumkin.browser.profiles import BrowserProfileError, create_profile, validate_task_name
from blumkin.config_lock import config_file_lock


class InitTaskError(ValueError):
    """The template or the task name is unknown, or `config.toml` cannot take the policy."""


@dataclass(frozen=True, slots=True)
class TaskTemplate:
    """The policy and the first steps for one known site."""

    allowed_controls: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    name: str
    #: Format with `task=`; the commands to run once the policy is in place.
    next_steps: tuple[str, ...]
    summary: str


TEMPLATES: dict[str, TaskTemplate] = {
    "teams-transcript": TaskTemplate(
        allowed_controls=("Notes", "Transcript"),
        allowed_hosts=("teams.cloud.microsoft", "teams.microsoft.com", "*.sharepoint.com"),
        name="teams-transcript",
        next_steps=(
            "Narrow `*.sharepoint.com` to your tenant's SharePoint host (for example "
            "`yourtenant-my.sharepoint.com`) in config.toml once you know it: the wildcard "
            "covers every organization's SharePoint.",
            "Launch the browser: `blumkin browser launch {task}`, then sign in to Teams by hand "
            "(SSO and MFA included), open the meeting and show its Transcript view.",
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
        create_profile(config_dir, task)
    except BrowserProfileError as exc:
        raise InitTaskError(str(exc)) from exc
    path = config_dir / "config.toml"
    table = render_table(task, template)
    declared, written = _update_config(path, task, table, write=write)
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


def _declared_tasks(parsed: dict[str, Any], path: Path) -> set[str]:
    """The tasks `config.toml` already declares; `browser` and `browser.tasks` must be tables."""
    browser = parsed.get("browser", {})
    if not isinstance(browser, dict):
        raise InitTaskError(
            f"Expected [browser] in {path} to be a table, got {type(browser).__name__}"
        )
    tasks = browser.get("tasks", {})
    if not isinstance(tasks, dict):
        raise InitTaskError(
            f"Expected [browser.tasks] in {path} to be a table, got {type(tasks).__name__}"
        )
    return set(tasks)


def _parse(text: str, path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        hint = (
            "; remove the byte order mark at the start of the file" if text.startswith("﻿") else ""
        )
        raise InitTaskError(f"Expected {path} to be valid TOML, got: {exc}{hint}") from exc


def _read_all(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        chunk = os.pread(fd, min(1 << 20, size - offset), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _update_config(path: Path, task: str, table: str, *, write: bool) -> tuple[bool, bool]:
    """(already declared, written): check `config.toml` and, when asked, extend it.

    A write holds the shared config lock for the whole read-to-write span, so two runs (or
    another command that edits the file) cannot interleave, and all of it happens on one file
    descriptor.
    """
    if write:
        with config_file_lock(path):
            return _update_locked(path, task, table, write=True)
    return _update_locked(path, task, table, write=False)


def _update_locked(path: Path, task: str, table: str, *, write: bool) -> tuple[bool, bool]:
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_RDWR | os.O_CREAT if write else os.O_RDONLY)
    try:
        fd = os.open(path, flags, 0o600)
    except FileNotFoundError:
        return False, False
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise InitTaskError(f"Expected {path} to be a regular file, got a symlink") from exc
        raise
    with contextlib.closing(os.fdopen(fd, "rb", closefd=True)):
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InitTaskError(f"Expected {path} to be a regular file, got something else")
        size = info.st_size
        try:
            text = _read_all(fd, size).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InitTaskError(f"Expected {path} to be UTF-8 text, got {exc.reason}") from exc
        declared = task in _declared_tasks(_parse(text, path), path)
        if declared or not write:
            return declared, False
        addition = _separator(text) + table
        try:
            tomllib.loads(text + addition)
        except tomllib.TOMLDecodeError as exc:
            raise InitTaskError(
                f"Expected {path} to take a [browser.tasks.{task}] table, got: {exc}"
            ) from exc
        _append(fd, size, addition.encode("utf-8"))
        return False, True


def _append(fd: int, size: int, data: bytes) -> None:
    """Write `data` at `size`; on any failure cut the file back to `size`."""
    try:
        written = 0
        while written < len(data):
            written += os.pwrite(fd, data[written:], size + written)
        os.fsync(fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.ftruncate(fd, size)
        raise


def _separator(text: str) -> str:
    """What to put between the existing text and the new table."""
    if not text:
        return ""
    return "\n" if text.endswith("\n") else "\n\n"
