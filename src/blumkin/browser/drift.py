"""Has the website changed since the site model was recorded? (issue #487).

The check compares one recorded page with what the browser shows now, using only roles and
accessible names (a Playwright ARIA snapshot), so layout, colors and ordering never count:

- `compatible`: every control resolves through its first verifiable locator and the page is
  still recognizably the same;
- `degraded`: it still works but the model should be updated: a control is found only by a
  fallback locator, cannot be verified from a snapshot (a selector-only control), or some of
  the page's landmarks changed;
- `incompatible`: re-explore this page: a control is gone, a new field appeared (it may be
  required) or the page no longer looks like the recorded one.

A new button or link is not drift. Nothing here touches the page: the caller supplies what
was observed, and `diff_models` compares two stored versions of a model.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from blumkin.browser.explore import ControlObservation, landmark_marks
from blumkin.browser.model import (
    Control,
    LocatorStrategy,
    Page,
    SiteModel,
    is_storable_label,
)


class FindingKind(StrEnum):
    """What a finding is about."""

    FALLBACK = "fallback"
    LOOKS_DIFFERENT = "looks_different"
    MISSING = "missing"
    NEW_FIELD = "new_field"
    UNVERIFIED = "unverified"


class Grade(StrEnum):
    """How well the recorded page still matches the live one, best first."""

    COMPATIBLE = "compatible"
    DEGRADED = "degraded"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing the check noticed, with what to do about it."""

    control: str | None
    grade: Grade
    kind: FindingKind
    note: str


@dataclass(frozen=True, slots=True)
class ModelDiff:
    """What differs between two versions of a model."""

    controls_added: dict[str, tuple[str, ...]]
    controls_changed: dict[str, tuple[str, ...]]
    controls_removed: dict[str, tuple[str, ...]]
    hosts_added: tuple[str, ...]
    hosts_removed: tuple[str, ...]
    pages_added: tuple[str, ...]
    pages_removed: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not (
            self.controls_added
            or self.controls_changed
            or self.controls_removed
            or self.hosts_added
            or self.hosts_removed
            or self.pages_added
            or self.pages_removed
        )

    def lines(self) -> list[str]:
        """The diff as plain lines, one change each."""
        if self.empty:
            return ["no differences"]
        out = [f"host added: {host}" for host in self.hosts_added]
        out += [f"host removed: {host}" for host in self.hosts_removed]
        out += [f"page added: {page}" for page in self.pages_added]
        out += [f"page removed: {page}" for page in self.pages_removed]
        for label, changes in (
            ("added", self.controls_added),
            ("changed", self.controls_changed),
            ("removed", self.controls_removed),
        ):
            for page, names in changes.items():
                out += [f"control {label} on {page}: {name}" for name in names]
        return out


@dataclass(frozen=True, slots=True)
class PageCheck:
    """The grade for one page and the findings behind it."""

    findings: tuple[Finding, ...]
    grade: Grade
    page_id: str


def check_page(page: Page, observed: Sequence[ControlObservation]) -> PageCheck:
    """Grade the recorded `page` against what was `observed` on the live page."""
    live = {(item.role, item.name) for item in observed if is_storable_label(item.name)}
    findings: list[Finding] = []
    for control in page.controls:
        findings.extend(_control_findings(control, live))
    findings.extend(_new_field_findings(page, observed))
    findings.extend(_appearance_findings(page, observed))
    grade = worst(finding.grade for finding in findings)
    return PageCheck(findings=tuple(findings), grade=grade, page_id=page.page_id)


def diff_models(old: SiteModel, new: SiteModel) -> ModelDiff:
    """What changed from `old` to `new`: hosts, pages, and controls on the pages both have."""
    old_pages = {page.page_id: page for page in old.pages}
    new_pages = {page.page_id: page for page in new.pages}
    added: dict[str, tuple[str, ...]] = {}
    changed: dict[str, tuple[str, ...]] = {}
    removed: dict[str, tuple[str, ...]] = {}
    for page_id in old_pages.keys() & new_pages.keys():
        before = {c.name: c for c in old_pages[page_id].controls}
        after = {c.name: c for c in new_pages[page_id].controls}
        if names := tuple(sorted(after.keys() - before.keys())):
            added[page_id] = names
        if names := tuple(sorted(before.keys() - after.keys())):
            removed[page_id] = names
        if names := tuple(sorted(n for n in before.keys() & after.keys() if before[n] != after[n])):
            changed[page_id] = names
    return ModelDiff(
        controls_added=added,
        controls_changed=changed,
        controls_removed=removed,
        hosts_added=tuple(sorted(set(new.hosts) - set(old.hosts))),
        hosts_removed=tuple(sorted(set(old.hosts) - set(new.hosts))),
        pages_added=tuple(sorted(new_pages.keys() - old_pages.keys())),
        pages_removed=tuple(sorted(old_pages.keys() - new_pages.keys())),
    )


def worst(grades: Iterable[Grade]) -> Grade:
    """The worst of `grades` (compatible when there are none)."""
    return max(grades, key=_RANK.__getitem__, default=Grade.COMPATIBLE)


_FIELD_ROLES = frozenset(
    {"checkbox", "combobox", "listbox", "radio", "searchbox", "spinbutton", "switch", "textbox"}
)
_LABELLED_ROLES = frozenset({"combobox", "listbox", "searchbox", "spinbutton", "textbox"})
_RANK = {Grade.COMPATIBLE: 0, Grade.DEGRADED: 1, Grade.INCOMPATIBLE: 2}
#: A page whose recorded landmarks are mostly gone is no longer the recorded page.
_SAME_PAGE = 0.5
#: Below this share of the recorded landmarks, the page changed enough to review the model.
_UNCHANGED = 0.8


def _appearance_findings(page: Page, observed: Sequence[ControlObservation]) -> list[Finding]:
    recorded = set(page.fingerprint)
    if not recorded:
        return []
    share = len(recorded & landmark_marks(observed)) / len(recorded)
    if share < _SAME_PAGE:
        return [
            Finding(
                control=None,
                grade=Grade.INCOMPATIBLE,
                kind=FindingKind.LOOKS_DIFFERENT,
                note=(
                    f"Only {share:.0%} of the recorded landmarks of {page.page_id} are on the "
                    "page; it may be another page or not loaded, so check the address and "
                    "re-explore it"
                ),
            )
        ]
    if share < _UNCHANGED:
        return [
            Finding(
                control=None,
                grade=Grade.DEGRADED,
                kind=FindingKind.LOOKS_DIFFERENT,
                note=(
                    f"{share:.0%} of the recorded landmarks of {page.page_id} are still on the "
                    "page; review the model and re-explore it if it matters"
                ),
            )
        ]
    return []


def _control_findings(control: Control, live: set[tuple[str, str]]) -> list[Finding]:
    verdicts = [_locator_verdict(loc.strategy, loc.value, live) for loc in control.locators]
    verifiable = [v for v in verdicts if v is not None]
    if not verifiable:
        return [
            Finding(
                control=control.name,
                grade=Grade.DEGRADED,
                kind=FindingKind.UNVERIFIED,
                note=(
                    f"{control.name} has only selector locators, which a page snapshot cannot "
                    "check; re-record it with a role and name locator"
                ),
            )
        ]
    if not any(verifiable):
        return [
            Finding(
                control=control.name,
                grade=Grade.INCOMPATIBLE,
                kind=FindingKind.MISSING,
                note=f"{control.name} is no longer on the page; re-explore this page",
            )
        ]
    if not verifiable[0]:
        return [
            Finding(
                control=control.name,
                grade=Grade.DEGRADED,
                kind=FindingKind.FALLBACK,
                note=(
                    f"{control.name} is found only by a fallback locator; update the model "
                    "(re-explore this page)"
                ),
            )
        ]
    return []


def _locator_verdict(
    strategy: LocatorStrategy, value: str, live: set[tuple[str, str]]
) -> bool | None:
    """Whether a locator finds its control on the live page; None when a snapshot cannot say."""
    if strategy is LocatorStrategy.ROLE_NAME:
        role, _, name = value.partition(":")
        return (role, name) in live
    if strategy is LocatorStrategy.LABEL:
        return any(role in _LABELLED_ROLES and name == value for role, name in live)
    return None


def _new_field_findings(page: Page, observed: Sequence[ControlObservation]) -> list[Finding]:
    known_role_names: set[tuple[str, str]] = set()
    known_labels: set[str] = set()
    for control in page.controls:
        for locator in control.locators:
            if locator.strategy is LocatorStrategy.ROLE_NAME:
                role, _, name = locator.value.partition(":")
                known_role_names.add((role, name))
            elif locator.strategy is LocatorStrategy.LABEL:
                known_labels.add(locator.value)
    fresh = [
        f"{item.role} {item.name!r}"
        for item in observed
        if item.role in _FIELD_ROLES
        and is_storable_label(item.name)
        and (item.role, item.name) not in known_role_names
        and not (item.role in _LABELLED_ROLES and item.name in known_labels)
    ]
    if not fresh:
        return []
    shown = ", ".join(fresh[:5]) + (" and more" if len(fresh) > 5 else "")
    return [
        Finding(
            control=None,
            grade=Grade.INCOMPATIBLE,
            kind=FindingKind.NEW_FIELD,
            note=f"New field(s) not in the model: {shown}; one may be required, so re-explore",
        )
    ]
