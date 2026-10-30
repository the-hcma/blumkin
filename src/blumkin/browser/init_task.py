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
import json
import os
import stat
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blumkin.browser.profiles import BrowserProfileError, create_profile, validate_task_name
from blumkin.config import validate_browser_task_table
from blumkin.config_lock import config_file_lock
from blumkin.providers.kind import ProviderConfigError


class InitTaskError(ValueError):
    """The template or the task name is unknown, or `config.toml` cannot take the policy."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskTemplate:
    """The policy and the first steps for one known site."""

    allowed_controls: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    #: Buttons that confirm a dialog's own rows (valid only inside a dialog).
    dialog_confirm: tuple[str, ...] = ()
    name: str
    #: The site's host differs per organization, so the caller names it (`--host`).
    needs_host: bool = False
    #: Format with `task=`; the commands to run once the policy is in place.
    next_steps: tuple[str, ...]
    summary: str
    #: Where uploads may be read from when the caller names no folder.
    upload_dirs: tuple[str, ...] = ()


TEMPLATES: dict[str, TaskTemplate] = {
    "expense-report": TaskTemplate(
        # Names the Oracle Fusion Expenses flow shows, none of which is a finalizing name:
        # `Actions for * Report` opens a saved report's actions menu (its `Open` item is
        # declared by itself; `Submit`, `Delete` and `Duplicate` never are), and `OK` confirms
        # the attachments dialog only inside that dialog. Edit them to what your form shows.
        allowed_controls=(
            "Actions for * Report",
            "Add Row",
            "Create Item",
            "Create Report",
            "Manage Attachments",
            "Open",
            "Save",
            "Save and Close",
        ),
        allowed_hosts=(),
        dialog_confirm=("OK",),
        name="expense-report",
        needs_host=True,
        next_steps=(
            "Check the controls in config.toml against what your expense form shows; they are the "
            "names of the Oracle Fusion Expenses flow; none is a finalizing name.",
            "Launch the browser and open the form: `blumkin browser launch {task} --open` (needs "
            "`--form-url` here or `form_url` in config.toml), then sign in by hand.",
            "Record each page and dialog the flow passes through, one at a time: stay on the "
            "page, then `blumkin browser explore observe {task} PAGE` (it adds the page to the "
            "same draft, or to the stored model); then `blumkin browser explore save {task} "
            "--yes`.",
            "File a receipt: `blumkin receipts summarize` the files, confirm the table and the "
            "total with the user, `blumkin receipts rehome ... --yes`, then fill the item with "
            "`blumkin browser fill`, `select` and `upload` (one run per receipt). The task never "
            "submits: end with `blumkin browser handoff {task}`.",
        ),
        summary="File receipts into an expense report through the task browser (never submits).",
        upload_dirs=("~/Documents/receipts-archive",),
    ),
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


def init_task(
    config_dir: Path,
    task: str,
    template_name: str,
    *,
    write: bool,
    hosts: Sequence[str] = (),
    upload_dirs: Sequence[str] = (),
    form_url: str = "",
) -> dict[str, Any]:
    """Create the task's profile; add its policy to `config.toml` when `write` is true.

    `hosts`, `upload_dirs` and `form_url` replace what the template starts with; a template whose
    site differs per organization (`needs_host`) refuses to go on without a host.
    """
    template = TEMPLATES.get(template_name)
    if template is None:
        raise InitTaskError(
            f"Expected one of {', '.join(sorted(TEMPLATES))}, got template {template_name!r}"
        )
    chosen_hosts = tuple(host.strip().lower() for host in hosts) or template.allowed_hosts
    if template.needs_host and not chosen_hosts:
        raise InitTaskError(
            f"Expected --host for the {template.name!r} template (the site's host differs per "
            "organization, for example erp.example.com), got none"
        )
    if (upload_dirs or form_url.strip()) and not template.upload_dirs:
        raise InitTaskError(
            f"Expected no --upload-dir or --form-url for the {template.name!r} template (it "
            "uploads nothing), got one"
        )
    chosen_dirs = tuple(upload_dirs) or template.upload_dirs
    chosen_url = form_url.strip()
    try:
        validate_task_name(task)
        create_profile(config_dir, task)
    except BrowserProfileError as exc:
        raise InitTaskError(str(exc)) from exc
    path = config_dir / "config.toml"
    table = render_table(
        task, template, hosts=chosen_hosts, upload_dirs=chosen_dirs, form_url=chosen_url
    )
    _check_policy(task, table)
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


def render_table(
    task: str,
    template: TaskTemplate,
    *,
    hosts: Sequence[str] | None = None,
    upload_dirs: Sequence[str] | None = None,
    form_url: str = "",
) -> str:
    """The `[browser.tasks.<task>]` table for `template`, as TOML text."""
    lines = [f"[browser.tasks.{task}]"]
    lines.append(
        f"allowed_hosts = {_toml_list(template.allowed_hosts if hosts is None else hosts)}"
    )
    lines.append(f"allowed_controls = {_toml_list(template.allowed_controls)}")
    if template.dialog_confirm:
        lines.append(f"dialog_confirm = {_toml_list(template.dialog_confirm)}")
    folders = template.upload_dirs if upload_dirs is None else upload_dirs
    if folders:
        lines.append(f"upload_dirs = {_toml_list(folders)}")
    if form_url:
        lines.append(f"form_url = {_toml_string(form_url)}")
    return "\n".join(lines) + "\n"


def _check_policy(task: str, table: str) -> None:
    """Refuse a table that the real config loader would reject, before anything is written."""
    try:
        parsed = tomllib.loads(table)
        validate_browser_task_table(task, parsed["browser"]["tasks"][task])
    except (tomllib.TOMLDecodeError, ProviderConfigError, KeyError) as exc:
        raise InitTaskError(f"Expected a valid [browser.tasks.{task}] table, got: {exc}") from exc


def _toml_list(values: Sequence[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _toml_string(value: str) -> str:
    """A TOML basic string: a JSON string, with every non-ASCII character escaped."""
    return json.dumps(value, ensure_ascii=True)


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
