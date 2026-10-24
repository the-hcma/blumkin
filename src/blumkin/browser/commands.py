"""Payloads behind the `blumkin browser` commands (issue #468).

The click wrappers in `blumkin.cli` stay thin: they load the config, call one of these
functions and render the result, mapping the exceptions below to the standard error
envelope and exit codes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from blumkin.browser.discovery import resolve_browser
from blumkin.browser.launcher import launch
from blumkin.browser.profiles import (
    BrowserProfile,
    create_profile,
    delete_profile,
    list_profiles,
    load_profile,
    read_session,
)
from blumkin.config import BrowserSetup

SIGN_IN_GUIDANCE = (
    "Sign in by hand in the browser window that opened (SSO and MFA included); "
    "blumkin never sees your credentials."
)


class TaskNotFoundError(LookupError):
    """No browser profile exists for the task."""


def create_task_profile(config_dir: Path, task: str) -> dict[str, Any]:
    """Create the profile for `task` (idempotent) and report whether it was new."""
    existed = load_profile(config_dir, task) is not None
    profile = create_profile(config_dir, task)
    return {
        "created": not existed,
        "ok": True,
        "path": str(profile.path),
        "task": profile.task,
    }


def delete_task_profile(
    config_dir: Path,
    task: str,
    *,
    pid_alive: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    """Delete the profile for `task`, saved sign-in included; refuses while it runs."""
    if load_profile(config_dir, task) is None:
        raise TaskNotFoundError(f"Expected an existing browser profile for task {task!r}, got none")
    delete_profile(config_dir, task, pid_alive=pid_alive)
    return {"deleted": True, "ok": True, "task": task}


def launch_task_browser(config: BrowserSetup, task: str) -> dict[str, Any]:
    """Launch (or find) the task's browser on its profile."""
    profile = load_profile(config.config_dir, task)
    if profile is None:
        raise TaskNotFoundError(f"Expected an existing browser profile for task {task!r}, got none")
    browser = resolve_browser(config.browser.default)
    result = launch(profile, browser)
    # Report what is actually running: with a session already open that is the browser it
    # was started with, not necessarily the one the config names today, and a fallback is
    # only news for a browser that was just started.
    started_fallback = None if result.already_running else browser.fallback_from
    return {
        "already_running": result.already_running,
        "browser": result.session.browser,
        "endpoint": result.session.endpoint,
        "fallback_from": started_fallback.value if started_fallback else None,
        "ok": True,
        "pid": result.session.pid,
        "port": result.session.port,
        "sign_in": SIGN_IN_GUIDANCE,
        "task": task,
    }


def list_task_profiles(config_dir: Path) -> dict[str, Any]:
    """Every profile blumkin created, with whether its browser is running."""
    return {"ok": True, "profiles": [_profile_entry(p) for p in list_profiles(config_dir)]}


def task_status(config_dir: Path, task: str | None) -> dict[str, Any]:
    """Status of one task's profile, or of all of them when `task` is None."""
    if task is None:
        return list_task_profiles(config_dir)
    profile = load_profile(config_dir, task)
    if profile is None:
        raise TaskNotFoundError(f"Expected an existing browser profile for task {task!r}, got none")
    return {"ok": True, "profiles": [_profile_entry(profile)]}


def _profile_entry(profile: BrowserProfile) -> dict[str, Any]:
    session = read_session(profile.path)
    return {
        "browser": session.browser if session else None,
        "endpoint": session.endpoint if session else None,
        "path": str(profile.path),
        "pid": session.pid if session else None,
        "running": session is not None,
        "task": profile.task,
    }
