"""Exploring a website once to record its site model (issue #486).

An exploration observes pages, drafts a model from what it sees and decides, control by
control, whether the explorer may click it. The rules are deliberately conservative:

- links, tabs and menu items are read-only navigation and are followed on the task's
  `allowed_hosts` only;
- a control that may change state (a button, a checkbox) is recorded as metadata and is
  clicked only after the user answers yes at their own terminal, with no as the default; the
  answer is read from the terminal, never from an argument, page text or agent input, so an
  agent cannot approve for itself, and with no terminal nothing is clicked;
- a control whose name starts a finalizing word (submit, send, pay, ...) is recorded and
  never clicked, with or without approval;
- an approval covers that one click and is not kept.

The page is only ever described by role and accessible name (nothing a user typed is read
into the draft), and a label that looks like typed data is dropped and counted.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

from blumkin.browser.actions import host_allowed
from blumkin.browser.cdp import DEFAULT_ACTION_TIMEOUT_S, ControlInfo, attach
from blumkin.browser.guard import is_finalizing_name, vet_control
from blumkin.browser.model import (
    Control,
    ControlKind,
    ControlSafety,
    Flow,
    FlowStep,
    Locator,
    LocatorStrategy,
    Page,
    SiteModel,
    is_storable_label,
)
from blumkin.browser.profiles import BrowserProfile


class ClickDecision(StrEnum):
    """What exploration may do with a control."""

    ASK = "ask"
    CLICK = "click"
    RECORD_ONLY = "record_only"


@dataclass(frozen=True, slots=True)
class ControlObservation:
    """One named control seen on a page: its ARIA role and accessible name."""

    role: str
    name: str


class ExploreDriver(Protocol):
    """What exploration needs from a browser: a page description and a vetted click."""

    @property
    def url(self) -> str: ...

    def aria_snapshot(self) -> str: ...

    def click_vetted(self, role: str, name: str) -> None: ...


class ExploreRawDriver(Protocol):
    """The raw browser surface exploration uses (`PlaywrightDriver` implements it)."""

    @property
    def url(self) -> str: ...

    def aria_snapshot(self) -> str: ...

    def block_form_submission(self) -> None: ...

    def checked_click(self, name: str, role: str, vet: Callable[[ControlInfo], None]) -> None: ...

    def close(self) -> None: ...


class ExploreRefusedError(RuntimeError):
    """Exploration refused to do something (an off-host page, a control it will not click)."""


class Confirmer(Protocol):
    """Asks the user a yes/no question; the answer must not come from the agent."""

    def confirm(self, prompt: str) -> bool: ...


class Explorer:
    """Observes pages and clicks controls under the exploration rules."""

    def __init__(
        self,
        driver: ExploreDriver,
        confirmer: Confirmer,
        *,
        allowed_hosts: Sequence[str],
        extra_deny: Sequence[str] = (),
    ) -> None:
        self._allowed_hosts = tuple(allowed_hosts)
        self._confirmer = confirmer
        self._driver = driver
        self._extra_deny = tuple(extra_deny)

    def click(self, role: str, name: str) -> bool:
        """Click the control if the rules allow it; False when it was only recorded."""
        self._require_host("before the click")
        decision = decide_click(role, name, extra_deny=self._extra_deny)
        if decision is ClickDecision.RECORD_ONLY:
            return False
        if decision is ClickDecision.ASK:
            host = urlsplit(self._driver.url).hostname or "this page"
            prompt = f"Click the {role} {name!r} on {host}? It may change state."
            if not self._confirmer.confirm(prompt):
                return False
        try:
            self._driver.click_vetted(role, name)
        except ExploreRefusedError:
            return False
        self._require_host("after the click")
        return True

    def observe(self) -> tuple[ControlObservation, ...]:
        """The named controls on the current page (which must be on an allowed host)."""
        self._require_host("before observing")
        return parse_aria_snapshot(self._driver.aria_snapshot())

    def _require_host(self, when: str) -> None:
        parts = urlsplit(self._driver.url)
        host = parts.hostname or ""
        if parts.scheme not in ("http", "https") or not host_allowed(host, self._allowed_hosts):
            raise ExploreRefusedError(
                f"Expected the page to be on an allowed host {when}, got {host or 'no host'!r}"
            )


class ExploringDriver:
    """The `ExploreDriver` over the raw browser driver.

    A click goes through the same element inspection as a guarded click (a submit control,
    a `formaction`, a script link or a submitting handler is refused), and while attached the
    driver swallows every non-GET navigation, the backstop that stops a submit nobody saw.
    """

    def __init__(self, raw: ExploreRawDriver) -> None:
        self._raw = raw
        raw.block_form_submission()

    def aria_snapshot(self) -> str:
        return self._raw.aria_snapshot()

    def click_vetted(self, role: str, name: str) -> None:
        def vet(info: ControlInfo) -> None:
            reason = vet_control(info)
            if reason is not None:
                raise ExploreRefusedError(f"Expected to click {name!r}, got a refusal: {reason}")

        self._raw.checked_click(name, role, vet)

    def close(self) -> None:
        self._raw.close()

    @property
    def url(self) -> str:
        return self._raw.url


class ModelDraft:
    """A site model under construction from observed pages and recorded steps."""

    def __init__(self, task: str, hosts: Sequence[str]) -> None:
        self.skipped = 0
        self._flow_steps: list[FlowStep] = []
        self._hosts = tuple(hosts)
        self._pages: dict[str, Page] = {}
        self._task = task

    @classmethod
    def from_model(cls, model: SiteModel) -> ModelDraft:
        """A draft that continues a stored or in-progress model."""
        draft = cls(task=model.task, hosts=model.hosts)
        draft._pages = {page.page_id: page for page in model.pages}
        draft._flow_steps = [step for flow in model.flows for step in flow.steps]
        return draft

    def add_page(self, page_id: str, url: str, controls: Sequence[ControlObservation]) -> None:
        """Record (or update) a page from what was observed on it."""
        if not _PAGE_ID.fullmatch(page_id):
            raise ExploreRefusedError(
                f"Expected a page id of lowercase words (such as expense-list), got {page_id!r}"
            )
        marks: list[str] = []
        built: list[Control] = []
        used: set[str] = set()
        for item in controls:
            if not is_storable_label(item.name) or not is_storable_label(
                f"{item.role}:{item.name}"
            ):
                self.skipped += 1
                continue
            if item.role not in _SKIPPED_FROM_FINGERPRINT and len(marks) < _MAX_MARKS:
                marks.append(f"{item.role}:{item.name}")
            kind = _KIND_BY_ROLE.get(item.role)
            if kind is None or len(built) >= _MAX_DRAFT_CONTROLS:
                continue
            built.append(_control(page_id, item, kind, used))
        self._pages[page_id] = Page(
            controls=tuple(built),
            fingerprint=tuple(sorted(set(marks))),
            page_id=page_id,
            url_template=url_template(url),
        )

    def add_step(
        self, action: str, *, page_id: str | None, control: str | None, expect: str | None = None
    ) -> None:
        """Record one step of the flow being explored."""
        self._flow_steps.append(
            FlowStep(action=action, control=control, expect=expect, page=page_id)
        )

    def to_model(self, *, recorded_at: str, blumkin_version: str) -> SiteModel:
        """The draft as a model (not yet validated or stored: `save_model` does both)."""
        empty = [page_id for page_id, page in self._pages.items() if not page.fingerprint]
        if empty:
            raise ExploreRefusedError(
                "Expected every page to have a landmark that can be stored, "
                f"got none for {empty[0]!r}"
            )
        flows = (Flow(name="recorded", steps=tuple(self._flow_steps)),) if self._flow_steps else ()
        return SiteModel(
            blumkin_version=blumkin_version,
            flows=flows,
            hosts=self._hosts,
            pages=tuple(self._pages.values()),
            recorded_at=recorded_at,
            task=self._task,
            version=0,
        )


class TerminalConfirmer:
    """Asks on the user's controlling terminal; the default answer is no.

    The question is written to and read from `/dev/tty`, not standard input, so a command
    started by an agent without a terminal gets no answer and nothing is approved.
    """

    def __init__(self, open_tty: Callable[[], Any] | None = None) -> None:
        self.unavailable = False
        self._open_tty = open_tty or _open_dev_tty

    def confirm(self, prompt: str) -> bool:
        try:
            tty = self._open_tty()
        except OSError:
            self.unavailable = True
            return False
        with contextlib.closing(tty):
            tty.write(f"{prompt} [y/N] ")
            tty.flush()
            reply = tty.readline()
        return reply.strip().lower() in {"y", "yes"}


def attach_exploring(
    profile: BrowserProfile,
    *,
    attach_raw: Callable[..., ExploreRawDriver] = attach,
    timeout_s: float = DEFAULT_ACTION_TIMEOUT_S,
    **attach_kwargs: Any,
) -> ExploringDriver:
    """Attach to the task browser for exploration."""
    raw = attach_raw(profile, timeout_s=timeout_s, **attach_kwargs)
    try:
        return ExploringDriver(raw)
    except BaseException:
        raw.close()
        raise


def decide_click(role: str, name: str, *, extra_deny: Sequence[str] = ()) -> ClickDecision:
    """What exploration may do with a control of this role and name."""
    if is_finalizing_name(name, extra_deny):
        return ClickDecision.RECORD_ONLY
    if role in _READ_ONLY_ROLES:
        return ClickDecision.CLICK
    if role in _STATE_CHANGING_ROLES:
        return ClickDecision.ASK
    return ClickDecision.RECORD_ONLY


def parse_aria_snapshot(text: str) -> tuple[ControlObservation, ...]:
    """The named nodes of a Playwright ARIA snapshot, in order and without repeats."""
    seen: set[tuple[str, str]] = set()
    found: list[ControlObservation] = []
    for line in text.splitlines():
        match = _SNAPSHOT_LINE.match(line)
        if match is None or match.group("name") is None or match.group("role") == "text":
            continue
        name = re.sub(r"\\(.)", r"\1", match.group("name"))
        key = (match.group("role"), name)
        if key in seen:
            continue
        seen.add(key)
        found.append(ControlObservation(role=match.group("role"), name=name))
    return tuple(found)


def url_template(url: str) -> str:
    """The path of `url` with every value-like segment replaced by `{id}`; no query or fragment."""
    path = urlsplit(url).path or "/"
    segments = path.split("/")[1:]
    out = [seg if not seg or _is_word(seg) else "{id}" for seg in segments]
    return "/" + "/".join(out)


_MAX_DRAFT_CONTROLS = 200
_MAX_MARKS = 100
_PAGE_ID = re.compile(r"[a-z]+(?:-[a-z]+){0,5}")
_READ_ONLY_ROLES = frozenset({"link", "menuitem", "tab"})
_SKIPPED_FROM_FINGERPRINT = frozenset({"img", "separator"})
_SNAPSHOT_LINE = re.compile(r'^\s*- (?P<role>[a-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?')
_STATE_CHANGING_ROLES = frozenset(
    {"button", "checkbox", "menuitemcheckbox", "menuitemradio", "option", "radio", "switch"}
)
_WORD = re.compile(r"[A-Za-z]{1,24}(?:[-_][A-Za-z]{1,24})*")
_KIND_BY_ROLE: dict[str, ControlKind] = {
    "button": ControlKind.BUTTON,
    "checkbox": ControlKind.BUTTON,
    "combobox": ControlKind.SELECT,
    "link": ControlKind.BUTTON,
    "listbox": ControlKind.SELECT,
    "menuitem": ControlKind.BUTTON,
    "radio": ControlKind.BUTTON,
    "searchbox": ControlKind.TEXT,
    "spinbutton": ControlKind.TEXT,
    "switch": ControlKind.BUTTON,
    "tab": ControlKind.BUTTON,
    "textbox": ControlKind.TEXT,
}
_LABELLED_ROLES = frozenset({"combobox", "listbox", "searchbox", "spinbutton", "textbox"})


def _control(page_id: str, item: ControlObservation, kind: ControlKind, used: set[str]) -> Control:
    slug = re.sub(r"[^a-z]+", "-", item.name.casefold()).strip("-")[:24].strip("-") or item.role
    name = f"{page_id}.{slug}"
    count = 1
    while name in used:
        count += 1
        name = f"{page_id}.{slug}-{count}"
    used.add(name)
    locators = [Locator(strategy=LocatorStrategy.ROLE_NAME, value=f"{item.role}:{item.name}")]
    if item.role in _LABELLED_ROLES:
        locators.append(Locator(strategy=LocatorStrategy.LABEL, value=item.name))
    safety = ControlSafety.FINALIZING if is_finalizing_name(item.name) else ControlSafety.SAFE
    return Control(kind=kind, locators=tuple(locators), name=name, safety=safety)


def _is_word(segment: str) -> bool:
    return bool(_WORD.fullmatch(segment)) and sum(char.isupper() for char in segment) <= 2


def _open_dev_tty() -> Any:
    return open("/dev/tty", "r+", encoding="utf-8")  # noqa: SIM115 - closed by the caller
