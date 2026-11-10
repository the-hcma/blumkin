"""Payloads behind the `blumkin browser model` commands (issue #487).

`check` grades one page of the stored model against the page the task's browser is on now
(read-only: it observes, never clicks), `versions` lists the saved versions and `diff`
compares two of them.
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Sequence
from typing import Any

from blumkin.browser.actions import host_allowed, sign_in_hosts_for
from blumkin.browser.commands import require_profile
from blumkin.browser.drift import (
    Finding,
    FindingKind,
    Grade,
    ModelDiff,
    best_matching_page,
    check_page,
    diff_models,
    has_thin_fingerprint,
    worst,
)
from blumkin.browser.explore import (
    ControlObservation,
    Explorer,
    PageIdError,
    TerminalConfirmer,
    attach_exploring,
    url_template,
)
from blumkin.browser.explore_commands import explore_policy
from blumkin.browser.flow_authoring import FlowSpecError, build_flow
from blumkin.browser.model import (
    Flow,
    Page,
    SiteModel,
    check_model,
    list_versions,
    load_model,
    load_version,
    resolve_store,
    save_model,
)
from blumkin.config import BrowserSetup


class ModelNotFoundError(LookupError):
    """The task has no saved model (or no such version of it)."""


def model_check(
    config: BrowserSetup, task: str, page_id: str, *, version: int | None = None
) -> dict[str, Any]:
    """Grade a page of the stored model against the page the task's browser is on."""
    hosts, extra_deny = explore_policy(config, task)
    model = _stored(config, task, version)
    page = next((p for p in model.pages if p.page_id == page_id), None)
    if page is None:
        known = ", ".join(p.page_id for p in model.pages)
        raise PageIdError(f"Expected one of the model's pages ({known}), got {page_id!r}")
    profile = require_profile(config.config_dir, task)
    with contextlib.closing(attach_exploring(profile, allowed_hosts=hosts)) as driver:
        driver.mark_window(task, hosts, sign_in_hosts_for(config, task))
        explorer = Explorer(driver, TerminalConfirmer(), allowed_hosts=hosts, extra_deny=extra_deny)
        url, controls = explorer.observe_with_url()
    check = check_page(page, controls)
    template = url_template(url)
    findings = list(check.findings)
    if template != page.url_template:
        findings.append(
            Finding(
                control=None,
                grade=Grade.INCOMPATIBLE if has_thin_fingerprint(page) else Grade.DEGRADED,
                kind=FindingKind.LOOKS_DIFFERENT,
                note=(
                    f"The browser is at a different address shape than the recorded "
                    f"{page.url_template}; check that it is on the {page_id} page"
                ),
            )
        )
    grade = worst([check.grade, *(f.grade for f in findings)])
    return {
        "findings": [
            {"control": f.control, "grade": f.grade.value, "kind": f.kind.value, "note": f.note}
            for f in findings
        ],
        "best_match": _best_match(model.pages, page_id, controls),
        "grade": grade.value,
        "ok": grade is not Grade.INCOMPATIBLE,
        "page": page_id,
        "task": task,
        "url_matches": template == page.url_template,
        "url_template": template,
        "version": model.version,
    }


def model_diff(config: BrowserSetup, task: str, old: int, new: int | None) -> dict[str, Any]:
    """What changed between two saved versions (`new` defaults to the current one)."""
    before = _version(config, task, old)
    after = _stored(config, task, new)
    return _diff_payload(task, old, after.version, diff_models(before, after))


def model_flow_add(
    config: BrowserSetup,
    task: str,
    name: str,
    *,
    params: Sequence[str],
    replace: bool,
    steps: Sequence[str],
    yes: bool,
) -> dict[str, Any]:
    """Add a flow written as specs to the stored model; only with `yes` is a version saved.

    The flow is built from its parameter and step specs, put into a copy of the current model
    and checked as any model is (its steps must name controls and pages the model records, its
    bindings must be declared parameters, a handoff names no control), so a spec cannot say what
    a recorded flow could not. Without `yes` that check is all that happens and nothing is
    written. A flow of the same name is replaced only with `replace`.
    """
    hosts, _ = explore_policy(config, task)
    model = _stored(config, task, None)
    flow = build_flow(name, list(params), list(steps))
    exists = any(existing.name == name for existing in model.flows)
    if exists and not replace:
        raise FlowSpecError(
            f"Expected a new flow name, got {name!r} which the model already has; "
            "add --replace to replace it"
        )
    _refuse_finalizing_clicks(model, flow)
    # A replaced flow keeps its place, so `model diff` shows the change and not a reordering;
    # the pages were not re-recorded, so their `recorded_at` and version stay as they were.
    flows = tuple(flow if existing.name == name else existing for existing in model.flows)
    candidate = dataclasses.replace(model, flows=flows if exists else (*flows, flow))
    allowed = lambda host: host_allowed(host, hosts)  # noqa: E731
    check_model(candidate, host_allowed=allowed)
    payload: dict[str, Any] = {
        "action": "model-flow-add",
        "flow": name,
        "ok": True,
        "params": [param.name for param in flow.params],
        "replaced": exists,
        "steps": len(flow.steps),
        "task": task,
    }
    if not yes:
        return payload | {"saved": False, "version": None}
    saved = save_model(
        resolve_store(config), candidate, base_version=model.version, host_allowed=allowed
    )
    return payload | {"saved": True, "version": saved.version}


def model_flows(config: BrowserSetup, task: str, name: str | None = None) -> dict[str, Any]:
    """The flows of the task's current model, with their parameters and steps (one by `name`)."""
    model = _stored(config, task, None)
    flows = [flow for flow in model.flows if name is None or flow.name == name]
    if name is not None and not flows:
        known = ", ".join(flow.name for flow in model.flows) or "none"
        raise FlowSpecError(f"Expected one of the model's flows ({known}), got {name!r}")
    return {
        "flows": [
            {
                "name": flow.name,
                "params": [
                    {
                        key: value
                        for key, value in dataclasses.asdict(param).items()
                        if value not in (None, ())
                    }
                    for param in flow.params
                ],
                "steps": [
                    {
                        key: value
                        for key, value in dataclasses.asdict(step).items()
                        if value is not None
                    }
                    for step in flow.steps
                ],
            }
            for flow in flows
        ],
        "ok": True,
        "task": task,
        "version": model.version,
    }


def model_versions(config: BrowserSetup, task: str) -> dict[str, Any]:
    """The saved versions of the task's model."""
    store = resolve_store(config)
    versions = list_versions(store, task)
    return {
        "current": versions[-1] if versions else None,
        "ok": True,
        "store": str(store),
        "task": task,
        "versions": versions,
    }


def _best_match(
    pages: Sequence[Page], asked: str, controls: Sequence[ControlObservation]
) -> dict[str, Any] | None:
    """The recorded page the live page matches best, when that is not the one asked about."""
    best = best_matching_page(pages, controls, prefer=asked)
    if best is None or best[0] == asked:
        return None
    return {"page": best[0], "share": round(best[1], 2)}


def _diff_payload(task: str, old: int, new: int, diff: ModelDiff) -> dict[str, Any]:
    return {
        "controls_added": {page: list(names) for page, names in diff.controls_added.items()},
        "controls_changed": {page: list(names) for page, names in diff.controls_changed.items()},
        "controls_removed": {page: list(names) for page, names in diff.controls_removed.items()},
        "empty": diff.empty,
        "flows_changed": list(diff.flows_changed),
        "hosts_added": list(diff.hosts_added),
        "hosts_removed": list(diff.hosts_removed),
        "lines": diff.lines(),
        "new": new,
        "ok": True,
        "old": old,
        "pages_added": list(diff.pages_added),
        "pages_changed": list(diff.pages_changed),
        "pages_removed": list(diff.pages_removed),
        "safety_changed": {page: list(names) for page, names in diff.safety_changed.items()},
        "task": task,
    }


def _refuse_finalizing_clicks(model: SiteModel, flow: Flow) -> None:
    """A flow cannot click or press a control the model records as finalizing.

    A recorded flow only holds clicks that were made through the guard, so it never has one; a
    written one could, and `click` would refuse it only when the flow runs.
    """
    finalizing = {
        (page.page_id, control.name)
        for page in model.pages
        for control in page.controls
        if control.safety.value == "finalizing"
    }
    for number, step in enumerate(flow.steps, start=1):
        if step.action not in ("click", "press") or step.control is None:
            continue
        # A step that names its page is judged by that page's control (names repeat across
        # pages); one that does not is judged by every page, so none can slip through.
        hit = (
            (step.page, step.control) in finalizing
            if step.page is not None
            else any(name == step.control for _page, name in finalizing)
        )
        if hit:
            raise FlowSpecError(
                f"Expected step {number} not to click {step.control!r}, got a control the "
                "model records as finalizing; blumkin never submits, so hand that step to the "
                "person with a handoff step"
            )


def _stored(config: BrowserSetup, task: str, version: int | None) -> SiteModel:
    if version is not None:
        return _version(config, task, version)
    model = load_model(resolve_store(config), task)
    if model is None:
        raise ModelNotFoundError(f"Expected a saved model for task {task!r}, got none")
    return model


def _version(config: BrowserSetup, task: str, version: int) -> SiteModel:
    if version not in list_versions(resolve_store(config), task):
        raise ModelNotFoundError(f"Expected version {version} of the {task!r} model, got none")
    return load_version(resolve_store(config), task, version)
