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
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from blumkin import __version__
from blumkin.browser.actions import host_allowed, sign_in_hosts_for
from blumkin.browser.commands import require_profile
from blumkin.browser.explore import (
    DraftNotFoundError,
    ExploreNeedsConfirmationError,
    Explorer,
    ExploreRefusedError,
    ModelDraft,
    PageIdError,
    TerminalConfirmer,
    attach_exploring,
    decide_click,
)
from blumkin.browser.guard import validate_control_patterns, validate_dialog_landmarks
from blumkin.browser.model import (
    SiteModel,
    discard_draft,
    is_logical_name,
    load_draft,
    load_model,
    resolve_store,
    save_model,
    update_draft,
)
from blumkin.browser.profiles import validate_task_name
from blumkin.config import BrowserSetup


def declared_controls(config: BrowserSetup, task: str) -> tuple[str, ...]:
    """The control names the task declared in `allowed_controls` (none when not configured)."""
    declared = config.browser.tasks.get(task)
    return tuple(declared.allowed_controls) if declared is not None else ()


def explore_click(
    config: BrowserSetup, task: str, name: str, *, page_id: str | None, role: str
) -> dict[str, Any]:
    """Follow or press one control under the exploration rules; see `Explorer.click`."""
    hosts, extra_deny = explore_policy(config, task)
    declared = declared_controls(config, task)
    # The same refusal `browser click` gives a pattern that is too loose, before the browser.
    validate_control_patterns(declared, extra_deny)
    profile = require_profile(config.config_dir, task)
    # A step is recorded after the click, so a models folder that would be refused must be
    # found out before the browser is touched, not after a state-changing click.
    store = resolve_store(config) if page_id is not None else None
    confirmer = TerminalConfirmer()
    decision = decide_click(role, name, extra_deny=extra_deny)
    with contextlib.closing(attach_exploring(profile, allowed_hosts=hosts)) as driver:
        driver.mark_window(task, hosts, sign_in_hosts_for(config, task))
        explorer = Explorer(
            driver,
            confirmer,
            allowed_hosts=hosts,
            declared_controls=declared,
            extra_deny=extra_deny,
        )
        clicked = explorer.click(role, name)
        driver.mark_window(task, hosts, sign_in_hosts_for(config, task))
    outcome = explorer.last_outcome
    unavailable = bool(getattr(confirmer, "unavailable", False))
    timed_out = bool(getattr(confirmer, "timed_out", False))
    recorded = (
        clicked
        and page_id is not None
        and store is not None
        and _record_step(store, task, page_id, role, name)
    )
    return {
        "approved_by": explorer.last_approval or None,
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
    return {"discarded": discard_draft(resolve_store(config), task), "ok": True, "task": task}


def explore_forget(config: BrowserSetup, task: str, page_id: str) -> dict[str, Any]:
    """Drop one page (and the recorded steps that used it) from the task's draft.

    Without a draft the stored model is continued, as `observe` does, so a page of the saved
    model can be removed in the next version. The last page cannot be forgotten: that is what
    `discard` is for.
    """
    _, extra_deny = explore_policy(config, task)
    if not is_logical_name(page_id):
        # A stored model may hold ids exploration would not make (`expense-item-2`).
        raise PageIdError(f"Expected the id of a page in the draft or model, got {page_id!r}")
    seen: dict[str, Any] = {"forgotten": False, "steps_dropped": {}}

    def change(current: SiteModel | None) -> SiteModel | None:
        if current is None:
            return None
        draft = ModelDraft.from_model(current, extra_deny)
        dropped = draft.forget(page_id)
        if dropped is None:
            return None
        if not any(True for _ in draft.page_ids()):
            raise ExploreRefusedError(
                "Expected a draft that keeps at least one page, got none; use "
                "`explore discard` to drop the whole draft"
            )
        seen["forgotten"] = True
        seen["steps_dropped"] = dropped
        return _to_model(draft)

    update_draft(resolve_store(config), task, change)
    payload: dict[str, Any] = {
        "forgotten": seen["forgotten"],
        "ok": True,
        "page": page_id,
        "task": task,
    }
    if seen["steps_dropped"]:
        payload["steps_dropped"] = seen["steps_dropped"]
        payload["warnings"] = [
            f"flow {name} lost {count} step(s) that used {page_id}; check that it still makes sense"
            for name, count in seen["steps_dropped"].items()
        ]
    return payload


def explore_observe(
    config: BrowserSetup,
    task: str,
    page_id: str,
    *,
    kind: str | None = None,
    landmarks: Sequence[str] = (),
) -> dict[str, Any]:
    """Add the page the task's browser is on to the draft (continuing a stored model).

    Without `kind`, a page that is already recorded keeps its kind and landmarks.

    `kind` says what the page is: an ordinary `page`, a `dialog` (a popup the page does not
    mark as one, such as an Oracle ADF attachments popup) or a `list-of-values` dialog whose
    rows are chosen by their text. A `dialog` records its `landmarks`, at least two controls
    seen on it that are not finalizing actions, which are what the task's `dialog_landmarks`
    policy key takes (`explore show` prints it).
    """
    if landmarks and kind is not None and kind != "dialog":
        raise PageIdError("Expected --landmark only with --kind dialog, got it with another kind")
    if kind == "dialog" and not landmarks and not _recorded_dialog(config, task, page_id):
        raise PageIdError("Expected at least two --landmark controls for a dialog page, got none")
    hosts, extra_deny = explore_policy(config, task)
    if landmarks:
        validate_dialog_landmarks(landmarks, extra_deny)
    profile = require_profile(config.config_dir, task)
    with contextlib.closing(attach_exploring(profile, allowed_hosts=hosts)) as driver:
        driver.mark_window(task, hosts, sign_in_hosts_for(config, task))
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
        draft.add_page(page_id, url, controls, page_kind=kind, landmarks=landmarks)
        model = _to_model(draft)
        page = next(p for p in model.pages if p.page_id == page_id)
        seen.update(
            controls=len(page.controls),
            kind=page.kind,
            skipped=draft.skipped,
            template=page.url_template,
        )
        seen["shared"] = {
            address: list(ids)
            for address, ids in draft.shared_addresses().items()
            if page_id in ids
        }
        return model

    update_draft(resolve_store(config), task, change)
    payload: dict[str, Any] = {
        "controls": seen["controls"],
        "kind": seen["kind"],
        "ok": True,
        "page": page_id,
        "skipped": seen["skipped"],
        "task": task,
        "url_template": seen["template"],
    }
    if seen["shared"]:
        payload["shared_addresses"] = seen["shared"]
        payload["warnings"] = [
            f"pages {', '.join(ids)} share the address {address} and differ by landmarks only: "
            "the address cannot tell them apart, so `model check` compares what is on the page"
            for address, ids in seen["shared"].items()
        ]
    return payload


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
    saved = save_model(resolve_store(config), draft, host_allowed=lambda h: host_allowed(h, hosts))
    discard_draft(resolve_store(config), task)
    return {"ok": True, "pages": len(saved.pages), "task": task, "version": saved.version}


def _recorded_dialog(config: BrowserSetup, task: str, page_id: str) -> bool:
    """Whether the draft (or the stored model) already holds `page_id` as a dialog."""
    current = load_draft(resolve_store(config), task) or load_model(resolve_store(config), task)
    return current is not None and any(
        page.page_id == page_id and page.kind == "dialog" for page in current.pages
    )


def explore_show(config: BrowserSetup, task: str) -> dict[str, Any]:
    """Summarize the task's draft: what would be saved."""
    draft = load_draft(resolve_store(config), task)
    if draft is None:
        return {"hosts": [], "ok": True, "pages": [], "steps": 0, "task": task}
    return {
        "hosts": list(draft.hosts),
        "ok": True,
        "pages": [
            {
                "controls": len(page.controls),
                "finalizing": [c.name for c in page.controls if c.safety.value == "finalizing"],
                "kind": page.kind,
                "page_id": page.page_id,
                "url_template": page.url_template,
            }
            | ({"dialog_landmarks": list(page.landmarks)} if page.landmarks else {})
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
            "yourself in a terminal, or declare the control in "
            f"[browser.tasks.{task}] allowed_controls in config.toml and run it again: "
            "a declared control is clicked without a question (never a finalizing one)."
        )
    if timed_out:
        return "Not clicked: no answer came from the terminal in time."
    return "Not clicked: the answer was no."


def _describe(draft: SiteModel) -> str:
    controls = sum(len(page.controls) for page in draft.pages)
    return f"The draft has {len(draft.pages)} page(s) and {controls} control(s)."


def require_recordable_click(config: BrowserSetup, task: str, page_id: str, name: str) -> None:
    """Refuse, before a click is made, when it could not be recorded as a step of `page_id`.

    The page must be in the draft (or the stored model that `observe` continues) and hold a
    control of that accessible name, so a click is never made and then found not recordable.
    """
    store = resolve_store(config)
    current = load_draft(store, task) or load_model(store, task)
    if current is None:
        raise DraftNotFoundError(
            f"Expected an exploration draft or a saved model for task {task!r} to record the "
            "click in, got none; observe the page first"
        )
    if not ModelDraft.from_model(current).has_control_named(page_id, name):
        raise PageIdError(
            f"Expected page {page_id!r} to hold a control named {name!r} to record the click "
            "as, got none; observe the page first (`blumkin browser explore observe`)"
        )


def record_browser_click(
    config: BrowserSetup,
    task: str,
    page_id: str,
    role: str,
    name: str,
    *,
    expect_heading: str | None = None,
) -> bool:
    """Record a click that `browser click` made as the next step of the recorded flow."""
    return _record_step(
        resolve_store(config), task, page_id, role, name, expect_heading=expect_heading
    )


def _record_step(
    store: Path,
    task: str,
    page_id: str,
    role: str,
    name: str,
    *,
    expect_heading: str | None = None,
) -> bool:
    recorded: list[bool] = []

    def change(current: SiteModel | None) -> SiteModel | None:
        if current is None:
            return None
        draft = ModelDraft.from_model(current)
        control = draft.control_for(page_id, role, name)
        if control is None:
            return None
        draft.add_step("click", page_id=page_id, control=control, expect_heading=expect_heading)
        recorded.append(True)
        return _to_model(draft)

    update_draft(store, task, change)
    return bool(recorded)


def _require_draft(config: BrowserSetup, task: str) -> SiteModel:
    draft = load_draft(resolve_store(config), task)
    if draft is None:
        raise DraftNotFoundError(f"Expected an exploration draft for task {task!r}, got none")
    return draft


def _to_model(draft: ModelDraft) -> SiteModel:
    return draft.to_model(
        recorded_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        blumkin_version=re.sub(r"[^0-9A-Za-z.+-]", "-", __version__)[:40] or "unknown",
    )
