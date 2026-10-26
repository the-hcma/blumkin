"""Payloads behind the `blumkin browser explore` commands (issue #486).

An exploration is a draft site model kept on disk between commands: `observe` adds the page
the browser is on, `click` follows a link (or, after the user says yes at their own
terminal, presses a control that may change state), `show` summarizes the draft, `save`
stores it as the next version of the task's model once the user has seen it and `discard`
drops it. Exploration needs the task to declare `allowed_hosts` and never leaves them.
"""

from __future__ import annotations

import contextlib
import re
import shlex
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from blumkin import __version__
from blumkin.browser.actions import host_allowed
from blumkin.browser.commands import require_profile
from blumkin.browser.explore import (
    DraftNotFoundError,
    ExploreNeedsConfirmationError,
    Explorer,
    ExploreRefusedError,
    ModelDraft,
    TerminalConfirmer,
    attach_exploring,
    decide_click,
)
from blumkin.browser.model import (
    SiteModel,
    discard_draft,
    load_draft,
    save_model,
    update_draft,
)
from blumkin.browser.profiles import validate_task_name
from blumkin.config import BrowserSetup


def explore_click(
    config: BrowserSetup, task: str, name: str, *, page_id: str | None, role: str
) -> dict[str, Any]:
    """Follow or press one control under the exploration rules; see `Explorer.click`."""
    hosts, extra_deny = explore_policy(config, task)
    profile = require_profile(config.config_dir, task)
    confirmer = TerminalConfirmer()
    decision = decide_click(role, name, extra_deny=extra_deny)
    with contextlib.closing(attach_exploring(profile, allowed_hosts=hosts)) as driver:
        explorer = Explorer(driver, confirmer, allowed_hosts=hosts, extra_deny=extra_deny)
        clicked = explorer.click(role, name)
    outcome = explorer.last_outcome
    unavailable = bool(getattr(confirmer, "unavailable", False))
    timed_out = bool(getattr(confirmer, "timed_out", False))
    recorded = clicked and page_id is not None and _record_step(config, task, page_id, role, name)
    return {
        "clicked": clicked,
        "decision": decision.value,
        "hint": _click_hint(
            task, name, role, outcome, unavailable=unavailable, timed_out=timed_out
        ),
        "name": name,
        "ok": True,
        "outcome": outcome,
        "recorded_step": recorded,
        "role": role,
        "terminal_unavailable": unavailable,
    }


def explore_discard(config: BrowserSetup, task: str) -> dict[str, Any]:
    """Drop the task's in-progress draft."""
    return {"discarded": discard_draft(config.config_dir, task), "ok": True, "task": task}


def explore_observe(config: BrowserSetup, task: str, page_id: str) -> dict[str, Any]:
    """Add the page the task's browser is on to the draft (continuing a stored model)."""
    hosts, extra_deny = explore_policy(config, task)
    profile = require_profile(config.config_dir, task)
    with contextlib.closing(attach_exploring(profile, allowed_hosts=hosts)) as driver:
        explorer = Explorer(driver, TerminalConfirmer(), allowed_hosts=hosts, extra_deny=extra_deny)
        url, controls = explorer.observe_with_url()
    seen: dict[str, Any] = {}

    def change(current: SiteModel | None) -> SiteModel:
        draft = (
            ModelDraft.from_model(current, extra_deny)
            if current is not None
            else ModelDraft(task=task, hosts=(), extra_deny=extra_deny)
        )
        draft.add_host(urlsplit(url).hostname or "")
        draft.add_page(page_id, url, controls)
        model = _to_model(draft)
        page = next(p for p in model.pages if p.page_id == page_id)
        seen.update(controls=len(page.controls), skipped=draft.skipped, template=page.url_template)
        return model

    update_draft(config.config_dir, task, change)
    return {
        "controls": seen["controls"],
        "ok": True,
        "page": page_id,
        "skipped": seen["skipped"],
        "task": task,
        "url_template": seen["template"],
    }


def explore_policy(config: BrowserSetup, task: str) -> tuple[Sequence[str], Sequence[str]]:
    validate_task_name(task)
    declared = config.browser.tasks.get(task)
    if declared is None or not declared.allowed_hosts:
        raise ExploreRefusedError(
            f"Expected [browser.tasks.{task}] to declare allowed_hosts in config.toml before "
            "exploring, got none"
        )
    return declared.allowed_hosts, declared.extra_deny_names


def explore_save(config: BrowserSetup, task: str, *, yes: bool) -> dict[str, Any]:
    """Store the draft as the next version of the task's model, once the user has said yes."""
    hosts, _ = explore_policy(config, task)
    draft = _require_draft(config, task)
    if not yes:
        raise ExploreNeedsConfirmationError(
            f"Expected --yes to save this exploration as the model for {task!r}, got none. "
            f"{_describe(draft)} Run `blumkin browser explore show {task}` to review it, then "
            f"`blumkin browser explore save {task} --yes`"
        )
    saved = save_model(config.config_dir, draft, host_allowed=lambda h: host_allowed(h, hosts))
    discard_draft(config.config_dir, task)
    return {"ok": True, "pages": len(saved.pages), "task": task, "version": saved.version}


def explore_show(config: BrowserSetup, task: str) -> dict[str, Any]:
    """Summarize the task's draft: what would be saved."""
    draft = load_draft(config.config_dir, task)
    if draft is None:
        return {"hosts": [], "ok": True, "pages": [], "steps": 0, "task": task}
    return {
        "hosts": list(draft.hosts),
        "ok": True,
        "pages": [
            {
                "controls": len(page.controls),
                "finalizing": [c.name for c in page.controls if c.safety.value == "finalizing"],
                "page_id": page.page_id,
                "url_template": page.url_template,
            }
            for page in draft.pages
        ],
        "steps": sum(len(flow.steps) for flow in draft.flows),
        "task": task,
    }


def _click_hint(
    task: str, name: str, role: str, outcome: str, *, unavailable: bool, timed_out: bool
) -> str | None:
    if outcome == "clicked":
        return None
    if outcome == "record_only":
        return (
            "Recorded as metadata only: blumkin never clicks a finalizing control or a "
            "control that is not a link, tab, menu item or state-changing control."
        )
    if outcome.startswith("refused: "):
        return f"Not clicked: {outcome.removeprefix('refused: ')}."
    if unavailable:
        return (
            "There is no terminal to ask on, so nothing was clicked. To approve this click, "
            f"run `blumkin browser explore click {task} {shlex.quote(name)} --role {role}` "
            "yourself in a terminal."
        )
    if timed_out:
        return "Not clicked: no answer came from the terminal in time."
    return "Not clicked: the answer was no."


def _describe(draft: SiteModel) -> str:
    controls = sum(len(page.controls) for page in draft.pages)
    return f"The draft has {len(draft.pages)} page(s) and {controls} control(s)."


def _record_step(config: BrowserSetup, task: str, page_id: str, role: str, name: str) -> bool:
    recorded: list[bool] = []

    def change(current: SiteModel | None) -> SiteModel | None:
        if current is None:
            return None
        draft = ModelDraft.from_model(current)
        control = draft.control_for(page_id, role, name)
        if control is None:
            return None
        draft.add_step("click", page_id=page_id, control=control)
        recorded.append(True)
        return _to_model(draft)

    update_draft(config.config_dir, task, change)
    return bool(recorded)


def _require_draft(config: BrowserSetup, task: str) -> SiteModel:
    draft = load_draft(config.config_dir, task)
    if draft is None:
        raise DraftNotFoundError(f"Expected an exploration draft for task {task!r}, got none")
    return draft


def _to_model(draft: ModelDraft) -> SiteModel:
    return draft.to_model(
        recorded_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        blumkin_version=re.sub(r"[^0-9A-Za-z.+-]", "-", __version__)[:40] or "unknown",
    )
