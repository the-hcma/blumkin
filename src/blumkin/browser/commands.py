"""Payloads behind the `blumkin browser` commands (issue #468).

The click wrappers in `blumkin.cli` stay thin: they load the config, call one of these
functions and render the result, mapping the exceptions below to the standard error
envelope and exit codes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from blumkin.browser.cdp import BrowserNotRunningError, count_page_targets
from blumkin.browser.discovery import resolve_browser
from blumkin.browser.launcher import DEFAULT_CLOSE_TIMEOUT_S, close_browser, launch
from blumkin.browser.profiles import (
    BrowserProfile,
    BrowserSession,
    create_profile,
    delete_profile,
    list_profiles,
    load_profile,
    read_session,
    validate_task_name,
)
from blumkin.config import BrowserSetup

SIGN_IN_GUIDANCE = (
    "Sign in by hand in the browser window that opened (SSO and MFA included); "
    "blumkin never sees your credentials."
)


class TaskNotFoundError(LookupError):
    """No browser profile exists for the task."""


def close_task_browser(
    config_dir: Path,
    task: str,
    *,
    force: bool = False,
    timeout_s: float = DEFAULT_CLOSE_TIMEOUT_S,
) -> dict[str, Any]:
    """Close the task's browser, keeping its profile (cookies, saved sign-in) for next time."""
    profile = require_profile(config_dir, task)
    result = close_browser(profile, force=force, timeout_s=timeout_s)
    return {
        "forced": result.forced,
        "ok": True,
        "task": task,
        "was_running": result.was_running,
    }


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
    require_profile(config_dir, task)
    delete_profile(config_dir, task, pid_alive=pid_alive)
    return {"deleted": True, "ok": True, "task": task}


def launch_task_browser(config: BrowserSetup, task: str) -> dict[str, Any]:
    """Launch (or find) the task's browser on its profile."""
    profile = require_profile(config.config_dir, task)
    # A browser that is already running is reported as it is, even when no installed browser
    # can be found any more: nothing needs launching, so nothing needs resolving.
    running = read_session(profile.path, strict=True)
    if running is not None:
        return _launch_payload(task, running, already_running=True, fallback_from=None)
    browser = resolve_browser(config.browser.default)
    result = launch(profile, browser)
    # Report what is actually running: with a session already open that is the browser it
    # was started with, not necessarily the one the config names today, and a fallback is
    # only news for a browser that was just started.
    started_fallback = None if result.already_running else browser.fallback_from
    return _launch_payload(
        task,
        result.session,
        already_running=result.already_running,
        fallback_from=started_fallback.value if started_fallback else None,
    )


def list_task_profiles(config_dir: Path) -> dict[str, Any]:
    """Every profile blumkin created, with whether its browser is running."""
    return {
        "ok": True,
        "profiles": [_profile_entry(p, lambda endpoint: None) for p in list_profiles(config_dir)],
    }


def require_profile(config_dir: Path, task: str) -> BrowserProfile:
    """The profile for `task`; a malformed name is a usage error, a missing one is not found."""
    validate_task_name(task)
    profile = load_profile(config_dir, task)
    if profile is None:
        raise TaskNotFoundError(f"Expected an existing browser profile for task {task!r}, got none")
    return profile


def task_endpoint(config_dir: Path, task: str) -> dict[str, Any]:
    """The DevTools endpoint of the task's running browser (it changes on every launch)."""
    profile = require_profile(config_dir, task)
    session = read_session(profile.path)
    if session is None:
        raise BrowserNotRunningError(
            f"Expected a running browser for task {task!r}, got none; "
            f"run `blumkin browser launch {task}` first"
        )
    return {
        "browser": session.browser,
        "endpoint": session.endpoint,
        "ok": True,
        "pid": session.pid,
        "port": session.port,
        "task": task,
    }


def task_status(
    config_dir: Path,
    task: str | None,
    *,
    count_pages: Callable[[str], int | None] = count_page_targets,
) -> dict[str, Any]:
    """Status of one task's profile, or of all of them when `task` is None."""
    if task is None:
        return {
            "ok": True,
            "profiles": [_profile_entry(p, count_pages) for p in list_profiles(config_dir)],
        }
    profile = require_profile(config_dir, task)
    return {"ok": True, "profiles": [_profile_entry(profile, count_pages)]}


def _launch_payload(
    task: str, session: BrowserSession, *, already_running: bool, fallback_from: str | None
) -> dict[str, Any]:
    return {
        "already_running": already_running,
        "browser": session.browser,
        "endpoint": session.endpoint,
        "fallback_from": fallback_from,
        "ok": True,
        "pid": session.pid,
        "port": session.port,
        "sign_in": SIGN_IN_GUIDANCE,
        "task": task,
    }


def _profile_entry(
    profile: BrowserProfile, count_pages: Callable[[str], int | None] = count_page_targets
) -> dict[str, Any]:
    session = read_session(profile.path)
    pages = count_pages(session.endpoint) if session else None
    note = None
    if session is not None and pages == 0:
        note = (
            "the browser is running but has no open page: open one in its window, or run "
            f"`blumkin browser goto {profile.task} URL` (a blank page is opened for you)"
        )
    return {
        "browser": session.browser if session else None,
        "endpoint": session.endpoint if session else None,
        "note": note,
        "pages": pages,
        "path": str(profile.path),
        "pid": session.pid if session else None,
        "running": session is not None,
        "task": profile.task,
    }
