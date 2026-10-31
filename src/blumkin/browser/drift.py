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

from blumkin.browser.explore import (
    LABELLED_ROLES,
    MAX_DRAFT_CONTROLS,
    ControlObservation,
    landmark_marks,
)
from blumkin.browser.model import (
    Control,
    LocatorStrategy,
    Page,
    SiteModel,
    is_storable_label,
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing the check noticed, with what to do about it."""

    control: str | None
    grade: Grade
    kind: FindingKind
    note: str


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
class ModelDiff:
    """What differs between two versions of a model."""

    controls_added: dict[str, tuple[str, ...]]
    controls_changed: dict[str, tuple[str, ...]]
    controls_removed: dict[str, tuple[str, ...]]
    flows_changed: tuple[str, ...]
    hosts_added: tuple[str, ...]
    hosts_removed: tuple[str, ...]
    pages_added: tuple[str, ...]
    pages_changed: tuple[str, ...]
    pages_removed: tuple[str, ...]
    safety_changed: dict[str, tuple[str, ...]]
    #: Lines for a changed safety flag, naming the flip; filled by `diff_models`.
    safety_lines: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (
            self.controls_added
            or self.controls_changed
            or self.controls_removed
            or self.flows_changed
            or self.hosts_added
            or self.hosts_removed
            or self.pages_added
            or self.pages_changed
            or self.pages_removed
            or self.safety_changed
        )

    def lines(self) -> list[str]:
        """The diff as plain lines, one change each."""
        if self.empty:
            return ["no differences"]
        out = [f"host added: {host}" for host in self.hosts_added]
        out += [f"host removed: {host}" for host in self.hosts_removed]
        out += [f"page added: {page}" for page in self.pages_added]
        out += [f"page removed: {page}" for page in self.pages_removed]
        out += [f"page changed: {page}" for page in self.pages_changed]
        out += [f"flow changed: {name}" for name in self.flows_changed]
        out += list(self.safety_lines)
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


def best_matching_page(
    pages: Sequence[Page], observed: Sequence[ControlObservation], prefer: str | None = None
) -> tuple[str, float] | None:
    """The page whose recorded landmarks are most on the live page: (page id, share).

    Only pages with enough landmarks to tell them apart count, and the best must reach the
    share at which two pages are the same page; with none that does, there is no best match.
    Some sites serve every page from one address, so this is how a caller learns which of
    the recorded pages the browser is actually on. A page whose few landmarks are all on a
    richer page scores the same as that page, so a tie goes to `prefer` (the page the caller
    asked about), then to the page with more landmarks.
    """
    live = {_canonical(mark) for mark in landmark_marks(observed) if not mark.startswith("link:")}
    best: tuple[str, float] | None = None
    best_key: tuple[float, bool, int] = (0.0, False, 0)
    for page in pages:
        recorded = {_canonical(mark) for mark in page.fingerprint if not mark.startswith("link:")}
        if len(recorded) < _MIN_LANDMARKS:
            continue
        share = len(recorded & live) / len(recorded)
        key = (share, page.page_id == prefer, len(recorded))
        if share >= _SAME_PAGE and (best is None or key > best_key):
            best, best_key = (page.page_id, share), key
    return best


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
    """What changed from `old` to `new`: hosts, pages, flows, and the controls on shared pages."""
    old_pages = {page.page_id: page for page in old.pages}
    new_pages = {page.page_id: page for page in new.pages}
    added: dict[str, tuple[str, ...]] = {}
    changed: dict[str, tuple[str, ...]] = {}
    removed: dict[str, tuple[str, ...]] = {}
    flipped: dict[str, tuple[str, ...]] = {}
    flip_lines: list[str] = []
    page_changes: list[str] = []
    for page_id in sorted(old_pages.keys() & new_pages.keys()):
        before_page, after_page = old_pages[page_id], new_pages[page_id]
        if (
            before_page.url_template != after_page.url_template
            or before_page.fingerprint != after_page.fingerprint
        ):
            page_changes.append(page_id)
        before = {c.name: c for c in before_page.controls}
        after = {c.name: c for c in after_page.controls}
        if names := tuple(sorted(after.keys() - before.keys())):
            added[page_id] = names
        if names := tuple(sorted(before.keys() - after.keys())):
            removed[page_id] = names
        both = sorted(before.keys() & after.keys())
        if names := tuple(n for n in both if before[n] != after[n]):
            changed[page_id] = names
        if names := tuple(n for n in both if before[n].safety != after[n].safety):
            flipped[page_id] = names
            flip_lines += [
                f"control now {after[n].safety.value} on {page_id}: {n} "
                f"(was {before[n].safety.value})"
                for n in names
            ]
    old_flows = {flow.name: flow for flow in old.flows}
    new_flows = {flow.name: flow for flow in new.flows}
    flow_changes = tuple(
        sorted(
            name
            for name in old_flows.keys() | new_flows.keys()
            if old_flows.get(name) != new_flows.get(name)
        )
    )
    return ModelDiff(
        controls_added=added,
        controls_changed=changed,
        controls_removed=removed,
        flows_changed=flow_changes,
        hosts_added=tuple(sorted(set(new.hosts) - set(old.hosts))),
        hosts_removed=tuple(sorted(set(old.hosts) - set(new.hosts))),
        pages_added=tuple(sorted(new_pages.keys() - old_pages.keys())),
        pages_changed=tuple(page_changes),
        pages_removed=tuple(sorted(old_pages.keys() - new_pages.keys())),
        safety_changed=flipped,
        safety_lines=tuple(flip_lines),
    )


def has_thin_fingerprint(page: Page) -> bool:
    """Whether too few landmarks were recorded to tell the page apart by them."""
    return sum(not mark.startswith("link:") for mark in page.fingerprint) < _MIN_LANDMARKS


def worst(grades: Iterable[Grade]) -> Grade:
    """The worst of `grades` (compatible when there are none)."""
    return max(grades, key=_RANK.__getitem__, default=Grade.COMPATIBLE)


_ALIASES = {
    "combobox": ("listbox",),
    "listbox": ("combobox",),
    "searchbox": ("textbox",),
    "textbox": ("searchbox",),
}
_FIELD_ROLES = frozenset(
    {
        "checkbox",
        "combobox",
        "listbox",
        "menuitemcheckbox",
        "menuitemradio",
        "radio",
        "searchbox",
        "slider",
        "spinbutton",
        "switch",
        "textbox",
    }
)
#: Fewer recorded landmarks than this say too little to judge a page by their share.
_MIN_LANDMARKS = 3
_RANK = {Grade.COMPATIBLE: 0, Grade.DEGRADED: 1, Grade.INCOMPATIBLE: 2}
#: A page whose recorded landmarks are mostly gone is no longer the recorded page.
_SAME_PAGE = 0.5
#: Navigation-like controls whose names are often data (mail subjects, row links).
_SOFT_ROLES = frozenset({"link", "menuitem", "tab"})
#: Below this share of the recorded landmarks, the page changed enough to review the model.
_UNCHANGED = 0.8


def _appearance_findings(page: Page, observed: Sequence[ControlObservation]) -> list[Finding]:
    if not observed:
        return [
            _looks_different("nothing was found on the page; is it loaded?", Grade.INCOMPATIBLE)
        ]
    recorded = {_canonical(mark) for mark in page.fingerprint if not mark.startswith("link:")}
    if len(recorded) < _MIN_LANDMARKS:
        live_marks = {_canonical(mark) for mark in landmark_marks(observed)}
        if recorded and not recorded & live_marks:
            return [
                _looks_different(
                    f"None of the recorded landmarks of {page.page_id} are on the page; it may "
                    "be another page or not loaded, so check the address and re-explore it",
                    Grade.INCOMPATIBLE,
                )
            ]
        return []
    live = {_canonical(mark) for mark in landmark_marks(observed) if not mark.startswith("link:")}
    share = len(recorded & live) / len(recorded)
    if share < _SAME_PAGE:
        return [
            _looks_different(
                f"Only {share:.0%} of the recorded landmarks of {page.page_id} are on the "
                "page; it may be another page or not loaded, so check the address and "
                "re-explore it",
                Grade.INCOMPATIBLE,
            )
        ]
    if share < _UNCHANGED:
        return [
            _looks_different(
                f"{share:.0%} of the recorded landmarks of {page.page_id} are still on the "
                "page; review the model and re-explore it if it matters",
                Grade.DEGRADED,
            )
        ]
    return []


def _canonical(mark: str) -> str:
    """A `role:name` mark with alias roles folded together (search box and text box alike)."""
    role, sep, name = mark.partition(":")
    folded = {"combobox": "listbox", "searchbox": "textbox"}.get(role, role)
    return f"{folded}{sep}{name}"


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
    if verifiable[0] == "found":
        return []
    if any(v in ("found", "alias") for v in verifiable):
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
    soft = _role_of(control) in _SOFT_ROLES
    return [
        Finding(
            control=control.name,
            grade=Grade.DEGRADED if soft else Grade.INCOMPATIBLE,
            kind=FindingKind.MISSING,
            note=(
                f"{control.name} is no longer on the page; "
                + ("links and menu items are often data, so this may be harmless; " if soft else "")
                + "re-explore this page"
            ),
        )
    ]


def _locator_verdict(
    strategy: LocatorStrategy, value: str, live: set[tuple[str, str]]
) -> str | None:
    """`found`, `alias` (a role the page treats alike), `absent`, or None when unverifiable."""
    if strategy is LocatorStrategy.ROLE_NAME:
        role, _, name = value.partition(":")
        if (role, name) in live:
            return "found"
        if any((alias, name) in live for alias in _ALIASES.get(role, ())):
            return "alias"
        return "absent"
    if strategy is LocatorStrategy.LABEL:
        found = any(role in LABELLED_ROLES and name == value for role, name in live)
        return "found" if found else "absent"
    return None


def _looks_different(note: str, grade: Grade) -> Finding:
    return Finding(control=None, grade=grade, kind=FindingKind.LOOKS_DIFFERENT, note=note)


def _new_field_findings(page: Page, observed: Sequence[ControlObservation]) -> list[Finding]:
    if len(page.controls) >= MAX_DRAFT_CONTROLS:
        return []  # the model was cut off at the cap, so what it lacks is not necessarily new
    known_role_names: set[tuple[str, str]] = set()
    known_labels: set[str] = set()
    for mark in page.fingerprint:  # fields the model records no control for are still known
        role, _, name = mark.partition(":")
        known_role_names.add((role, name))
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
        and not (item.role in LABELLED_ROLES and item.name in known_labels)
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


def _role_of(control: Control) -> str:
    """The ARIA role a control was recorded with (from its role-and-name locator)."""
    for locator in control.locators:
        if locator.strategy is LocatorStrategy.ROLE_NAME:
            return locator.value.partition(":")[0]
    return ""
