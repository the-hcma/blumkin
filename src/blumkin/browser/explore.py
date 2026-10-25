"""Exploring a website once to record its site model (issue #486).

An exploration observes pages, drafts a model from what it sees and decides, control by
control, whether the explorer may click it. The rules are deliberately conservative:

- links and tabs are read-only navigation and are followed on the task's
  `allowed_hosts` only;
- a control that may change state (a button, a checkbox, a menu item) is recorded as metadata and is
  clicked only after the user answers yes at their own terminal, with no as the default; the
  answer is read from the terminal, never from an argument, page text or agent input, so an
  agent cannot approve for itself, and with no terminal nothing is clicked;
- a control whose name starts a finalizing word (submit, send, pay, ...) is recorded and
  never clicked, with or without approval;
- an approval covers that one click and is not kept.

One limit: the answer comes from the controlling terminal, so a process that can create its
own pseudo-terminal (a pty) could answer for you. Run the clicks that ask for approval from
your own terminal session, not from one an agent opened.

The page is only ever described by role and accessible name (nothing a user typed is read
into the draft), and a label that looks like typed data is dropped and counted.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import termios
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from blumkin.browser.actions import host_allowed
from blumkin.browser.cdp import DEFAULT_ACTION_TIMEOUT_S, BrowserActionError, ControlInfo, attach
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

LABELLED_ROLES = frozenset({"combobox", "listbox", "searchbox", "spinbutton", "textbox"})
MAX_DRAFT_CONTROLS = 200


class ClickDecision(StrEnum):
    """What exploration may do with a control."""

    ASK = "ask"
    CLICK = "click"
    RECORD_ONLY = "record_only"


class Confirmer(Protocol):
    """Asks the user a yes/no question; the answer must not come from the agent."""

    def confirm(self, prompt: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ControlObservation:
    """One named control seen on a page: its ARIA role and accessible name."""

    role: str
    name: str


class DraftNotFoundError(LookupError):
    """There is no in-progress exploration draft for the task."""


class ExploreDriver(Protocol):
    """What exploration needs from a browser: a page description, an inspection and a click."""

    @property
    def url(self) -> str: ...

    def aria_snapshot(self) -> str: ...

    def click_vetted(self, role: str, name: str, expected: Inspection) -> None: ...

    def inspect(self, role: str, name: str) -> Inspection: ...


class ExploreNeedsConfirmationError(ValueError):
    """Saving the draft needs the user's explicit `--yes` after they have seen it."""


class ExploreRawDriver(Protocol):
    """The raw browser surface exploration uses (`PlaywrightDriver` implements it)."""

    @property
    def url(self) -> str: ...

    def aria_snapshot(self) -> str: ...

    def block_form_submission(self) -> None: ...

    def checked_click(self, name: str, role: str, vet: Callable[[ControlInfo], None]) -> None: ...

    def close(self) -> None: ...

    def restrict_navigation(self, is_allowed: Callable[[str], bool]) -> None: ...

    def settle(self, previous_url: str) -> None: ...


class ExploreRefusedError(RuntimeError):
    """Exploration refused to do something (an off-host page, a control it will not click)."""


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
        self.last_outcome = ""
        self._allowed_hosts = tuple(allowed_hosts)
        self._confirmer = confirmer
        self._driver = driver
        self._extra_deny = tuple(extra_deny)

    def click(self, role: str, name: str) -> bool:
        """Click the control if the rules allow it; False when it was only recorded.

        `last_outcome` says what happened: `clicked`, `record_only`, `declined` (the user
        said no) or `refused: <why>`. The element is inspected before anyone is asked, and
        the host is checked again after the answer, because the page may have moved on.
        """
        self._require_host("before the click")
        decision = decide_click(role, name, extra_deny=self._extra_deny)
        if decision is ClickDecision.RECORD_ONLY:
            self.last_outcome = "record_only"
            return False
        try:
            inspection = self._driver.inspect(role, name)
        except BrowserActionError as exc:
            self.last_outcome = f"refused: {_first_line(exc)}"
            return False
        if inspection.refusal is not None:
            self.last_outcome = f"refused: {inspection.refusal}"
            return False
        problem = _link_problem(self._driver.url, inspection.href, self._allowed_hosts)
        if problem is not None:
            self.last_outcome = f"refused: {problem}"
            return False
        if decision is ClickDecision.CLICK and _href_looks_destructive(inspection.href):
            decision = ClickDecision.ASK
        if decision is ClickDecision.ASK:
            host = urlsplit(self._driver.url).hostname or "this page"
            if not self._confirmer.confirm(_prompt(role, name, host, inspection.href)):
                self.last_outcome = "declined"
                return False
            self._require_host("after the answer and before the click")
        try:
            self._driver.click_vetted(role, name, inspection)
        except (BrowserActionError, ExploreRefusedError) as exc:
            self.last_outcome = f"refused: {_first_line(exc)}"
            return False
        try:
            self._require_host("after the click")
        except ExploreRefusedError as exc:
            raise ExploreRefusedError(
                f"The click happened, and the page is now off the allowed hosts: {exc}"
            ) from exc
        self.last_outcome = "clicked"
        return True

    def observe(self) -> tuple[ControlObservation, ...]:
        """The named controls on the current page (which must be on an allowed host)."""
        return self.observe_with_url()[1]

    def observe_with_url(self) -> tuple[str, tuple[ControlObservation, ...]]:
        """The page's URL and its controls; refused if the page changes while it is read."""
        self._require_host("before observing")
        url = self._driver.url
        controls = parse_aria_snapshot(self._driver.aria_snapshot())
        if self._driver.url != url:
            raise ExploreRefusedError(
                "Expected the page to stay put while it is observed, got a change"
            )
        return url, controls

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
    With `allowed_hosts` it also aborts any main-frame navigation to another host.
    """

    def __init__(self, raw: ExploreRawDriver, allowed_hosts: Sequence[str] = ()) -> None:
        self._raw = raw
        raw.block_form_submission()
        if allowed_hosts:
            hosts = tuple(allowed_hosts)
            raw.restrict_navigation(lambda host: host_allowed(host, hosts))

    def aria_snapshot(self) -> str:
        return self._raw.aria_snapshot()

    def click_vetted(self, role: str, name: str, expected: Inspection) -> None:
        def vet(info: ControlInfo) -> None:
            reason = vet_control(info) or _window_problem(info)
            if reason is not None:
                raise ExploreRefusedError(f"Expected to click {name!r}, got a refusal: {reason}")
            if info.tag != expected.tag or info.href != expected.href:
                raise ExploreRefusedError(
                    f"Expected to click the {role} {name!r} that was inspected, got an element "
                    "that changed since"
                )

        before = self._raw.url
        self._raw.checked_click(name, role, vet)
        self._raw.settle(before)

    def close(self) -> None:
        self._raw.close()

    def inspect(self, role: str, name: str) -> Inspection:
        """Why the control cannot be clicked (if so) and where it links, without clicking it."""

        def look(info: ControlInfo) -> None:
            refusal = vet_control(info) or _window_problem(info)
            raise _Inspected(Inspection(refusal=refusal, href=info.href, tag=info.tag))

        try:
            self._raw.checked_click(name, role, look)
        except _Inspected as done:
            return done.inspection
        raise ExploreRefusedError(f"Expected to inspect {name!r} without clicking, got a click")

    @property
    def url(self) -> str:
        return self._raw.url


@dataclass(frozen=True, slots=True)
class Inspection:
    """What looking at a control (without clicking it) showed."""

    refusal: str | None
    href: str
    tag: str = ""


class ModelDraft:
    """A site model under construction from observed pages and recorded steps."""

    def __init__(self, task: str, hosts: Sequence[str], extra_deny: Sequence[str] = ()) -> None:
        self.skipped = 0
        self._extra_deny = tuple(extra_deny)
        self._flows: dict[str, list[FlowStep]] = {}
        self._hosts = tuple(hosts)
        self._pages: dict[str, Page] = {}
        self._task = task

    @classmethod
    def from_model(cls, model: SiteModel, extra_deny: Sequence[str] = ()) -> ModelDraft:
        """A draft that continues a stored or in-progress model."""
        draft = cls(task=model.task, hosts=model.hosts, extra_deny=extra_deny)
        draft._pages = {page.page_id: page for page in model.pages}
        draft._flows = {flow.name: list(flow.steps) for flow in model.flows}
        return draft

    def add_host(self, host: str) -> None:
        """Record a host the site was seen on."""
        if host not in self._hosts:
            self._hosts = (*self._hosts, host)

    def add_page(self, page_id: str, url: str, controls: Sequence[ControlObservation]) -> None:
        """Record (or update) a page from what was observed on it."""
        if not _PAGE_ID.fullmatch(page_id):
            raise PageIdError(
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
            if kind is None:
                continue
            if len(built) >= MAX_DRAFT_CONTROLS:
                self.skipped += 1
                continue
            built.append(_control(page_id, item, kind, used, self._extra_deny))
        self._pages[page_id] = Page(
            controls=tuple(built),
            fingerprint=tuple(sorted(set(marks))),
            page_id=page_id,
            url_template=url_template(url),
        )
        self._prune_steps()

    def add_step(
        self, action: str, *, page_id: str | None, control: str | None, expect: str | None = None
    ) -> None:
        """Record one step of the flow being explored."""
        self._flows.setdefault("recorded", []).append(
            FlowStep(action=action, control=control, expect=expect, page=page_id)
        )

    def control_for(self, page_id: str, role: str, name: str) -> str | None:
        """The logical name of the control with this role and name on the page, if recorded."""
        page = self._pages.get(page_id)
        wanted = f"{role}:{name}"
        for control in page.controls if page else ():
            if any(loc.value == wanted for loc in control.locators):
                return control.name
        return None

    def _prune_steps(self) -> None:
        """Drop recorded steps whose page or control is no longer in the draft."""
        controls = {pid: {c.name for c in page.controls} for pid, page in self._pages.items()}
        everywhere = {name for names in controls.values() for name in names}

        def alive(step: FlowStep) -> bool:
            if step.page is not None and step.page not in self._pages:
                return False
            if step.control is None:
                return True
            known = controls[step.page] if step.page is not None else everywhere
            return step.control in known

        self._flows = {
            name: [step for step in steps if alive(step)] for name, steps in self._flows.items()
        }

    def to_model(self, *, recorded_at: str, blumkin_version: str) -> SiteModel:
        """The draft as a model (not yet validated or stored: `save_model` does both)."""
        empty = [page_id for page_id, page in self._pages.items() if not page.fingerprint]
        if empty:
            raise ExploreRefusedError(
                "Expected every page to have a landmark that can be stored, "
                f"got none for {empty[0]!r}"
            )
        flows = tuple(
            Flow(name=name, steps=tuple(steps)) for name, steps in self._flows.items() if steps
        )
        return SiteModel(
            blumkin_version=blumkin_version,
            flows=flows,
            hosts=self._hosts,
            pages=tuple(self._pages.values()),
            recorded_at=recorded_at,
            task=self._task,
            version=0,
        )


class PageIdError(ValueError):
    """A page id that is not lowercase words."""


class Terminal:
    """The user's controlling terminal, opened once for writing the question and reading the answer.

    A terminal is not seekable, so it cannot be opened read-write in text mode: the question
    and the answer use two handles on the same descriptor. `O_NOCTTY` keeps opening it from
    making it the controlling terminal of a process that has none.
    """

    def __init__(self, path: str = "/dev/tty") -> None:
        descriptor = os.open(path, os.O_RDWR | os.O_NOCTTY)
        try:
            self._reader = os.fdopen(os.dup(descriptor), "r", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        try:
            self._writer = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            self._reader.close()
            raise

    def close(self) -> None:
        try:
            self._writer.close()
        finally:
            self._reader.close()

    def fileno(self) -> int:
        return self._reader.fileno()

    def flush(self) -> None:
        self._writer.flush()

    def readline(self) -> str:
        return self._reader.readline()

    def write(self, text: str) -> int:
        return self._writer.write(text)


class TerminalConfirmer:
    """Asks on the user's controlling terminal; the default answer is no.

    The question is written to and read from `/dev/tty`, not standard input, so a command
    started by an agent without a terminal gets no answer and nothing is approved. A terminal
    that does not answer within `timeout_s` counts as no. A process that owns a pseudo-terminal
    could answer for the user: approve clicks from your own terminal session.
    """

    def __init__(self, open_tty: Callable[[], Any] | None = None, timeout_s: float = 300.0) -> None:
        self.timed_out = False
        self.unavailable = False
        self._open_tty = open_tty or _open_dev_tty
        self._timeout_s = timeout_s

    def confirm(self, prompt: str) -> bool:
        try:
            tty = self._open_tty()
        except OSError:
            self.unavailable = True
            return False
        with contextlib.closing(tty):
            if not _in_foreground(tty):
                # A background job that touched the terminal would be stopped by the shell.
                self.unavailable = True
                return False
            _discard_pending_input(tty)
            try:
                tty.write(f"{prompt} [y/N] ")
                tty.flush()
                if not _answers_in_time(tty, self._timeout_s):
                    self.timed_out = True
                    return False
                reply = tty.readline()
            except OSError, UnicodeDecodeError:
                return False
        return reply.strip().lower() in {"y", "yes"}


def attach_exploring(
    profile: BrowserProfile,
    *,
    allowed_hosts: Sequence[str] = (),
    attach_raw: Callable[..., ExploreRawDriver] = attach,
    timeout_s: float = DEFAULT_ACTION_TIMEOUT_S,
    **attach_kwargs: Any,
) -> ExploringDriver:
    """Attach to the task browser for exploration."""
    raw = attach_raw(profile, timeout_s=timeout_s, **attach_kwargs)
    try:
        return ExploringDriver(raw, allowed_hosts)
    except BaseException:
        raw.close()
        raise


def decide_click(role: str, name: str, *, extra_deny: Sequence[str] = ()) -> ClickDecision:
    """What exploration may do with a control of this role and name."""
    if _is_finalizing(name, extra_deny):
        return ClickDecision.RECORD_ONLY
    if role in _READ_ONLY_ROLES:
        return ClickDecision.ASK if _needs_a_question(name) else ClickDecision.CLICK
    if role in _STATE_CHANGING_ROLES:
        return ClickDecision.ASK
    return ClickDecision.RECORD_ONLY


def landmark_marks(controls: Sequence[ControlObservation]) -> frozenset[str]:
    """The `role:name` marks a page is recognized by, as `ModelDraft` records them."""
    return frozenset(
        f"{item.role}:{item.name}"
        for item in controls
        if item.role not in _SKIPPED_FROM_FINGERPRINT
        and is_storable_label(item.name)
        and is_storable_label(f"{item.role}:{item.name}")
    )


def parse_aria_snapshot(text: str) -> tuple[ControlObservation, ...]:
    """The named nodes of a Playwright ARIA snapshot, in order and without repeats."""
    seen: set[tuple[str, str]] = set()
    found: list[ControlObservation] = []
    for line in text.splitlines():
        match = _SNAPSHOT_LINE.match(line)
        if match is None or match.group("name") is None or match.group("role") == "text":
            continue
        name = _unescape(match.group("name"))
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


_DESTRUCTIVE = re.compile(
    r"\b(?:archive|cancel|clear|close account|deactivate|delete|disable|discard|empty|erase|"
    r"exit|log\s*off|log\s*out|logout|mark as|move to|remove|reset|revoke|sign\s*off|"
    r"sign\s*out|signout|trash|unsubscribe)\b"
)
_DESTRUCTIVE_HREF = re.compile(
    "archive|cancel|closeaccount|deactivate|delete|destroy|disable|discard|empty|erase|kill|"
    "logoff|logout|purge|remove|reset|revoke|signoff|signout|terminate|trash|unlink|"
    "unsubscribe|wipe"
)
_MAX_MARKS = 100
_PAGE_ID = re.compile(r"[a-z]+(?:-[a-z]+){0,5}")
_READ_ONLY_ROLES = frozenset({"link", "tab"})
_SKIPPED_FROM_FINGERPRINT = frozenset(
    {
        "alert",
        "article",
        "cell",
        "columnheader",
        "definition",
        "gridcell",
        "img",
        "listitem",
        "option",
        "paragraph",
        "row",
        "rowheader",
        "separator",
        "status",
        "term",
        "text",
        "treeitem",
    }
)
_SNAPSHOT_LINE = re.compile(r'^\s*- (?P<role>[a-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?')
_STATE_CHANGING_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "radio",
        "switch",
    }
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


class _Inspected(Exception):  # noqa: N818 - control flow, not an error
    """Raised from the inspection callback to stop `checked_click` before it clicks."""

    def __init__(self, inspection: Inspection) -> None:
        super().__init__("inspected")
        self.inspection = inspection


def _answers_in_time(tty: Any, timeout_s: float) -> bool:
    try:
        descriptor = tty.fileno()
    except AttributeError, OSError, ValueError:
        return True  # a stream with no descriptor (a test double) is read directly
    ready, _, _ = select.select([descriptor], [], [], timeout_s)
    return bool(ready)


def _control(
    page_id: str,
    item: ControlObservation,
    kind: ControlKind,
    used: set[str],
    extra_deny: Sequence[str],
) -> Control:
    words = re.sub(r"[^a-z]+", "-", item.name.casefold()).strip("-").split("-")
    # A word over 16 letters would read as a token to the model's name check.
    slug = "-".join(word[:16] for word in words)[:24].strip("-") or item.role
    name = f"{page_id}.{slug}"
    count = 1
    while name in used:
        count += 1
        name = f"{page_id}.{slug}-{count}"
    used.add(name)
    locators = [Locator(strategy=LocatorStrategy.ROLE_NAME, value=f"{item.role}:{item.name}")]
    if item.role in LABELLED_ROLES:
        locators.append(Locator(strategy=LocatorStrategy.LABEL, value=item.name))
    finalizing = _is_finalizing(item.name, extra_deny)
    safety = ControlSafety.FINALIZING if finalizing else ControlSafety.SAFE
    return Control(kind=kind, locators=tuple(locators), name=name, safety=safety)


def _discard_pending_input(tty: Any) -> None:
    """Throw away anything typed ahead, so only an answer to this question counts."""
    try:
        termios.tcflush(tty.fileno(), termios.TCIFLUSH)
    except AttributeError, OSError, ValueError, termios.error:
        return


def _first_line(error: BaseException) -> str:
    return (str(error).splitlines() or [type(error).__name__])[0]


def _href_looks_destructive(href: str) -> bool:
    """Whether the whole address (path, query and fragment) carries a destructive verb."""
    return bool(_DESTRUCTIVE_HREF.search(re.sub(r"[\W_]+", "", href.casefold())))


def _in_foreground(tty: Any) -> bool:
    """Whether this process may use the terminal now (a background job would be stopped)."""
    try:
        return os.tcgetpgrp(tty.fileno()) == os.getpgrp()
    except AttributeError, OSError, ValueError:
        return True  # not a terminal descriptor (a test double): nothing to be stopped by


def _is_finalizing(name: str, extra_deny: Sequence[str]) -> bool:
    return is_finalizing_name(name, extra_deny) or is_finalizing_name(
        "".join(name.split()), extra_deny
    )


def _is_word(segment: str) -> bool:
    return bool(_WORD.fullmatch(segment)) and sum(char.isupper() for char in segment) <= 2


def _link_problem(page_url: str, href: str, allowed_hosts: Sequence[str]) -> str | None:
    """Why a link may not be followed (it leaves the web or the allowed hosts), else None."""
    if not href:
        return None
    parts = urlsplit(urljoin(page_url, href))
    if parts.scheme not in ("http", "https"):
        return f"its link leaves the web ({parts.scheme or 'no scheme'}:)"
    host = parts.hostname or ""
    if not host_allowed(host, allowed_hosts):
        return f"its link leads to {host or 'no host'}"
    return None


def _needs_a_question(name: str) -> bool:
    return (
        bool(_DESTRUCTIVE.search(_spaced(name)))
        or any(ord(char) > 127 for char in name)
        or sum(char.isalpha() for char in name) < 2
    )


def _open_dev_tty() -> Any:
    return Terminal()


def _prompt(role: str, name: str, host: str, href: str) -> str:
    shown = name if len(name) <= 80 else name[:80] + "..."
    target = f" (it links to {href[:120]!r})" if href else ""
    return f"Click the {role} {shown!r} on {host}{target}? It may change state."


def _spaced(text: str) -> str:
    """Lowercase text with `_`, `-` and `/` read as spaces, so `log-out` matches `log out`."""
    return re.sub(r"[_\-/]+", " ", text.casefold())


def _unescape(quoted: str) -> str:
    try:
        return str(json.loads(f'"{quoted}"'))
    except ValueError:
        return re.sub(r"\\(.)", r"\1", quoted)


def _window_problem(info: ControlInfo) -> str | None:
    """Why a link cannot be followed in this window: it would open another one."""
    if info.target not in ("", "_self"):
        return "it opens a new window"
    return None
