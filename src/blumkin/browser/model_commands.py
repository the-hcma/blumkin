"""Payloads behind the `blumkin browser model` commands (issue #487).

`check` grades one page of the stored model against the page the task's browser is on now
(read-only: it observes, never clicks), `versions` lists the saved versions and `diff`
compares two of them.
"""

from __future__ import annotations

import contextlib
from typing import Any

from blumkin.browser.commands import require_profile
from blumkin.browser.drift import (
    Finding,
    FindingKind,
    Grade,
    ModelDiff,
    check_page,
    diff_models,
    has_thin_fingerprint,
    worst,
)
from blumkin.browser.explore import (
    Explorer,
    PageIdError,
    TerminalConfirmer,
    attach_exploring,
    url_template,
)
from blumkin.browser.explore_commands import explore_policy
from blumkin.browser.model import (
    SiteModel,
    list_versions,
    load_model,
    load_version,
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


def model_versions(config: BrowserSetup, task: str) -> dict[str, Any]:
    """The saved versions of the task's model."""
    versions = list_versions(config.config_dir, task)
    return {
        "current": versions[-1] if versions else None,
        "ok": True,
        "task": task,
        "versions": versions,
    }


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


def _stored(config: BrowserSetup, task: str, version: int | None) -> SiteModel:
    if version is not None:
        return _version(config, task, version)
    model = load_model(config.config_dir, task)
    if model is None:
        raise ModelNotFoundError(f"Expected a saved model for task {task!r}, got none")
    return model


def _version(config: BrowserSetup, task: str, version: int) -> SiteModel:
    if version not in list_versions(config.config_dir, task):
        raise ModelNotFoundError(f"Expected version {version} of the {task!r} model, got none")
    return load_version(config.config_dir, task, version)
