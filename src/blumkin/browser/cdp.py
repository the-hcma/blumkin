"""Attach to the task browser over CDP with Playwright and drive it (issue #466).

blumkin attaches to a browser the user launched (`blumkin.browser.launcher`) and signed
in to by hand; it never starts or stops that browser, only connects to and disconnects
from it. Tasks depend on the small `BrowserDriver` interface, so the library behind it
can be swapped without touching them. Every call has an explicit timeout, the attach is
retried a bounded number of times with backoff, and only a loopback `http` DevTools
endpoint is ever accepted; the WebSocket URL the browser advertises must be that same
loopback endpoint, so a rogue listener on the recorded port cannot redirect the connection.

Playwright's synchronous API cannot run inside a running asyncio event loop, so the
drivers here are for synchronous callers such as the CLI.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import math
import os
import re
import signal
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from blumkin.browser import teams
from blumkin.browser.indicator import INDICATOR_JS, indicator_args
from blumkin.browser.profiles import (
    BrowserProfile,
    read_session,
    require_blumkin_profile,
)
from blumkin.prompt_injection import scan_for_injection

ATTACH_ATTEMPTS = 3


#: How long a role-less click waits for its control to appear before reporting it missing.
DEFAULT_CLICK_WAIT_S = 5.0


#: The longest any wait for the page may last, whatever was asked for.
MAX_WAIT_S = 300.0


class BrowserActionError(RuntimeError):
    """A page action failed or was refused."""


class CellEditedError(BrowserActionError):
    """A grid cell was edited, but the cell could not be read again afterwards."""


class AmbiguousFieldError(BrowserActionError):
    """More than one field matched the label, so none was used."""


class MissingFieldError(BrowserActionError):
    """Fewer fields matched than the `nth` asked for."""


class BrowserAttachError(RuntimeError):
    """The task browser could not be attached to."""


class BrowserDependencyError(BrowserAttachError):
    """Playwright (or another browser-driver dependency) is missing or broken."""


class BrowserNoPageError(BrowserAttachError):
    """The browser is running but has no open page, and none could be opened."""


class BrowserNotRunningError(BrowserAttachError):
    """No browser was launched for the task, or it has exited."""


class BrowserProtocolError(BrowserAttachError):
    """The browser is running but the DevTools attach to it failed."""


class BrowserDriver(Protocol):
    """What a browser task may do: the guarded surface, with no way to finalize a form."""

    def aria_snapshot(self) -> str: ...

    def click(
        self, name: str, role: str | None = None, wait_s: float = DEFAULT_CLICK_WAIT_S
    ) -> str: ...

    def close(self) -> None: ...

    def dismiss(self) -> None: ...

    def download(
        self,
        name: str,
        role: str | None = None,
        *,
        save_to: Path,
        wait_s: float = DEFAULT_CLICK_WAIT_S,
    ) -> DownloadedFile: ...

    def edit_cell(
        self, row: str, column: str, value: str | None = None, *, grid: int = 1
    ) -> CellEdit:
        """Read one data-grid cell by row and column; with a `value`, edit it."""
        ...

    def fill(self, label: str, value: str, target: FieldTarget | None = None) -> str | None: ...

    def frame_aria_snapshot(
        self,
        index: int,
        is_allowed: Callable[[str], bool],
        expect_host: str | None = None,
        *,
        wait_s: float = 0.0,
    ) -> str: ...

    def frame_list(
        self, is_allowed: Callable[[str], bool], *, wait_s: float = 0.0
    ) -> tuple[FrameInfo, ...]: ...

    def goto(self, url: str) -> None: ...

    def mark_window(
        self, task: str, hosts: Sequence[str] = (), blocked: Sequence[str] = ()
    ) -> None: ...

    def pick(
        self, text: str, role: str = "row", nth: int | None = None, prefix: bool = False
    ) -> None: ...

    def pick_account(
        self,
        email: str,
        hosts: Sequence[str],
        blocked: Sequence[str],
        vet_names: Callable[[Sequence[str]], bool] | None = None,
    ) -> AccountPick: ...

    def press(self, label: str, key: str) -> None: ...

    def read_field(self, label: str, target: FieldTarget | None = None) -> FieldReading: ...

    def read_frame_text(
        self,
        index: int,
        is_allowed: Callable[[str], bool],
        expect_host: str | None = None,
        *,
        wait_s: float = 0.0,
    ) -> PageText: ...

    def read_text(self) -> PageText: ...

    def scroll_frame_text(
        self, is_allowed: Callable[[str], bool], *, max_steps: int, wait_s: float
    ) -> FrameCapture: ...

    def list_options(
        self,
        label: str,
        target: FieldTarget | None = None,
        vet: Callable[[ControlInfo], None] | None = None,
        vet_name: Callable[[str], None] | None = None,
    ) -> OptionsListing: ...

    def select(
        self,
        label: str,
        option: str,
        target: FieldTarget | None = None,
        vet: Callable[[ControlInfo], None] | None = None,
        vet_name: Callable[[str], None] | None = None,
    ) -> str | None: ...

    def title(self) -> str: ...

    def upload(
        self, label: str, path: Path, target: FieldTarget | None = None
    ) -> UploadObservation: ...

    @property
    def url(self) -> str: ...

    def wait_ms(self, milliseconds: int) -> None: ...


@dataclass(frozen=True, slots=True)
class AccountPick:
    """What `pick_account` found on the page: `ok` (one tile, clicked), `wrong_origin`,
    `unsupported` (a field to type into), `none` (no tile for the account) or `ambiguous`."""

    status: str
    tiles: int = 0


@dataclass(frozen=True, slots=True)
class CellEdit:
    """What `edit_cell` did to one cell of an Oracle JET data grid.

    `row` and `column` are 1-based positions (columns count the frozen ones first, then the
    scrolling ones); the labels are the headers they resolved to. `text` is what the cell
    shows afterwards, `edited` whether a value was typed, and `rows` / `columns` the grid's
    size.
    """

    column: int
    column_label: str
    columns: int
    edited: bool
    row: int
    row_label: str
    rows: int
    text: str


@dataclass(frozen=True, slots=True)
class FieldReading:
    """What `read_field` found: `read` (with the value), `missing`, `ambiguous` or `unreadable`
    (a password or other secret field, or a widget whose value cannot be read)."""

    status: str
    value: str | None = None


@dataclass(frozen=True, slots=True)
class OptionsListing:
    """What `list_options` read from a dropdown without choosing anything.

    `complete` is False when the cap on scrolling or on options was reached (the list may hold
    more); `closed` says whether the list was closed again afterwards.
    """

    options: tuple[str, ...]
    complete: bool = True
    closed: bool = True


@dataclass(frozen=True, slots=True)
class UploadObservation:
    """What the page showed after a file was set on an input (issue #607).

    `persisted` is `shown` when the file's name appears on the page near the field, `missing`
    when the field was dropped by a re-render (or lost the file) and the name is nowhere on
    the page, and `unknown` when the input holds the file but the page does not display it (or
    the page could not be read). `input_hidden` flags a hidden or zero-size input, such as a
    drop zone's, which may not be the one the form saves. `files` are the names it holds.
    """

    persisted: str
    input_hidden: bool = False
    files: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ControlInfo:
    """What the page says an element is, read from the element itself before it is used.

    `input_type` is the browser's canonical `type` property (a `<button>` with no type, or
    an invalid one, is `submit`), never the raw attribute.
    """

    tag: str
    input_type: str
    in_form: bool
    #: The element carries a `formaction` or `formmethod` override.
    has_formaction: bool = False
    #: The `href` attribute, for links.
    href: str = ""
    #: The inline `onclick` attribute text.
    inline_handler: str = ""
    #: Where a link opens: empty or `_self` stays in the window, anything else opens another.
    target: str = ""
    #: The element sits inside a dialog (`<dialog>`, `role=dialog`, `alertdialog` or a modal).
    in_dialog: bool = False
    #: Inside a dialog, and its form (if it has one) lies inside that same dialog: the click
    #: can only submit the dialog's own form, never a page-level one the dialog sits in.
    form_in_dialog: bool = False
    #: Every name the button-like controls inside that dialog go by (accessible-name sources:
    #: aria-labelledby, aria-label, text, value, title, id, name), so the guard can tell a small
    #: dialog from a modal wrapped around the whole report and its own Submit.
    dialog_controls: tuple[str, ...] = ()
    #: False when the dialog held more controls, or longer names, than were collected: nothing
    #: can then be said about what the dialog holds.
    dialog_controls_complete: bool = True
    #: The method of the form the element sits in, lower case (`post`, `get`), empty with none.
    form_method: str = ""
    #: The container found by the landmarks is the form itself, not a popup inside it.
    dialog_is_form: bool = False
    #: The "dialog" is not marked as one by the page: it is the smallest container around the
    #: element that holds every control the task declared as a dialog landmark.
    dialog_landmark: bool = False
    #: Set by `checked_pick` only: the element holds, sits inside or is made of a control.
    holds_controls: bool = False
    #: Every name the clicked element itself goes by, beyond the accessible name it was found
    #: by (aria-labelledby, aria-label, text, value, title, alt, id, name and the labels of
    #: its children), so a draft control cannot be a finalizing button renamed by the page.
    own_names: tuple[str, ...] = ()
    #: False when a name was cut at 200 characters or there were more than 50: the list then
    #: may be missing the very name that matters, and a draft control is refused.
    own_names_complete: bool = True


DEFAULT_ACTION_TIMEOUT_S = 30.0
#: How long a page that was just acted on may take to go quiet, and how long its DOM must stay
#: unchanged to count as quiet.
HEADING_REPORT_CHARS = 200
#: Most addresses a recorded navigation chain keeps.
_MAX_CHAIN = 40
#: Seconds added to twice the action timeout for a driver call's deadline.
_DEADLINE_SLACK_S = 10.0
#: The longest the cosmetic window mark may take.
_INDICATOR_DEADLINE_S = 5.0
HEADINGS_REPORTED = 20
SETTLE_GRACE_MS = 300
SETTLE_QUIET_MS = 500
SETTLE_WAIT_S = 4.0
#: How long `--expect-heading` waits for the heading before the click is reported as not landed.
SETTLE_EXPECT_WAIT_S = 15.0

#: Most file names, and most characters of each, reported back from an input (page-supplied).
_MAX_FILES = 10
_MAX_FILE_NAME = 100

#: How long to let a form re-render after a file is set, before reading what it shows.
_UPLOAD_SETTLE_MS = 500


#: Accessible roles a field can be looked up by (a typo would otherwise wait out the timeout).
FIELD_ROLES = frozenset(
    {
        "checkbox",
        "combobox",
        "listbox",
        "radio",
        "searchbox",
        "slider",
        "spinbutton",
        "switch",
        "textbox",
    }
)

#: `autocomplete` values that mark a secret field, which is never read back.
_SECRET_AUTOCOMPLETE = frozenset({"current-password", "new-password", "one-time-code"})

#: Most characters of a field's value read back for comparing (the rest is not looked at).
_MAX_READ_BACK = 100_000

#: Most matches listed when a label is ambiguous.
MAX_LISTED_MATCHES = 12


@dataclass(frozen=True, slots=True)
class FieldTarget:
    """Which field a label means: the narrowing `fill`, `upload` and `select` share.

    `role` looks the field up by accessible role and name instead of by label, `exact` matches
    the whole label (the default is a case-insensitive substring), and `nth` (1-based, DOM
    order) picks one of several matches. `frame` is a child frame number (see `frames`) and
    `frame_host` pins the host that frame must be on; `is_allowed` is the task's host check,
    applied to the frame before anything is touched.
    """

    exact: bool = False
    frame: int | None = None
    frame_host: str | None = None
    is_allowed: Callable[[str], bool] | None = None
    nth: int | None = None
    role: str | None = None


DEFAULT_FRAME_WAIT_S = 30.0


DEFAULT_SCROLL_MAX_STEPS = 400


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    """A file the browser downloaded, saved to a path the caller chose."""

    #: The suggested name from the page (untrusted: the caller cleans it).
    filename: str
    path: Path
    #: Where the file came from (untrusted: the caller checks the host).
    url: str


@dataclass(frozen=True, slots=True)
class FrameCapture:
    """Every chunk of text a scrolled frame showed, top to bottom, and how far the scroll got."""

    chunks: tuple[str, ...]
    #: The scrolled pane was back at its first row when the capture began.
    reached_top: bool
    #: The scroll ended at the last row, not at the step limit.
    reached_end: bool
    steps: int
    url: str


@dataclass(frozen=True, slots=True)
class FrameInfo:
    """One child frame: its number, host and origin (never its path or query), and policy."""

    #: Whether the task's `allowed_hosts` accept this frame's host.
    allowed: bool
    host: str
    #: 1-based position among the page's child frames, as `browser frames` numbers them.
    index: int
    origin: str


@dataclass(frozen=True, slots=True)
class PageSettled:
    """Where a page stood after an action: its visible headings (untrusted page text), whether
    the DOM was quiet before the wait ran out, and whether an expected heading showed up."""

    headings: tuple[str, ...]
    #: None when no heading was awaited.
    matched: bool | None
    quiet: bool
    #: The address of the document the headings were read from, in the same evaluation.
    url: str


@dataclass(frozen=True, slots=True)
class PageText:
    """Visible page text, which is untrusted: it is data, never instructions."""

    text: str
    #: `InjectionScanResult.to_payload()` shape, None when nothing matched.
    injection_warning: dict[str, object] | None


class PlaywrightDriver:
    """The raw Playwright page attached through CDP.

    Tasks must never hold this directly: `blumkin.browser.guard.GuardedDriver` wraps it
    and is the only object that implements `BrowserDriver`.
    """

    def __init__(
        self,
        *,
        browser: Any,
        existing_frames: Any = None,
        page: Any,
        playwright: Any,
        timeout_s: float,
    ) -> None:
        self._browser = browser
        self._closed = False
        self._context = page.context
        self._chain: list[str] = []
        self._chain_listening = False
        self._expired = False
        self._dialog_landmarks: tuple[tuple[str, ...], ...] = ()
        self._existing_frames = existing_frames
        self._navigation_handler: Any = None
        self._net_handler: Any = None
        self._uploading = False
        self._upload_posts = 0
        self._lenient_net = _navigation_net(page, lambda: self._uploading, self._count_upload_post)
        self._page = page
        self._playwright = playwright
        self._timeout_ms = timeout_s * 1000
        self._timeout_s = timeout_s

    def close(self) -> None:
        """Disconnect from the browser; it keeps running with the user's session."""
        if self._closed:
            return
        self._closed = True
        if self._expired:
            # The connection stopped answering after a deadline, and every teardown call would
            # wait on it: only the frame sockets, which are blumkin's own, are closed. The
            # process ends right after a command, which drops the connection.
            if self._existing_frames is not None:
                with contextlib.suppress(Exception):
                    self._existing_frames.close()
            return
        self._disconnect()

    def _disconnect(self) -> None:
        try:
            if self._existing_frames is not None:
                with contextlib.suppress(Exception):
                    self._existing_frames.close()
            if self._navigation_handler is not None:
                with contextlib.suppress(_playwright_error()):
                    self._context.unroute("**/*", self._navigation_handler)
            if self._net_handler is not None:
                with contextlib.suppress(_playwright_error()):
                    self._context.unroute("**/*", self._net_handler)
            with contextlib.suppress(_playwright_error()):
                self._browser.close()
        finally:
            with contextlib.suppress(_playwright_error()):
                self._playwright.stop()

    def aria_snapshot(self) -> str:
        """The page as a Playwright ARIA snapshot: roles and accessible names, no field values."""
        self._require_open()
        try:
            return str(self._page.locator("body").aria_snapshot(timeout=self._timeout_ms))
        except _playwright_error() as exc:
            raise self._action_error("describe the page", exc) from exc

    def block_form_submission(self, *, background_writes: bool = False) -> None:
        """Swallow every non-GET navigation while attached (a form POST), whatever triggers it.

        This is the backstop behind the guard's checks: a form can also be submitted by a
        script, an inherited handler or a field that submits on change, none of which a
        static look at one control can rule out. The route lives only on this connection, so
        it is gone once the driver detaches and never gets in the way of the user's own
        final click.

        With `background_writes` (a task's strict mode) it also swallows every other write
        (`fetch`, `XMLHttpRequest`, a beacon), so a registered click handler cannot post form
        data in the background. That breaks pages that need background writes to work, which
        is why it is opt-in. Strict mode, once installed, is never weakened by a later lenient
        request. Install the net before restricting hosts: both routes end by continuing the
        request, so a net registered after the host route would run first and skip it.
        """
        self._require_open()
        handler = _abort_every_write if background_writes else self._lenient_net
        if self._net_handler is handler or self._net_handler is _abort_every_write:
            return
        previous = self._net_handler
        try:
            self._context.route("**/*", handler)
        except _playwright_error() as exc:
            raise self._action_error("install the form-submission safety net", exc) from exc
        self._net_handler = handler
        if previous is not None:
            # The new route went in before the old one comes out, so there is no gap. If the
            # old one cannot be removed it stays behind the stricter one, which is harmless.
            with contextlib.suppress(_playwright_error()):
                self._context.unroute("**/*", previous)

    def checked_click(
        self,
        name: str,
        role: str,
        vet: Callable[[ControlInfo], None],
        nth: int | None = None,
    ) -> None:
        """Resolve one element, let `vet` inspect that very element, then click it.

        Inspecting and clicking the same element handle leaves no window in which the page
        could swap in a different element between the check and the click. With `nth` (1-based,
        DOM order) a name the page legitimately repeats is narrowed to that one match; without
        it, more than one match is refused.
        """
        handle = self._resolve(name, role, nth)
        try:
            vet(self._describe(handle))
            # A swallowed submit never commits a navigation, so do not wait for one.
            handle.click(timeout=self._timeout_ms, no_wait_after=True)
        except _playwright_error() as exc:
            raise self._action_error(f"click the {role} named {name!r}", exc) from exc

    def checked_download(
        self,
        name: str,
        role: str,
        vet: Callable[[ControlInfo], None],
        *,
        save_to: Path,
        timeout_s: float,
    ) -> DownloadedFile:
        """Resolve one element, vet it, click it and save the download it starts to `save_to`.

        The file lands at `save_to/download.bin`; its name and address, which the page
        controls, are returned for the caller to check.
        """
        self._require_open()
        try:
            handle = self._page.get_by_role(role, name=name, exact=True).element_handle(
                timeout=self._timeout_ms
            )
        except _playwright_error() as exc:
            raise self._action_error(f"find the {role} named {name!r}", exc) from exc
        target = save_to / "download.bin"
        try:
            vet(self._describe(handle))
            with self._page.expect_download(timeout=timeout_s * 1000) as started:
                handle.click(timeout=self._timeout_ms, no_wait_after=True)
            download = started.value
            download.save_as(str(target))
        except _playwright_error() as exc:
            raise self._action_error(f"download from the {role} named {name!r}", exc) from exc
        return DownloadedFile(
            filename=str(download.suggested_filename), path=target, url=str(download.url)
        )

    def checked_pick(
        self,
        name: str,
        role: str,
        vet: Callable[[ControlInfo], None],
        nth: int | None = None,
        prefix: bool = False,
    ) -> None:
        """Choose a row (or cell) by its text; `vet` also learns whether it holds a control.

        Like `checked_click`, the very element that was vetted is the one clicked. A click on a
        row lands in its middle, where a control of its own could sit, and a control around
        it would take the click too, so `holds_controls` tells the guard when the row holds,
        sits inside or is made of one (see `_PICK_BLOCKERS_JS`).
        """
        handle, locator = self._resolve_with_locator(name, role, nth, prefix=prefix)
        try:
            info = dataclasses.replace(
                self._describe(handle), holds_controls=bool(handle.evaluate(_PICK_BLOCKERS_JS))
            )
            if prefix:
                # The accessible name a prefix matched can come from anywhere (a label elsewhere,
                # an aria-label): the element's own accessibility snapshot holds all of it.
                # The element that will be clicked is marked with a one-time token, and the
                # snapshot is read through that mark: a page that swaps in another element
                # while it is read (even one that puts the original back) has no marked element
                # to show, so the names checked are only ever the clicked element's.
                # (A pick is always a lookup in the page's own document, as `_resolve` makes it,
                # so the marked element is looked up there too.)
                token = uuid.uuid4().hex
                handle.evaluate("(e, t) => e.setAttribute('data-blumkin-pick', t)", token)
                try:
                    marked = self._page.locator(f'[data-blumkin-pick="{token}"]')
                    shown = str(marked.aria_snapshot(timeout=self._timeout_ms))
                    again = marked.element_handle(timeout=self._timeout_ms)
                    if not handle.evaluate("(a, b) => a === b", again):
                        raise BrowserActionError(
                            f"Expected the {role} named {name!r} to stay the same element "
                            "while it was checked, got another one; nothing was clicked"
                        )
                finally:
                    # Every element that carries the token, not only the original: a page that
                    # clones the element while it is read copies the attribute with it.
                    with contextlib.suppress(_playwright_error()):
                        self._page.evaluate(
                            "t => document.querySelectorAll('[data-blumkin-pick]').forEach("
                            "e => { if (e.getAttribute('data-blumkin-pick') === t) "
                            "e.removeAttribute('data-blumkin-pick'); })",
                            token,
                        )
                info = dataclasses.replace(
                    info,
                    own_names=(*info.own_names, shown[:_PICK_SNAPSHOT_CHARS]),
                    own_names_complete=info.own_names_complete
                    and len(shown) <= _PICK_SNAPSHOT_CHARS,
                )
            vet(info)
            handle.click(timeout=self._timeout_ms, no_wait_after=True)
        except _playwright_error() as exc:
            raise self._action_error(f"pick the {role} named {name!r}", exc) from exc

    def describe_field(self, label: str, target: FieldTarget | None = None) -> ControlInfo:
        """Describe the element a label resolves to (for example to see it is a button).

        With `target` the field is found as `fill` finds it (exact, role, `nth`, frame).
        """
        self._require_open()
        try:
            if target is None:
                handle = self._page.get_by_label(label).element_handle(timeout=self._timeout_ms)
            else:
                scope = self._scope(target)
                locator = self._field(scope, label, target, prefer_visible=True)
                self._still_allowed(scope, target)
                handle = locator.element_handle(timeout=self._timeout_ms)
        except _playwright_error() as exc:
            raise self._action_error(f"find the field labelled {label!r}", exc) from exc
        try:
            return self._describe(handle)
        except _playwright_error() as exc:
            raise self._action_error(f"inspect the field labelled {label!r}", exc) from exc

    def dismiss(self) -> None:
        """Press Escape on the page, which closes an open dialog or menu; nothing else."""
        self._require_open()
        try:
            self._page.keyboard.press("Escape")
        except _playwright_error() as exc:
            raise self._action_error("press Escape on the page", exc) from exc

    def edit_cell(
        self,
        row: str,
        column: str,
        value: str | None,
        grid: int,
        vet: Callable[[ControlInfo], None],
        vet_name: Callable[[str], None],
    ) -> CellEdit:
        """Read one cell of an Oracle JET data grid; with a `value`, select it, type and commit.

        The cell is found by row and column (see `_CELL_JS`) and inspected by `vet` as the very
        element that is clicked, and a dropdown option is vetted the same way (its element by
        `vet`, its text by `vet_name`) before it is clicked. With no `value` nothing is clicked
        or typed, so a read cannot
        change the cell's mode. With one, nothing is clicked if a control sits at the click
        point, and keys go only where an editor (or the cell itself) has focus inside the data
        grid. Enter, which could submit a form, is never pressed. The cell is read again
        afterwards.
        """
        self._require_open()
        try:
            handle, meta = self._find_cell(row, column, grid)
            vet(self._describe(handle))
            if value is not None:
                self._write_cell(handle, value, grid, vet, vet_name)
                handle, meta = self._read_cell_after_edit(row, column, grid)
            text = str(handle.evaluate(_CELL_TEXT_JS))
        except _playwright_error() as exc:
            raise self._action_error(
                f"edit the cell at row {row!r}, column {column!r}", exc
            ) from exc
        return CellEdit(
            column=int(meta["column"]),
            column_label=str(meta["columnLabel"]),
            columns=int(meta["columns"]),
            edited=value is not None,
            row=int(meta["row"]),
            row_label=str(meta["rowLabel"]),
            rows=int(meta["rows"]),
            text=text,
        )

    def fill(self, label: str, value: str, target: FieldTarget | None = None) -> str | None:
        """Fill one field and return the value it holds afterwards (None when not readable).

        The read-back lets a caller tell when an autocomplete did not resolve: the field can
        hold the canonical lookup value, not the text that was typed. A password is never read.
        """
        self._require_open()
        target = target or FieldTarget()
        try:
            scope = self._scope(target)
            locator = self._field(scope, label, target, prefer_visible=True)
            self._still_allowed(scope, target)
            locator.fill(value, timeout=self._timeout_ms)
            return self._read_back(locator)
        except _playwright_error() as exc:
            error = self._action_error(f"fill the field labelled {label!r}", exc)
            if value:  # a Playwright message can quote the typed text; keep it out of errors
                error = BrowserActionError(str(error).replace(value, "<value>"))
            raise error from None  # the original message can quote the value

    def frame_aria_snapshot(
        self,
        index: int,
        is_allowed: Callable[[str], bool],
        expect_host: str | None = None,
        *,
        wait_s: float = 0.0,
    ) -> str:
        """A child frame's body as a Playwright ARIA snapshot, on an allowed host only."""
        self._wait_for_frame(index, wait_s)
        frame = self._child_frame(index, is_allowed, expect_host)
        try:
            snapshot = str(frame.locator("body").aria_snapshot(timeout=self._timeout_ms))
        except _playwright_error() as exc:
            raise self._action_error(f"describe frame {index}", exc) from exc
        self._require_allowed_frame_host(frame, is_allowed)
        return snapshot

    def frame_list(
        self, is_allowed: Callable[[str], bool], *, wait_s: float = 0.0
    ) -> tuple[FrameInfo, ...]:
        """The page's child frames, numbered from 1; each shows its host and origin only.

        A frame in another origin is reported to a fresh connection a moment after it
        attaches, and with no address at first, so `wait_s` waits for at least one frame that
        has a web address (every frame, once there is one) before listing.
        """
        self._require_open()
        deadline = time.monotonic() + clamp_wait(wait_s)
        while True:
            # One read of each address per poll: a frame's address is a live read, and the
            # listing must be built from the same values the wait judged.
            addresses = [str(frame.url) for frame in self._child_frames()]
            if _all_addressed(addresses) or time.monotonic() >= deadline:
                break
            self._pause()
        found: list[FrameInfo] = []
        for number, address in enumerate(addresses, start=1):
            host = _web_host(address)
            parts = urlsplit(address)
            port = f":{parts.port}" if host and parts.port else ""
            found.append(
                FrameInfo(
                    allowed=bool(host) and is_allowed(host),
                    host=host,
                    index=number,
                    origin=f"{parts.scheme}://{host}{port}" if host else "",
                )
            )
        return tuple(found)

    def mark_window(
        self, task: str, hosts: Sequence[str] = (), blocked: Sequence[str] = ()
    ) -> None:
        """Show the task's indicator on the page (best effort: a page that refuses is skipped).

        The page's own address is checked in the evaluation that adds the pill: off `hosts`, or
        on a `blocked` host, nothing is added.
        """
        if self._expired:
            return  # the page stopped answering: nothing cosmetic is worth another wait
        self._require_open()
        # Cosmetic: a page that refuses it, or hangs, is skipped, and never for more than a few
        # seconds (a bare page evaluation has no timeout of its own).
        with contextlib.suppress(_playwright_error(), BrowserActionError):
            with self._bounded(_INDICATOR_DEADLINE_S, "mark the window"):
                self._page.evaluate(INDICATOR_JS, indicator_args(task, hosts, blocked))

    def goto(self, url: str) -> None:
        self._require_open()
        checked_http_url(url)
        try:
            self._page.goto(url, timeout=self._timeout_ms)
        except _playwright_error() as exc:
            raise self._action_error(f"open {url!r}", exc) from exc

    def matching_roles(
        self, name: str, roles: Sequence[str], wait_s: float, nth: int | None = None
    ) -> tuple[str, ...]:
        """The roles in which a control named `name` exists, waiting up to `wait_s` for one.

        Polls the page's own accessibility tree (a count, not a click), so a wrong role or a
        missing control is reported after `wait_s`, not after the 30 second action timeout.
        Two controls with that name in one role are refused as ambiguous, unless `nth` (1-based,
        DOM order) says which one is meant. That never hides a second role: a name that exists in
        more than one role is returned as such, so the caller still has to choose. With one role,
        it counts as found once it has at least that many; if it never does within the wait, the
        largest count seen is reported.
        """
        self._require_open()
        deadline = time.monotonic() + clamp_wait(wait_s)
        short: tuple[int, str] | None = None
        while True:
            found: list[str] = []
            present: list[tuple[str, int]] = []
            for role in roles:
                try:
                    # Each count is bounded, not the whole wait, which `--wait` sets.
                    with self._bounded(self._call_deadline_s(), "count the controls"):
                        count = int(self._page.get_by_role(role, name=name, exact=True).count())
                except _playwright_error() as exc:
                    raise self._action_error(f"look for a control named {name!r}", exc) from exc
                if nth is not None:
                    if count > 0:
                        present.append((role, count))
                    continue
                if count > 1:
                    raise BrowserActionError(
                        f"Expected one {role} named {name!r}, got {count} {role}s named {name!r}"
                    )
                if count == 1:
                    found.append(role)
            if nth is not None:
                if len(present) > 1:
                    # The name exists in more than one role, however many matches each has:
                    # `nth` must not hide that, so the caller still has to choose a role.
                    return tuple(role for role, _ in present)
                if present:
                    role, count = present[0]
                    if count >= nth:
                        found.append(role)
                    elif short is None or count >= short[0]:
                        # Fewer than `nth` is not final while the wait runs: a list may still
                        # be rendering. The largest count seen is reported if it never grows.
                        short = (count, role)
            if found or time.monotonic() >= deadline:
                if not found and short is not None:
                    raise BrowserActionError(
                        f"Expected at least {nth} {short[1]}s named {name!r}, got {short[0]}"
                    )
                return tuple(found)
            self._pause()

    def raw_press(self, label: str, key: str, target: FieldTarget | None = None) -> None:
        self._require_open()
        try:
            if target is None:
                self._page.get_by_label(label).press(key, timeout=self._timeout_ms)
            else:
                scope = self._scope(target)
                locator = self._field(scope, label, target, prefer_visible=True)
                self._still_allowed(scope, target)
                locator.press(key, timeout=self._timeout_ms)
        except _playwright_error() as exc:
            raise self._action_error(f"press {key!r} in the field labelled {label!r}", exc) from exc

    def press_field(
        self,
        label: str,
        key: str,
        target: FieldTarget | None,
        vet: Callable[[ControlInfo], None],
    ) -> None:
        """Press `key` in the field `label` names, after `vet` has seen that same element.

        One handle is inspected and pressed, so a re-render between the check and the key
        cannot put the key into a different element (which `--nth` makes likelier).
        """
        self._require_open()
        target = target or FieldTarget()
        try:
            scope = self._scope(target)
            locator = self._field(scope, label, target, prefer_visible=True)
            handle = locator.element_handle(timeout=self._timeout_ms)
            vet(self._describe(handle))
            self._still_allowed(scope, target)
            handle.press(key, timeout=self._timeout_ms)
        except _playwright_error() as exc:
            raise self._action_error(f"press {key!r} in the field labelled {label!r}", exc) from exc

    def read_field(self, label: str, target: FieldTarget | None = None) -> FieldReading:
        """Read one field's value, for a comparison made by the caller (never printed).

        The field is found exactly as `fill` finds it. A missing field, an ambiguous label and
        a password or other secret field are reported as such, with no value.
        """
        self._require_open()
        target = target or FieldTarget()
        try:
            scope = self._scope(target)
            try:
                locator = self._field(scope, label, target, prefer_visible=True)
            except AmbiguousFieldError:
                return FieldReading("ambiguous")
            except MissingFieldError:
                return FieldReading("missing")
            self._still_allowed(scope, target)
            value = self._read_value(locator)
            self._still_allowed(scope, target)  # the frame must still be allowed after the read
        except _playwright_error() as exc:
            if _is_timeout(exc):
                return FieldReading("missing")
            raise self._action_error(f"read the field labelled {label!r}", exc) from exc
        if value is None or len(value) >= _MAX_READ_BACK:  # a clipped value is not the value
            return FieldReading("unreadable")
        return FieldReading("read", value)

    def read_frame_text(
        self,
        index: int,
        is_allowed: Callable[[str], bool],
        expect_host: str | None = None,
        *,
        wait_s: float = 0.0,
    ) -> PageText:
        """A child frame's visible text, on an allowed host only; untrusted data.

        With `wait_s`, waits that long for the frame to attach and to have text, so a pane that
        loads late is not read empty.
        """
        self._wait_for_frame(index, wait_s)
        frame = self._child_frame(index, is_allowed, expect_host)
        text = self._frame_text(frame, index, wait_s)
        self._require_allowed_frame_host(frame, is_allowed)
        return PageText(
            text=text,
            injection_warning=scan_for_injection(text, location="frame.text").to_payload(),
        )

    def pick_account(
        self,
        email: str,
        hosts: Sequence[str],
        blocked: Sequence[str],
        vet_names: Callable[[Sequence[str]], bool] | None = None,
    ) -> AccountPick:
        """Click the one account tile bound to ``email`` on an identity provider's picker.

        Everything is decided in one evaluation on the page itself, so a redirect between a
        check and the click cannot change what is judged: the page must be https on one of
        ``hosts`` and not on one of ``blocked`` (the task's own sites); it must show no field
        to type into (a password or code page is not a picker); and exactly one clickable
        element, outermost, must carry the address as a whole word in its test id, label or
        text. Anything else is reported, not clicked. Nothing is typed. ``vet_names`` (the guard's
        finalizing-name check) sees the tile's names before the click; the click then re-judges
        the same element on the page and requires the same names, so a tile changed in between
        is not clicked.
        """
        self._require_open()
        try:
            with self._bounded(self._timeout_s, "pick the account on the sign-in page"):
                return self._pick_account(email, hosts, blocked, vet_names)
        except _playwright_error() as exc:
            raise self._action_error("pick the account on the sign-in page", exc) from exc

    def _pick_account(
        self,
        email: str,
        hosts: Sequence[str],
        blocked: Sequence[str],
        vet_names: Callable[[Sequence[str]], bool] | None,
    ) -> AccountPick:
        """The work of `pick_account`, run under its deadline."""
        try:
            args: dict[str, Any] = {"blocked": list(blocked), "email": email, "hosts": list(hosts)}
            handle = self._page.evaluate_handle(_PICK_ACCOUNT_JS, args)
            status = str(handle.get_property("0").json_value())
            count = int(handle.get_property("2").json_value() or 0)
            if status != "ok":
                return AccountPick(status=status, tiles=count)
            names = [str(n) for n in handle.get_property("3").json_value() or ()]
            if vet_names is not None and not vet_names(names):
                return AccountPick(status="denied", tiles=1)
            element = handle.get_property("1").as_element()
            if element is None:
                return AccountPick(status="none", tiles=0)
            clicked = self._page.evaluate(
                _PICK_ACCOUNT_JS, {**args, "click": element, "names": names}
            )
            status = str(clicked[0])
        except _playwright_error() as exc:
            raise self._action_error("pick the account on the sign-in page", exc) from exc
        return AccountPick(status=status, tiles=count)

    def read_text(self) -> PageText:
        self._require_open()
        try:
            text = str(self._page.inner_text("body", timeout=self._timeout_ms))
        except _playwright_error() as exc:
            raise self._action_error("read the page text", exc) from exc
        return PageText(
            text=text,
            injection_warning=scan_for_injection(text, location="page.text").to_payload(),
        )

    def clear_session(self) -> int:
        """Delete every cookie the browser holds, returning how many there were.

        This signs the profile out of every site, the identity provider included, and nothing
        else: its folder, its extensions and its other saved site data stay.
        """
        self._require_open()
        try:
            context = self._page.context
            count = len(context.cookies())
            context.clear_cookies()
        except _playwright_error() as exc:
            raise self._action_error("clear the saved sign-in", exc) from exc
        return count

    def restrict_navigation(self, is_allowed: Callable[[str], bool]) -> None:
        """Abort every main-frame navigation to a host `is_allowed` refuses, before it loads.

        Pages that are not web addresses (`about:blank`, an error page) and requests that are
        not navigations (scripts, images, background calls) are left alone. Like the
        form-submission net, the route lives only on this connection.
        """
        self._require_open()
        if self._navigation_handler is not None:
            return

        def handler(route: Any) -> None:
            request = route.request
            if request.is_navigation_request() and request.frame.parent_frame is None:
                parts = urlsplit(request.url)
                if parts.scheme in ("http", "https") and not is_allowed(parts.hostname or ""):
                    route.abort()
                    return
            route.fallback()

        try:
            self._context.route("**/*", handler)
        except _playwright_error() as exc:
            raise self._action_error("restrict navigation to the allowed hosts", exc) from exc
        self._navigation_handler = handler

    @contextlib.contextmanager
    def sign_in_pass(
        self,
        is_sign_in_host: Callable[[str], bool],
        is_task_host: Callable[[str], bool] | None = None,
    ) -> Iterator[None]:
        """While the user signs in by hand, let identity-provider requests past the safety net.

        A login form POST, or the identity provider's POST back to the site, would otherwise be
        answered with an empty page and the sign-in could never finish. Only a request that goes
        to a sign-in host or comes from a page on one is let through, and only inside this block.
        With ``is_task_host``, a top-level navigation out of a sign-in page may go only to a
        sign-in host or a task host; any other is left to the navigation guard.
        """
        self._require_open()
        handler = _sign_in_handler(is_sign_in_host, is_task_host)
        try:
            self._context.route("**/*", handler)
        except _playwright_error() as exc:
            raise self._action_error("let the sign-in through", exc) from exc
        try:
            yield
        finally:
            with contextlib.suppress(_playwright_error()):
                self._context.unroute("**/*", handler)

    def scroll_frame_text(
        self, is_allowed: Callable[[str], bool], *, max_steps: int, wait_s: float
    ) -> FrameCapture:
        """Scroll the biggest scrollable pane of an allowed child frame, collecting its text.

        Virtualized lists keep only the rows near the viewport in the page, so one read sees
        a fraction of them. This waits up to `wait_s` for a child frame whose host
        `is_allowed` accepts, scrolls its largest scrollable element from the top in
        viewport-sized steps and records the frame's text at each one. Read-only: the pane
        is scrolled, nothing is clicked or typed.
        """
        self._require_open()
        deadline = time.monotonic() + wait_s
        frame = None
        while frame is None:
            frame = self._ready_frame(is_allowed)
            if frame is not None:
                break
            if time.monotonic() >= deadline:
                raise BrowserActionError(
                    f"Expected a child frame on an allowed host with text within {wait_s:g}s, "
                    "got none (is the pane open and its host in allowed_hosts?)"
                )
            self._page.wait_for_timeout(500)
        try:
            scroll_mode = str(frame.evaluate(teams.SCROLL_PREPARE_JS))
            if scroll_mode == "missing":
                raise BrowserActionError(
                    "Expected a scrollable transcript pane or loaded transcript rows, got neither "
                    "(is the transcript still loading?)"
                )
            if scroll_mode == "fixed":
                self._require_allowed_frame_host(frame, is_allowed)
                text = str(frame.evaluate("document.body.innerText"))
                self._require_allowed_frame_host(frame, is_allowed)
                return FrameCapture(
                    chunks=(text,),
                    reached_end=True,
                    reached_top=True,
                    steps=1,
                    url=frame.url,
                )
            chunks: list[str] = []
            reached_top = bool(frame.evaluate(teams.SCROLL_TO_TOP_JS))
            steps = 0
            reached_end = False
            while steps < max_steps:
                self._require_allowed_frame_host(frame, is_allowed)
                self._page.wait_for_timeout(_SCROLL_SETTLE_MS)
                text = str(frame.evaluate("document.body.innerText"))
                self._require_allowed_frame_host(frame, is_allowed)
                chunks.append(text)
                steps += 1
                step = frame.evaluate(teams.SCROLL_STEP_JS)
                if step is None:
                    raise BrowserActionError(
                        "Expected the transcript pane to stay in the page, got a missing or "
                        "detached pane"
                    )
                if step:
                    reached_end = True
                    break
            return FrameCapture(
                chunks=tuple(chunks),
                reached_end=reached_end,
                reached_top=reached_top,
                steps=steps,
                url=frame.url,
            )
        except _playwright_error() as exc:
            raise self._action_error("scroll the frame", exc) from exc

    def set_dialog_landmarks(self, groups: Sequence[Sequence[str]]) -> None:
        """Treat the smallest container holding a control of each name of a group as a dialog.

        For a popup the page does not mark as one (`role=dialog`), one group of names per popup:
        the guard still judges what the container holds before a declared dialog confirm may
        be clicked inside it.
        """
        self._dialog_landmarks = tuple(tuple(group) for group in groups)

    def settle(self, previous_url: str, timeout_s: float = 5.0) -> None:
        """Wait for a navigation the last action started: the URL moving on, then the load.

        A click returns before the page it triggered has committed, so a URL read straight
        away can still be the old one. This waits up to a second for the URL to differ from
        `previous_url` (it returns at once if it already does, or when a navigation commits),
        then up to `timeout_s` for the load. Neither timeout is an error: a click that does
        not navigate leaves the URL alone, and a swallowed form POST never commits.
        """
        self._require_open()
        with contextlib.suppress(_playwright_error()):
            self._page.wait_for_url(lambda url: url != previous_url, timeout=1000)
        with contextlib.suppress(_playwright_error()):
            self._page.wait_for_load_state("load", timeout=timeout_s * 1000)

    def settle_page(
        self,
        *,
        expect_heading: str | None = None,
        expect_wait_s: float = SETTLE_EXPECT_WAIT_S,
        quiet_ms: int = SETTLE_QUIET_MS,
        timeout_s: float = SETTLE_WAIT_S,
    ) -> PageSettled:
        """Wait for the page an action started to settle, then say what heading it ended on.

        Settled means the network went idle and the DOM stayed unchanged for `quiet_ms`; both
        waits share one `timeout_s` deadline and neither is an error (a page that keeps polling
        never goes idle). With `expect_heading`, the page's headings are then polled up to
        `expect_wait_s` for one that contains the text (ignoring case): `matched` says whether
        one did. Nothing on the page is touched.
        """
        self._require_open()
        # One deadline for the whole settle, however many waits it takes.
        deadline = time.monotonic() + timeout_s
        # Playwright's network-idle state can already hold from before the click, and returns
        # at once; a short grace lets the request or update the click started begin first.
        with contextlib.suppress(_playwright_error()):
            self._page.wait_for_timeout(SETTLE_GRACE_MS)

        def left_ms() -> int:
            return max(1, int((deadline - time.monotonic()) * 1000))

        with contextlib.suppress(_playwright_error()):
            self._page.wait_for_load_state("networkidle", timeout=left_ms())
        quiet = False
        for _ in range(2):
            try:
                quiet = bool(
                    self._page.evaluate(_QUIET_DOM_JS, {"quiet": quiet_ms, "total": left_ms()})
                )
                break
            except _playwright_error():
                # A navigation destroyed the page the wait ran in: wait for the new one.
                with contextlib.suppress(_playwright_error()):
                    self._page.wait_for_load_state("load", timeout=left_ms())
        url, headings = self._headings()
        matched: bool | None = None
        if expect_heading is not None:
            wanted = expect_heading.casefold()
            deadline = time.monotonic() + expect_wait_s
            while True:
                matched = any(wanted in heading.casefold() for heading in headings)
                if matched or time.monotonic() >= deadline:
                    break
                with contextlib.suppress(_playwright_error()):
                    self._page.wait_for_timeout(250)
                url, headings = self._headings()
        return PageSettled(
            headings=tuple(text[:HEADING_REPORT_CHARS] for text in headings[:HEADINGS_REPORTED]),
            matched=matched,
            quiet=quiet,
            url=url,
        )

    def list_options(
        self,
        label: str,
        target: FieldTarget | None = None,
        vet: Callable[[ControlInfo], None] | None = None,
        vet_name: Callable[[str], None] | None = None,
    ) -> OptionsListing:
        """List every option of a native dropdown or an ARIA combobox, choosing none.

        A native `<select>` is only read. An ARIA combobox is opened like `select` opens it
        (so `vet` and `vet_name` see it first), its list is scrolled until nothing new appears
        (a virtualized list renders a few rows at a time), and it is closed again with Escape,
        whatever happens in between. At most `_MAX_OPTIONS` options and `_MAX_OPTION_SCROLLS`
        scrolls are taken; `complete` says whether the end was reached.
        """
        self._require_open()
        target = target or FieldTarget()
        opener = None
        try:
            scope = self._scope(target)
            locator = self._field(scope, label, target, prefer_visible=True)
            opener = locator.element_handle(timeout=self._timeout_ms)
            self._still_allowed(scope, target)
            kind = opener.evaluate(_DROPDOWN_INFO_JS)
            if vet_name is not None:
                vet_name(str(opener.evaluate(_MATCH_INFO_JS).get("name", "")))
            if str(kind.get("tag")) == "select":
                self._still_allowed(scope, target)
                names = opener.evaluate(_NATIVE_OPTIONS_JS)
                return OptionsListing(
                    tuple(_clean(str(n), 200) for n in names[:_MAX_OPTIONS]),
                    complete=len(names) <= _MAX_OPTIONS,
                )
            if str(kind.get("role")) == "listbox":
                # A listbox that is already there is only read: clicking it would land on one
                # of its items, so it is neither clicked nor closed.
                self._still_allowed(scope, target)
                return self._read_open_list(scope, opener, target, popup=locator, close=False)
            if not kind.get("dropdown"):
                role = _clean(str(kind.get("role")), 30)
                raise BrowserActionError(
                    f"Expected a dropdown labelled {label!r}, got a {role} that is not one"
                )
            if vet is not None:
                vet(self._describe(opener))
            self._still_allowed(scope, target)
            # A combobox the page already opened is read where it is and left open: a click
            # on one that toggles would close it and the list would come back empty.
            expanded = str(opener.get_attribute("aria-expanded") or "").lower() == "true"
            if not expanded:
                opener.click(timeout=self._timeout_ms, no_wait_after=True)
            return self._read_open_list(scope, opener, target, close=not expanded)
        except _playwright_error() as exc:
            raise self._action_error(
                f"list the options of the field labelled {label!r}", exc
            ) from exc

    def select(
        self,
        label: str,
        option: str,
        target: FieldTarget | None = None,
        vet: Callable[[ControlInfo], None] | None = None,
        vet_name: Callable[[str], None] | None = None,
    ) -> str | None:
        """Choose `option` in a native dropdown or an ARIA combobox; return what it shows after.

        An ARIA combobox and its option are clicked, so `vet` inspects each element before it
        is clicked (the submit guard) and `vet_name` is shown the resolved field's own accessible
        name (a label that merely contained a safe word can resolve to a finalizing field); a
        native `<select>` is only given a value. The frame, when there is one, is checked again
        right before every write.
        """
        self._require_open()
        target = target or FieldTarget()
        try:
            scope = self._scope(target)
            locator = self._field(scope, label, target, prefer_visible=True)
            # One handle for everything that follows: what is inspected (what kind of field it
            # is, what it is called, what it can submit) is what is acted on, so a re-render
            # cannot swap in a different element between the check and the use.
            opener = locator.element_handle(timeout=self._timeout_ms)
            kind = opener.evaluate(_DROPDOWN_INFO_JS)
            if vet_name is not None:
                vet_name(str(opener.evaluate(_MATCH_INFO_JS).get("name", "")))
            if str(kind.get("tag")) == "select":
                self._still_allowed(scope, target)
                opener.select_option(label=option, timeout=self._timeout_ms)
                return self._selected_text(opener)
            if not kind.get("dropdown"):
                # Whatever the lookup role, only a dropdown is opened: a labelled button or link
                # is for `click`, which has its own allow list.
                role = _clean(str(kind.get("role")), 30)
                raise BrowserActionError(
                    f"Expected a dropdown labelled {label!r}, got a {role} that is not one"
                )
            # An ARIA combobox: open it, then pick the one option with exactly that name.
            if vet is not None:
                vet(self._describe(opener))
            self._still_allowed(scope, target)
            opener.click(timeout=self._timeout_ms, no_wait_after=True)
            # The popup relationship may only appear once the list is open: read it again.
            opened = opener.evaluate(_DROPDOWN_INFO_JS)
            choices = self._popup(scope, str(opened.get("controls", ""))).get_by_role(
                "option", name=option, exact=True
            )
            with contextlib.suppress(_playwright_error()):
                # The list may render a moment after the click; the count below reports none.
                choices.first.wait_for(state="visible", timeout=self._timeout_ms)
            count = int(choices.count())
            if count != 1:
                raise BrowserActionError(
                    f"Expected one option named {option!r} in {label!r}, got {count}"
                )
            chosen = choices.element_handle(timeout=self._timeout_ms)
            if vet is not None:
                vet(self._describe(chosen))
            self._still_allowed(scope, target)
            chosen.click(timeout=self._timeout_ms, no_wait_after=True)
            return self._read_back(locator, aria=True)
        except _playwright_error() as exc:
            raise self._action_error(
                f"select {option!r} in the field labelled {label!r}", exc
            ) from exc

    def navigations(self) -> tuple[str, ...]:
        """The addresses the top-level page moved through since `track_navigations`."""
        return tuple(self._chain)

    def title(self) -> str:
        self._require_open()
        try:
            return str(self._page.title())
        except _playwright_error() as exc:
            raise self._action_error("read the page title", exc) from exc

    def track_navigations(self) -> None:
        """Start recording the addresses the top-level page is sent through (a redirect chain)."""
        self._require_open()
        self._chain.clear()
        on = getattr(self._page, "on", None)
        if on is None or self._chain_listening:
            return
        self._chain_listening = True
        main = self._page.main_frame

        def moved(request: Any) -> None:
            # Every hop of a redirect is a request of its own (a committed document is not: a
            # chain of server redirects commits only the last page).
            if not request.is_navigation_request() or request.frame != main:
                return
            url = str(request.url)
            if self._chain and self._chain[-1] == url:
                return
            self._chain.append(url)
            if len(self._chain) > _MAX_CHAIN:
                # Keep the first half and the most recent hops, dropping the middle of a loop.
                del self._chain[_MAX_CHAIN // 2]

        on("request", moved)

    def _count_upload_post(self) -> None:
        # One post only: the window closes as soon as the file's post has been let through.
        self._upload_posts += 1
        self._uploading = False

    def _hold_for_upload_post(self) -> None:
        """Keep the net open until the frame post of a chosen file shows up, for a short while.

        Oracle ADF sends the file from a hidden frame a moment after it is chosen, so the post
        can arrive after `set_input_files` returned. A page that never posts costs the cap, and
        nothing is waited for in strict mode, where the net lets nothing through.
        """
        for _ in range(_UPLOAD_POST_POLLS):
            if self._upload_posts:
                break
            self._page.wait_for_timeout(_UPLOAD_POST_POLL_MS)
        if self._upload_posts:
            self._page.wait_for_timeout(_UPLOAD_POST_GRACE_MS)

    def upload(
        self, label: str, path: Path, target: FieldTarget | None = None
    ) -> UploadObservation:
        self._require_open()
        target = target or FieldTarget()
        resolved = path.resolve()
        if resolved != path:
            raise BrowserActionError(
                f"Expected a fully resolved path (no symlinks or '..') to upload, got {str(path)!r}"
            )
        if not resolved.is_file():
            raise BrowserActionError(f"Expected an existing file to upload, got {str(path)!r}")
        try:
            try:
                scope = self._scope(target)
                locator = self._field(scope, label, target)
                self._still_allowed(scope, target)
                handle = locator.element_handle(timeout=self._timeout_ms)
                hidden = self._probe(handle, _UPLOAD_INFO_JS) is True
                before = self._probe(handle, _UPLOAD_COUNT_JS, resolved.name)
                # Through the handle the probes read: a form that swaps the input in between
                # would otherwise receive the file while the replaced one is observed.
                # Oracle ADF posts the chosen file from a hidden frame the moment it is
                # chosen; the safety net lets that one frame post through, and only while
                # this runs.
                self._uploading = True
                self._upload_posts = 0
                handle.set_input_files(str(resolved), timeout=self._timeout_ms)
            except _playwright_error() as exc:
                raise self._action_error(f"upload to the field labelled {label!r}", exc) from exc
            if self._net_handler is self._lenient_net:
                try:
                    self._hold_for_upload_post()
                except _playwright_error() as exc:
                    raise self._action_error(
                        f"wait after uploading to the field labelled {label!r}", exc
                    ) from exc
            return self._observe_upload(handle, resolved.name, hidden, before)
        finally:
            self._uploading = False

    @property
    def url(self) -> str:
        return str(self._page.url)

    def wait_ms(self, milliseconds: int) -> None:
        """Pause through Playwright, so page events (a redirect finishing) are delivered."""
        self._require_open()
        try:
            self._page.wait_for_timeout(milliseconds)
        except _playwright_error() as exc:
            raise self._action_error("wait for the page", exc) from exc

    def _cell_focus(self, grid: int) -> str:
        """Where focus is: `cell`, `editor`, `other` (a control inside the grid) or `outside`."""
        return str(self._page.evaluate(_CELL_FOCUS_JS, grid))

    def _commit_typed_cell(
        self,
        value: str,
        vet: Callable[[ControlInfo], None],
        vet_name: Callable[[str], None],
    ) -> None:
        """Commit typed text: pick the matching option of a search-select, else press Tab.

        A list-of-values cell (a project, a time reporting code) opens a dropdown of matches
        while the text is typed and keeps the typed text uncommitted until an option is chosen,
        and Tab alone does not choose one. The options load a moment after the typing, so they
        are waited for, and the same list must be seen twice before anything is clicked. The
        one option whose text equals the typed text (or, failing that, the only one that starts
        with it) is clicked, by its position and only if it still reads the same, and Tab then
        moves focus on, which closes the editor; anything
        else is refused with the options listed, leaving the editor open. The option is vetted
        like a click first: its text by `vet_name` and its element by `vet`, so a submit-type
        control or an option named like a finalizing action is never clicked. Two visible lists are
        refused too, since the options could belong to another widget. A plain cell (hours)
        shows no dropdown and is committed with Tab, and so is a value with nothing to match on
        (only punctuation). Enter is never pressed.
        """
        wanted = _option_key(value)
        drops = self._page.locator(".oj-listbox-drop:visible")
        if wanted:
            try:
                drops.first.wait_for(state="visible", timeout=_CELL_DROP_WAIT_MS)
            except _playwright_error():
                wanted = ""
        if not wanted:
            self._page.keyboard.press("Tab")
            return
        if int(drops.count()) > 1:
            raise BrowserActionError(
                f"Expected one dropdown list for the cell, got {int(drops.count())} on the page"
            )
        rows = drops.first.locator("li[role=row]")
        previous: list[str] | None = None
        texts: list[str] = []
        for _ in range(_CELL_OPTION_POLLS):
            listed = [(i, t.strip()) for i, t in enumerate(rows.all_inner_texts()) if t.strip()]
            texts = [t for _, t in listed]
            keys = [_option_key(t) for t in texts]
            exact = [i for i, k in enumerate(keys) if k == wanted]
            starts = [i for i, k in enumerate(keys) if k.startswith(wanted)]
            chosen = (
                exact if len(exact) == 1 else (starts if not exact and len(starts) == 1 else [])
            )
            if chosen and texts == previous:
                index, text = listed[chosen[0]]
                # One handle is read, vetted and clicked, so a list that is redrawn in between
                # makes it fail (a detached element) and never clicks the row that replaced it.
                option = rows.nth(index).element_handle(timeout=self._timeout_ms)
                if option.inner_text().strip() == text:
                    vet_name(text)
                    vet(self._describe(option))
                    option.click(timeout=self._timeout_ms)
                    # The chosen option stays in the editor until focus leaves the cell, so the
                    # commit is finished with Tab, as for a plain cell.
                    self._page.keyboard.press("Tab")
                    return
            if texts and len(exact) > 1:
                break
            previous = texts
            self._page.wait_for_timeout(_CELL_OPTION_POLL_MS)
        shown = "; ".join(repr(_printable(t)[:60]) for t in texts[:5]) or "none"
        raise BrowserActionError(
            f"Expected one dropdown option matching {value!r}, got {len(texts)}: {shown}"
        )

    def _editor_open(self, grid: int) -> bool:
        """Whether a text editor is still open in the grid, after a commit had time to land."""
        for _ in range(_CELL_COMMIT_POLLS):
            if not self._page.evaluate(_CELL_EDITING_JS, grid):
                return False
            self._page.wait_for_timeout(_CELL_OPTION_POLL_MS)
        return True

    def _find_cell(self, row: str, column: str, grid: int) -> tuple[Any, dict[str, Any]]:
        """The cell element at `row` and `column` of the `grid`-th data grid, and its position."""
        found = self._page.evaluate_handle(_CELL_JS, {"column": column, "grid": grid, "row": row})
        error = found.evaluate("o => o.error || ''")
        if error:
            raise BrowserActionError(str(error))
        element = found.get_property("element").as_element()
        if element is None:
            raise BrowserActionError(f"Expected a cell at row {row!r}, column {column!r}, got none")
        return element, dict(found.evaluate("o => o.meta"))

    def _read_cell_after_edit(self, row: str, column: str, grid: int) -> tuple[Any, dict[str, Any]]:
        """Find the cell again after an edit; if that fails, say that the edit was made."""
        try:
            return self._find_cell(row, column, grid)
        except BrowserActionError as exc:
            raise CellEditedError(
                f"{exc}; the edit itself was made, so check the cell before editing it again"
            ) from exc

    def _require_committed(self, grid: int) -> None:
        """Cancel the edit and fail if an editor is still open after the commit."""
        if self._editor_open(grid):
            self._page.keyboard.press("Escape")
            raise BrowserActionError(
                "Expected the cell to be committed, got an editor still open; "
                "the edit was cancelled"
            )

    def _write_cell(
        self,
        handle: Any,
        value: str,
        grid: int,
        vet: Callable[[ControlInfo], None],
        vet_name: Callable[[str], None],
    ) -> None:
        """Select the cell, type `value` into its editor and commit; clear it when empty.

        Nothing is clicked if a control (a button, a link, a checkbox) is what the click would
        hit inside the cell, or if something else covers it. Typing starts only when the cell
        itself or an editor has focus inside the data grid, and the rest of the value only
        after the first key has opened an editor, so a space can never reach a focused button.
        """
        handle.scroll_into_view_if_needed(timeout=self._timeout_ms)
        target = str(handle.evaluate(_CELL_CLICK_TARGET_JS))
        if target != "ok":
            raise BrowserActionError(
                f"Expected the click on the cell to hit the cell itself, got {target}; "
                "nothing was clicked"
            )
        handle.click(timeout=self._timeout_ms)
        focus = self._cell_focus(grid)
        if focus not in ("cell", "editor"):
            raise BrowserActionError(
                f"Expected focus on the cell or its editor inside the data grid, got {focus}; "
                "nothing was typed"
            )
        if focus == "editor":
            # An editor left open by an earlier attempt still holds its text: replace it.
            self._page.keyboard.press("ControlOrMeta+A")
        if not value:
            self._page.keyboard.press("Delete")
            self._page.keyboard.press("Tab")
            self._require_committed(grid)
            return
        self._page.keyboard.type(value[0])
        if self._cell_focus(grid) != "editor":
            raise BrowserActionError(
                "Expected typing to open an editor inside the data grid, got none; "
                "the rest of the value was not typed"
            )
        if len(value) > 1:
            self._page.keyboard.type(value[1:])
        self._commit_typed_cell(value, vet, vet_name)
        self._require_committed(grid)

    def _headings(self) -> tuple[str, tuple[str, ...]]:
        """The document's address and its visible headings, read in one evaluation."""
        try:
            found = self._page.evaluate(_HEADINGS_JS) or {}
        except _playwright_error():
            return "", ()
        return str(found.get("url", "")), tuple(str(text) for text in found.get("headings", ()))

    def _describe(self, handle: Any) -> ControlInfo:
        raw: dict[str, Any] = {}
        for group in self._dialog_landmarks or ((),):
            found = handle.evaluate(_DESCRIBE_CONTROL_JS, list(group))
            if not raw:
                raw = found
            elif found.get("dialogLandmark") and (
                not raw.get("dialogLandmark")
                or found.get("containerSize", 0) < raw.get("containerSize", 0)
            ):
                # With several popups in one form a group can match the form itself: the
                # nearest container across the groups is the popup the button is in.
                raw = found
        return ControlInfo(
            tag=str(raw["tag"]),
            input_type=str(raw["type"]),
            in_form=bool(raw["form"]),
            has_formaction=bool(raw["formaction"]),
            href=str(raw["href"]),
            inline_handler=str(raw["onclick"]),
            target=str(raw.get("target", "")),
            in_dialog=bool(raw.get("dialog", False)),
            form_in_dialog=bool(raw.get("formInDialog", False)),
            dialog_controls=tuple(str(text) for text in raw.get("dialogControls", ()) or ()),
            dialog_controls_complete=bool(raw.get("dialogComplete", True)),
            dialog_landmark=bool(raw.get("dialogLandmark", False)),
            form_method=str(raw.get("formMethod", "")),
            dialog_is_form=bool(raw.get("dialogIsForm", False)),
            own_names=tuple(str(text) for text in raw.get("ownNames", ()) or ()),
            own_names_complete=bool(raw.get("ownComplete", True)),
        )

    def _resolve(self, name: str, role: str, nth: int | None, *, prefix: bool = False) -> Any:
        """The one element of `role` named exactly `name` (the `nth` match when given).

        With `prefix` its name only has to start with `name` (case-sensitive): a list row whose
        name carries a date or an author after the text the caller knows.
        """
        return self._resolve_with_locator(name, role, nth, prefix=prefix)[0]

    def _resolve_with_locator(
        self, name: str, role: str, nth: int | None, *, prefix: bool = False
    ) -> tuple[Any, Any]:
        """The element `_resolve` finds, and the locator that found it."""
        self._require_open()
        try:
            if prefix:
                locator = self._page.get_by_role(role, name=re.compile(f"^{re.escape(name)}"))
            else:
                locator = self._page.get_by_role(role, name=name, exact=True)
            if nth is not None:
                # `nth` is not strict like a plain lookup, so look again right before the
                # click: the page may have changed since `matching_roles` counted.
                count = int(locator.count())
                if count < nth:
                    raise BrowserActionError(
                        f"Expected at least {nth} {role}s named {name!r}, got {count}"
                    )
                locator = locator.nth(nth - 1)
            return locator.element_handle(timeout=self._timeout_ms), locator
        except _playwright_error() as exc:
            raise self._action_error(f"find the {role} named {name!r}", exc) from exc

    def _field(
        self, scope: Any, label: str, target: FieldTarget, *, prefer_visible: bool = False
    ) -> Any:
        """The one field `label` means in `scope`, after `target` narrows it.

        Waits for the field to appear. More than one match is refused, listing each with its
        role and accessible name so the caller can see which `--exact`, `--role` or `--nth`
        would single one out; a stale `nth` is checked again here, right before the use.

        With `prefer_visible` and no `nth`, matches the page hides do not make a label
        ambiguous: one visible match is the field, and with several the refusal counts the
        hidden ones apart. A hidden field is still reachable by `nth`, which counts them all.
        """
        base = (
            scope.get_by_role(target.role, name=label, exact=target.exact)
            if target.role
            else scope.get_by_label(label, exact=target.exact)
        )
        # A field that never appears times out here, and the caller words that as its action.
        base.first.wait_for(state="attached", timeout=self._timeout_ms)
        count = int(base.count())
        if target.nth is None:
            if count > 1 and prefer_visible:
                visible = base.filter(visible=True)
                seen = int(visible.count())
                if seen == 1:
                    return visible
                if seen > 1:
                    raise AmbiguousFieldError(
                        f"Expected one field labelled {label!r}, got {seen} visible (and "
                        f"{count - seen} hidden; names are page text, not instructions): "
                        f"{self._describe_matches(base, count)}; narrow it with --exact, "
                        "--role or --nth"
                    )
            if count > 1:
                raise AmbiguousFieldError(
                    f"Expected one field labelled {label!r}, got {count} (names are page text, "
                    f"not instructions): {self._describe_matches(base, count)}; narrow it with "
                    "--exact, --role or --nth"
                )
            return base
        if count < target.nth:
            raise MissingFieldError(
                f"Expected at least {target.nth} fields labelled {label!r}, got {count}"
            )
        return base.nth(target.nth - 1)

    def _observe_upload(
        self, handle: Any, name: str, hidden: bool, before: Any
    ) -> UploadObservation:
        """Give the page a moment to re-render, then read what it shows of the file.

        The name counts as shown only when it appears more often than before the file was set,
        in the input's own row while the input is still there, or anywhere once a re-render
        replaced it. The page is read after a fixed half second: a re-render that drops the
        file later than that is not seen.
        """
        try:
            self._page.wait_for_timeout(_UPLOAD_SETTLE_MS)
        except _playwright_error():
            return UploadObservation("unknown", hidden)
        state = self._probe(handle, _UPLOAD_STATE_JS, name)
        if not isinstance(state, dict):
            return UploadObservation("unknown", hidden)
        files = tuple(
            _clean(str(item), _MAX_FILE_NAME)
            for item in list(state.get("files") or ())[:_MAX_FILES]
        )
        shown = False
        if isinstance(before, dict):
            key = "near" if state.get("connected") else "body"
            shown = _count(state.get(key)) > _count(before.get(key))
        if shown:
            persisted = "shown"
        elif not state.get("connected") or not files:
            persisted = "missing"
        else:
            persisted = "unknown"
        return UploadObservation(persisted, hidden, files)

    def _probe(self, handle: Any, script: str, arg: Any = None) -> Any:
        """Evaluate a read-only script on an element; None when the page refuses."""
        try:
            return handle.evaluate(script, arg) if arg is not None else handle.evaluate(script)
        except _playwright_error():
            return None

    def _describe_matches(self, base: Any, count: int) -> str:
        """Each match as `N. role 'name'` (page-supplied names, one line each, capped)."""
        parts = []
        for index in range(min(count, MAX_LISTED_MATCHES)):
            try:
                info = (
                    base.nth(index)
                    .element_handle(timeout=self._timeout_ms)
                    .evaluate(_MATCH_INFO_JS)
                )
                role, name = str(info.get("role", "")), str(info.get("name", ""))
            except _playwright_error():
                role, name, info = "?", "?", {}
            shown = "" if info.get("visible", True) else " (hidden)"
            parts.append(f"{index + 1}. {_clean(role, 30)} {_clean(name, 40)!r}{shown}")
        more = f" (and {count - MAX_LISTED_MATCHES} more)" if count > MAX_LISTED_MATCHES else ""
        return "; ".join(parts) + more

    def _read_back(self, locator: Any, *, aria: bool = False) -> str | None:
        """The value a field holds now (whole, up to a large cap); never a password's.

        Callers compare it whole and show only a cleaned, shortened form. `aria` also reads a
        widget that is not an `<input>` (a `div` or `button` combobox) from what it displays.
        """
        try:
            if str(locator.get_attribute("type", timeout=self._timeout_ms) or "").lower() == (
                "password"
            ):
                return None
            tokens = str(locator.get_attribute("autocomplete", timeout=self._timeout_ms) or "")
            if _SECRET_AUTOCOMPLETE.intersection(tokens.lower().split()):
                return None
            return str(locator.input_value(timeout=self._timeout_ms))[:_MAX_READ_BACK]
        except _playwright_error():
            if not aria:
                return None
        try:
            return str(locator.evaluate(_ARIA_VALUE_JS))[:_MAX_READ_BACK]
        except _playwright_error():
            return None

    def _read_value(self, locator: Any) -> str | None:
        """A field's value, with the secret check and the read made on the same element at once.

        None for a password or a field marked as a one-time or password code, and for anything
        that cannot be read: a failure closes the read instead of opening it.
        """
        try:
            value = locator.evaluate(_SAFE_VALUE_JS, sorted(_SECRET_AUTOCOMPLETE))
        except _playwright_error():
            return None
        return None if value is None else str(value)[:_MAX_READ_BACK]

    def _scope(self, target: FieldTarget) -> Any:
        """The page, or the child frame `target` names (refused unless its host is allowed)."""
        if target.frame is None:
            return self._page
        if target.is_allowed is None:
            raise BrowserActionError("Expected allowed hosts to use a frame, got none")
        frame = self._child_frame(target.frame, target.is_allowed, target.frame_host)
        if not hasattr(frame, "get_by_label"):
            # A cross-origin frame reached over its own DevTools socket can be read, not driven.
            raise BrowserActionError(
                f"Expected frame {target.frame} to accept input, got a frame that can only be read"
            )
        return frame

    def _popup(self, scope: Any, controls: str) -> Any:
        """Where a combobox's options live: the element it controls.

        With no usable `aria-controls`/`aria-owns` the whole scope is searched, but only when it
        shows at most one visible list: with several, an option could belong to another
        dropdown, so the select is refused instead of guessed (hidden lists are not counted, as
        the option lookup does not see them either).
        """
        popup_id = controls.split()[0] if controls.split() else ""
        if popup_id and re.fullmatch(r"[A-Za-z0-9_.:\-]+", popup_id):
            return scope.locator(f'[id="{popup_id}"]')
        lists = int(scope.locator('[role="listbox"]:visible').count())
        if lists > 1:
            raise BrowserActionError(
                f"Expected the dropdown to name its list (aria-controls), got none while the "
                f"page shows {lists} lists"
            )
        return scope

    def _read_open_list(
        self,
        scope: Any,
        opener: Any,
        target: FieldTarget,
        *,
        popup: Any = None,
        close: bool = True,
    ) -> OptionsListing:
        """Collect the options of the list `opener` just opened, then close it with Escape.

        With `popup` and no `close` the list is one that was already on the page: it is read
        where it is and left alone.
        """
        seen: list[str] = []
        complete = False
        try:
            controls = ""
            if popup is None:
                opened = opener.evaluate(_DROPDOWN_INFO_JS)
                controls = str(opened.get("controls", ""))
                popup = self._popup(scope, controls)
            options = popup.get_by_role("option")
            with contextlib.suppress(_playwright_error()):
                options.first.wait_for(state="visible", timeout=self._timeout_ms)
            # With no list named by `aria-controls` the popup is the whole page or frame, and
            # the list to scroll is the one visible listbox.
            lists = scope.locator('[role="listbox"]:visible') if popup is scope else popup
            # A virtualized list can open part way down (at the selected item): go to the
            # top first, so the walk down reaches every option.
            lists.first.evaluate(_SCROLL_TOP_JS)
            self._page.wait_for_timeout(120)
            for _ in range(_MAX_OPTION_SCROLLS):
                self._still_allowed(scope, target)
                names = options.evaluate_all(_OPTION_NAMES_JS)
                self._still_allowed(scope, target)  # a frame can navigate during the read
                fresh = [str(n) for n in names if str(n) and str(n) not in seen]
                seen.extend(fresh)
                if len(seen) >= _MAX_OPTIONS:
                    break
                moved = lists.first.evaluate(_SCROLL_LIST_JS)
                if not moved:
                    complete = True
                    break
                self._page.wait_for_timeout(120)
        finally:
            closed = self._close_list(scope, opener, target) if close else True
        return OptionsListing(
            tuple(_clean(name, 200) for name in seen[:_MAX_OPTIONS]),
            complete=complete,
            closed=closed,
        )

    def _close_list(self, scope: Any, opener: Any, target: FieldTarget) -> bool:
        """Close an open list without choosing; True when no list is visible afterwards.

        No key is sent into a frame that has left the allowed hosts.
        """
        try:
            self._still_allowed(scope, target)
        except BrowserActionError:
            return False
        with contextlib.suppress(_playwright_error()):
            opener.press("Escape", timeout=self._timeout_ms)
        try:
            return int(scope.locator('[role="listbox"]:visible').count()) == 0
        except _playwright_error():
            return False

    def _selected_text(self, locator: Any) -> str | None:
        try:
            return str(locator.evaluate(_SELECTED_TEXT_JS))[:_MAX_READ_BACK]
        except _playwright_error():
            return None

    def _still_allowed(self, scope: Any, target: FieldTarget) -> None:
        """Check a frame's host again right before a write: a frame can navigate while waited on."""
        if target.frame is not None and target.is_allowed is not None:
            self._require_allowed_frame_host(scope, target.is_allowed)
            pinned = target.frame_host
            host = _web_host(scope.url)
            if pinned is not None and host.lower() != pinned.strip().lower():
                raise BrowserActionError(
                    f"Expected frame {target.frame} to stay on {pinned!r}, got {host!r}; "
                    "list the frames again"
                )

    def _action_error(self, what: str, exc: Exception) -> BrowserActionError:
        return BrowserActionError(
            f"Expected to {what} (timeout {self._timeout_s} seconds), got: "
            f"{str(exc).splitlines()[0] if str(exc) else type(exc).__name__}"
        )

    def _child_frame(
        self, index: int, is_allowed: Callable[[str], bool], expect_host: str | None = None
    ) -> Any:
        """Child frame number `index` (1-based), refused unless its host is allowed.

        `expect_host` pins the frame the caller meant: frame numbers shift when the page adds
        or reloads a frame, so a different frame at that number is refused.
        """
        self._require_open()
        frames = self._child_frames()
        if not frames:
            raise BrowserActionError("Expected a child frame, got no child frames on the page")
        if not 1 <= index <= len(frames):
            raise BrowserActionError(
                f"Expected a frame number between 1 and {len(frames)}, got {index}"
            )
        frame = frames[index - 1]
        self._require_allowed_frame_host(frame, is_allowed)
        host = _web_host(frame.url)
        if expect_host is not None and host.lower() != expect_host.strip().lower():
            raise BrowserActionError(
                f"Expected frame {index} to be on {expect_host!r}, got {host!r}; "
                "list the frames again"
            )
        return frame

    def _frame_text(self, frame: Any, index: int, wait_s: float) -> str:
        """The frame's text; with `wait_s`, waits for it to be non-blank.

        A read that fails while the frame is still navigating is retried until the deadline.
        """
        wait = clamp_wait(wait_s)
        deadline = time.monotonic() + wait
        while True:
            try:
                text = str(frame.evaluate("document.body.innerText"))
            except (_playwright_error(), _raw_frame_error()) as exc:
                if wait <= 0.0 or time.monotonic() >= deadline:
                    raise self._action_error(f"read frame {index}", exc) from exc
                self._pause()
                continue
            if text.strip() or wait <= 0.0:
                return text
            if time.monotonic() >= deadline:
                raise BrowserActionError(
                    f"Expected text in frame {index} within {wait:g} seconds, got an empty frame"
                )
            self._pause()

    def _pause(self) -> None:
        """Let the page run for one poll interval, reporting a page that went away."""
        try:
            self._page.wait_for_timeout(_POLL_MS)
        except _playwright_error() as exc:
            raise self._action_error("wait for the page", exc) from exc

    def _wait_for_frame(self, index: int, wait_s: float) -> None:
        """Wait up to `wait_s` for child frame number `index` to exist and have a web address.

        A frame is reported to a new connection with no address at first, so existing is not
        enough: reading it then would be refused for having no host.
        """
        deadline = time.monotonic() + clamp_wait(wait_s)
        while not self._frame_has_address(index) and time.monotonic() < deadline:
            self._pause()

    def _frame_has_address(self, index: int) -> bool:
        frames = self._child_frames()
        return len(frames) >= index and bool(_web_host(frames[index - 1].url))

    def _child_frames(self) -> list[Any]:
        """Playwright's child frames, then any frame it cannot see that Chrome still lists.

        A connection made after a cross-origin frame exists never hears of it from Playwright
        (#551); those frames are reached over their own DevTools socket instead.
        """
        frames = [frame for frame in self._page.frames if frame != self._page.main_frame]
        if self._existing_frames is not None:
            known = [str(frame.url) for frame in frames]
            try:
                frames.extend(self._existing_frames.frames(known))
            except Exception:  # noqa: BLE001 - a best-effort source must not hide Playwright's frames
                pass
        return frames

    def _ready_frame(self, is_allowed: Callable[[str], bool]) -> Any | None:
        """The first child frame on an allowed host whose transcript pane has loaded, else None."""
        for frame in self._child_frames():
            host = _web_host(frame.url)
            if not host or not is_allowed(host):
                continue
            with contextlib.suppress(_playwright_error(), _raw_frame_error()):
                if bool(frame.evaluate(teams.FRAME_READY_JS)):
                    return frame
        return None

    def _require_allowed_frame_host(self, frame: Any, is_allowed: Callable[[str], bool]) -> None:
        host = _web_host(frame.url)
        if not host or not is_allowed(host):
            # Never the frame's URL: a data: or blob: address can carry a payload or a token.
            detail = host or f"no web host (scheme {urlsplit(frame.url).scheme or 'none'!r})"
            raise BrowserActionError(
                f"Expected the frame to stay on an allowed host, got {detail!r}"
            )

    @contextlib.contextmanager
    def _bounded(self, seconds: float, what: str) -> Iterator[None]:
        """`_deadline` that also marks this driver as no longer trustworthy once it expires."""
        try:
            with _deadline(seconds, what):
                yield
        except _DeadlineExpired:
            self._expired = True
            raise

    def _call_deadline_s(self) -> float:
        """How long one driver call may take: Playwright's own waits end at the timeout and a
        call can run a few of them in a row (find, vet, act), so this is only for what has no
        wait, twice the timeout and some seconds on top."""
        return 2 * self._timeout_s + _DEADLINE_SLACK_S

    def _require_open(self) -> None:
        if self._closed:
            raise BrowserActionError("Expected an attached browser, got a closed driver")
        if self._expired:
            raise BrowserActionError(
                "Expected the page to answer, got a connection that stopped answering after a "
                "deadline; close it and look at the page before trying again"
            )


# The driver calls that talk to the page and have no wait of their own to bound them: a page
# whose script never returns would hold any of them for good (#623). `checked_download`,
# `edit_cell`, `scroll_frame_text`, the frame reads (`frame_aria_snapshot`, `frame_list`,
# `read_frame_text`) and the settle waits are left out, since each runs a loop or a wait whose
# length the caller sets (`--wait` up to 300 seconds).
_DEADLINE_METHODS = (
    "aria_snapshot",
    "checked_click",
    "checked_pick",
    "describe_field",
    "fill",
    "goto",
    "list_options",
    "press_field",
    "raw_press",
    "read_field",
    "read_text",
    "select",
    "title",
    "upload",
)


def _with_deadline(method: Callable[..., Any], name: str) -> Callable[..., Any]:
    @functools.wraps(method)
    def bounded(self: PlaywrightDriver, *args: Any, **kwargs: Any) -> Any:
        with self._bounded(self._call_deadline_s(), f"finish {name}"):
            return method(self, *args, **kwargs)

    return bounded


for _name in _DEADLINE_METHODS:
    setattr(PlaywrightDriver, _name, _with_deadline(getattr(PlaywrightDriver, _name), _name))


class RawBrowserDriver(Protocol):
    """The unguarded surface `GuardedDriver` wraps (`PlaywrightDriver` implements it)."""

    def aria_snapshot(self) -> str: ...

    def block_form_submission(self, *, background_writes: bool = False) -> None: ...

    def checked_click(
        self,
        name: str,
        role: str,
        vet: Callable[[ControlInfo], None],
        nth: int | None = None,
    ) -> None: ...

    def checked_download(
        self,
        name: str,
        role: str,
        vet: Callable[[ControlInfo], None],
        *,
        save_to: Path,
        timeout_s: float,
    ) -> DownloadedFile: ...

    def checked_pick(
        self,
        name: str,
        role: str,
        vet: Callable[[ControlInfo], None],
        nth: int | None = None,
        prefix: bool = False,
    ) -> None: ...

    def close(self) -> None: ...

    def describe_field(self, label: str, target: FieldTarget | None = None) -> ControlInfo: ...

    def dismiss(self) -> None: ...

    def edit_cell(
        self,
        row: str,
        column: str,
        value: str | None,
        grid: int,
        vet: Callable[[ControlInfo], None],
        vet_name: Callable[[str], None],
    ) -> CellEdit:
        """Read or edit one data-grid cell; `vet` and `vet_name` judge what is clicked."""
        ...

    def fill(self, label: str, value: str, target: FieldTarget | None = None) -> str | None: ...

    def frame_aria_snapshot(
        self,
        index: int,
        is_allowed: Callable[[str], bool],
        expect_host: str | None = None,
        *,
        wait_s: float = 0.0,
    ) -> str: ...

    def frame_list(
        self, is_allowed: Callable[[str], bool], *, wait_s: float = 0.0
    ) -> tuple[FrameInfo, ...]: ...

    def goto(self, url: str) -> None: ...

    def mark_window(
        self, task: str, hosts: Sequence[str] = (), blocked: Sequence[str] = ()
    ) -> None: ...

    def matching_roles(
        self, name: str, roles: Sequence[str], wait_s: float, nth: int | None = None
    ) -> tuple[str, ...]: ...

    def raw_press(self, label: str, key: str, target: FieldTarget | None = None) -> None: ...

    def press_field(
        self,
        label: str,
        key: str,
        target: FieldTarget | None,
        vet: Callable[[ControlInfo], None],
    ) -> None: ...

    def pick_account(
        self,
        email: str,
        hosts: Sequence[str],
        blocked: Sequence[str],
        vet_names: Callable[[Sequence[str]], bool] | None = None,
    ) -> AccountPick: ...

    def read_field(self, label: str, target: FieldTarget | None = None) -> FieldReading: ...

    def read_frame_text(
        self,
        index: int,
        is_allowed: Callable[[str], bool],
        expect_host: str | None = None,
        *,
        wait_s: float = 0.0,
    ) -> PageText: ...

    def read_text(self) -> PageText: ...

    def sign_in_pass(
        self,
        is_sign_in_host: Callable[[str], bool],
        is_task_host: Callable[[str], bool] | None = None,
    ) -> AbstractContextManager[None]: ...

    def scroll_frame_text(
        self, is_allowed: Callable[[str], bool], *, max_steps: int, wait_s: float
    ) -> FrameCapture: ...

    def list_options(
        self,
        label: str,
        target: FieldTarget | None = None,
        vet: Callable[[ControlInfo], None] | None = None,
        vet_name: Callable[[str], None] | None = None,
    ) -> OptionsListing: ...

    def select(
        self,
        label: str,
        option: str,
        target: FieldTarget | None = None,
        vet: Callable[[ControlInfo], None] | None = None,
        vet_name: Callable[[str], None] | None = None,
    ) -> str | None: ...

    def title(self) -> str: ...

    def upload(
        self, label: str, path: Path, target: FieldTarget | None = None
    ) -> UploadObservation: ...

    @property
    def url(self) -> str: ...

    def wait_ms(self, milliseconds: int) -> None: ...


def attach(
    profile: BrowserProfile,
    *,
    attempts: int = ATTACH_ATTEMPTS,
    factory: Callable[[], Any] | None = None,
    fetch_version: Callable[[str, float], dict[str, Any]] | None = None,
    open_page: bool = True,
    pid_alive: Callable[[int], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    strict: bool = False,
    timeout_s: float = DEFAULT_ACTION_TIMEOUT_S,
) -> PlaywrightDriver:
    """Attach to the browser launched for `profile`, refusing profiles blumkin did not make.

    With `strict` a recorded process that cannot be confirmed as the browser (no working `ps`)
    does not count as running: for a command that deletes something.

    A browser with no page gets a blank one unless `open_page` is off, which makes it a
    `BrowserNoPageError` instead: a diagnostic must not change the state it is looking at.
    """
    require_blumkin_profile(profile.path)
    session = read_session(profile.path, pid_alive=pid_alive, strict=strict)
    if session is None:
        raise BrowserNotRunningError(
            f"Expected a running browser for task {profile.task!r}, got none; "
            f"run `blumkin browser launch {profile.task}` and sign in first"
        )
    return open_driver(
        session.endpoint,
        attempts=attempts,
        factory=factory,
        fetch_version=fetch_version,
        open_page=open_page,
        sleep=sleep,
        timeout_s=timeout_s,
    )


def count_page_targets(
    endpoint: str,
    timeout_s: float | None = None,
    fetch: Callable[[str, float], list[dict[str, Any]]] | None = None,
) -> int | None:
    """How many open pages (tabs) the browser at a loopback `endpoint` has; None if unknown."""
    try:
        _require_loopback_endpoint(endpoint)
        wait = _DISCOVERY_TIMEOUT_S if timeout_s is None else timeout_s
        targets = (fetch or _fetch_targets)(endpoint, wait)
    except BrowserAttachError, OSError, ValueError:
        return None
    return sum(1 for target in targets if target.get("type") == "page")


def clamp_wait(seconds: float) -> float:
    """`seconds` as a wait that is finite and between 0 and `MAX_WAIT_S` (nan counts as 0)."""
    if math.isnan(seconds) or seconds <= 0.0:
        return 0.0
    return min(seconds, MAX_WAIT_S)


def checked_http_url(url: str) -> str:
    """The lowercase host of an http(s) URL that a browser reads the way `urlsplit` does.

    Raises `BrowserActionError` for anything the two could read differently: a backslash
    (browsers treat it as a slash, so `https://evil.example\\@good.example/` is evil.example),
    user info, whitespace or control characters, or a missing host or scheme.
    """
    ambiguous = any(
        char == "\\" or char.isspace() or unicodedata.category(char).startswith("C") for char in url
    )
    parts = None
    if not ambiguous:
        try:
            parts = urlsplit(url)
        except ValueError:
            parts = None  # for example an unclosed IPv6 bracket
    if (
        parts is None
        or parts.scheme not in ("http", "https")
        or not parts.netloc
        or "@" in parts.netloc
        or not parts.hostname
    ):
        raise BrowserActionError(
            "Expected an http or https URL without backslashes, spaces, control characters or "
            f"user info, got {url[:100]!r}"
        )
    return parts.hostname.lower()


def open_driver(
    endpoint: str,
    *,
    attempts: int = ATTACH_ATTEMPTS,
    existing_frames: Any = None,
    factory: Callable[[], Any] | None = None,
    fetch_version: Callable[[str, float], dict[str, Any]] | None = None,
    open_page: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    timeout_s: float = DEFAULT_ACTION_TIMEOUT_S,
) -> PlaywrightDriver:
    """Connect to a loopback DevTools `endpoint`; retries transient failures, bounded.

    Before connecting, `/json/version` is read (no redirects) and the WebSocket URL it
    advertises must be the same loopback host and port as `endpoint`; the connection is then
    made to that exact URL, so the answer that was checked is the one used.
    """
    _require_loopback_endpoint(endpoint)
    fetch = fetch_version or _fetch_version
    # Default factory only: a missing/broken playwright driver must surface as
    # BrowserDependencyError (reinstall guidance), not a generic attach failure.
    # Caller-supplied factories keep their own exception type.
    if factory is None:
        try:
            playwright = _sync_playwright().start()
        except Exception as exc:
            raise BrowserDependencyError(
                f"Expected the playwright package to start, got {exc}; "
                "run `uv sync --reinstall-package playwright` in the blumkin clone "
                "(or `uv tool install -e . --reinstall` / "
                "`uv tool upgrade blumkin --reinstall-package playwright`) "
                "to restore it, then retry the browser command"
            ) from exc
    else:
        playwright = factory().start()
    try:
        last: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                sleep(0.5 * 2 ** (attempt - 1))
            try:
                version = fetch(endpoint, timeout_s)
            except urllib.error.HTTPError as exc:
                raise BrowserProtocolError(
                    f"Expected {endpoint}/json/version to answer directly, got HTTP {exc.code}"
                ) from exc
            except (OSError, ValueError) as exc:
                last = exc
                continue
            websocket = _require_loopback_websocket(version, endpoint)
            try:
                browser = playwright.chromium.connect_over_cdp(websocket, timeout=timeout_s * 1000)
            except _playwright_error() as exc:
                last = exc
                continue
            return _driver_for(
                browser,
                playwright,
                endpoint=endpoint,
                existing_frames=existing_frames,
                open_page=open_page,
                read_existing_frames=factory is None,
                timeout_s=timeout_s,
            )
        detail = str(last).splitlines()[0] if last and str(last) else "no detail"
        raise BrowserProtocolError(
            f"Expected to attach to {endpoint}, got {detail} after {attempts} attempts"
        )
    except BaseException:
        with contextlib.suppress(_playwright_error()):
            playwright.stop()
        raise


_DISCOVERY_TIMEOUT_S = 3.0


_POLL_MS = 250
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


_SCROLL_SETTLE_MS = 250


_ARIA_VALUE_JS = (
    "(e) => (e.value !== undefined && e.value !== '') ? String(e.value) : "
    "String(e.getAttribute('aria-valuetext') || e.innerText || e.textContent || '')"
)

#: A field's value, or null when it is a password or a secret code field (checked on the same
#: element, in the same turn, so a re-render cannot swap one in between the check and the read).
_SAFE_VALUE_JS = (
    "(e, secrets) => { "
    "if (String(e.type || '').toLowerCase() === 'password') return null; "
    "const tokens = String(e.getAttribute('autocomplete') || '').toLowerCase().split(/\\s+/); "
    "if (tokens.some((t) => secrets.includes(t))) return null; "
    "if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName)) return String(e.value); "
    "return (e.value !== undefined && e.value !== '') ? String(e.value) : "
    "String(e.getAttribute('aria-valuetext') || e.innerText || e.textContent || ''); }"
)

_DROPDOWN_INFO_JS = (
    "(e) => { const role = e.getAttribute('role') || ''; "
    "const popup = e.getAttribute('aria-haspopup') || ''; "
    "return {tag: e.tagName.toLowerCase(), role: role || e.tagName.toLowerCase(), "
    "controls: e.getAttribute('aria-controls') || e.getAttribute('aria-owns') || '', "
    "dropdown: role === 'combobox' || role === 'listbox' || popup === 'listbox'}; }"
)

_MATCH_INFO_JS = (
    "(e) => { const text = (n) => (n ? (n.innerText || n.textContent || '').trim() : ''); "
    "const by = (e.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean)"
    ".map((id) => text(document.getElementById(id))).join(' '); "
    "const l = e.labels && e.labels[0] ? text(e.labels[0]) : ''; "
    "const name = by || e.getAttribute('aria-label') || l || e.getAttribute('placeholder') "
    "|| e.getAttribute('name') || ''; "
    "const t = String(e.type || '').toLowerCase(); "
    "const inputs = {checkbox: 'checkbox', radio: 'radio', range: 'slider', "
    "number: 'spinbutton', search: 'searchbox', file: 'file'}; "
    "const tags = {SELECT: (e.multiple || e.size > 1) ? 'listbox' : 'combobox', "
    "TEXTAREA: 'textbox', BUTTON: 'button'}; "
    "const role = e.getAttribute('role') || "
    "(e.tagName === 'INPUT' ? (inputs[t] || 'textbox') : tags[e.tagName]) "
    "|| e.tagName.toLowerCase(); "
    "const box = e.getBoundingClientRect(); "
    "const visible = box.width > 0 && box.height > 0 "
    "&& getComputedStyle(e).visibility !== 'hidden'; "
    "return {role: role, name: name.trim(), visible: visible}; }"
)

#: Whether a file input is hidden, zero-size or invisible (a drop zone's input often is one:
#: `display: none`, a 1px box, `opacity: 0` or a clip).
_UPLOAD_INFO_JS = (
    "(e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); "
    "let faded = false; "
    "for (let n = e; n && n.nodeType === 1; n = n.parentElement) "
    "{ if (parseFloat(getComputedStyle(n).opacity) === 0) faded = true; } "
    "const inputHidden = !(e.offsetWidth || e.offsetHeight || e.getClientRects().length) "
    "|| s.visibility === 'hidden' || faded || r.width <= 1 || r.height <= 1 "
    "|| (s.clipPath && s.clipPath !== 'none') || s.clip === 'rect(0px, 0px, 0px, 0px)'; "
    "return inputHidden; }"
)

#: How often a file's name is on the page: in the input's own row (a table row, list item,
#: fieldset or group; with none, nothing local can be said and the result stays `unknown`) and
#: on the whole page, compared ignoring case and spacing. Run before and after the file is set;
#: a name that is only there already cannot vouch for the upload.
_UPLOAD_COUNT_HEAD = (
    "(e, name) => { "
    "const norm = (t) => String(t || '').normalize('NFC').replace(/\\s+/g, ' ').toLowerCase(); "
    "const want = norm(name); "
    "const count = (n) => (want && n ? norm(n.innerText).split(want).length - 1 : 0); "
    "const connected = e.isConnected; let near = null; "
    "if (connected) { near = e.closest('tr, [role=row], li, fieldset, [role=group]'); } "
)
_UPLOAD_COUNT_JS = _UPLOAD_COUNT_HEAD + (
    "return {near: count(near), body: count(document.body)}; }"
)
#: The same counts after the set, plus the names the input holds (none once it is detached).
_UPLOAD_STATE_JS = _UPLOAD_COUNT_HEAD + (
    "return {connected: connected, "
    "files: connected && e.files ? Array.from(e.files).map((f) => f.name) : [], "
    "near: count(near), body: count(document.body)}; }"
)

#: Most options and scrolls `list_options` takes from one list.
_MAX_OPTIONS = 500
_MAX_OPTION_SCROLLS = 60

_NATIVE_OPTIONS_JS = "(e) => Array.from(e.options || []).map((o) => o.label || o.text || '')"

#: Each option's accessible name as `select` matches it: its `aria-label`, the text of the
#: elements it is labelled by, or its own text.
_OPTION_NAMES_JS = (
    "(els) => els.map((e) => { const by = (e.getAttribute('aria-labelledby') || '').split(/\\s+/)"
    ".map((id) => { const n = id ? document.getElementById(id) : null; "
    "return n ? (n.innerText || n.textContent || '') : ''; }).join(' '); "
    "return (e.getAttribute('aria-label') || by || e.innerText || e.textContent || '')"
    ".replace(/\\s+/g, ' ').trim(); })"
)

#: The elements a list scrolls in: itself, what is inside it, and the containers around it up
#: to (not including) the page, since a popup's scrollbar is often on a wrapper.
_SCROLLERS_JS = (
    "const scrollers = (p) => { const out = [p, ...p.querySelectorAll('*')]; "
    "for (let a = p.parentElement; a && a !== document.body && a !== document.documentElement; "
    "a = a.parentElement) out.push(a); "
    "return out.filter((e) => ['auto', 'scroll'].includes(getComputedStyle(e).overflowY)); }; "
)

#: Scroll the list one page down; true when something moved.
_SCROLL_LIST_JS = (
    "(p) => { " + _SCROLLERS_JS + "for (const e of scrollers(p)) { "
    "const before = e.scrollTop; e.scrollTop = before + e.clientHeight; "
    "if (e.scrollTop !== before) return true; } return false; }"
)

#: Scroll the list back to its first rows.
_SCROLL_TOP_JS = (
    "(p) => { "
    + _SCROLLERS_JS
    + "for (const e of scrollers(p)) { e.scrollTop = 0; } return true; }"
)

_SELECTED_TEXT_JS = (
    "(e) => e.selectedOptions && e.selectedOptions[0] ? e.selectedOptions[0].label : ''"
)

#: Finds the one account tile for an address on an identity provider's picker; run on the page.
#: Returns [status, element, count] (see `AccountPick`). The page must be https on a listed
#: host and not on a blocked one, and must show nothing to type into. A tile is the actionable
#: element (a button or link, or the nearest one around a marked child) whose own names (text,
#: labels, titles, referenced labels) hold the address as a whole token and no other address,
#: carry no word such as remove, sign out, send, code, confirm or allow, and link nowhere off
#: the listed hosts. Anything with `@` in a name must be exactly the address.
_PICK_ACCOUNT_JS = r"""(args) => {
  const lower = (t) => String(t || '').replace(/[A-Z]/g, (c) => c.toLowerCase());
  const want = lower(args.email);
  const strip = (h) => h.toLowerCase().replace(/\.+$/, '');
  const hostMatch = (host, p) => p.startsWith('*.')
    ? (host.endsWith(p.slice(1)) && host.length > p.length - 1) : host === p;
  const here = strip(location.hostname);
  if (location.protocol !== 'https:' || !args.hosts.some((p) => hostMatch(here, p))
      || args.blocked.some((p) => hostMatch(here, p))) return ['wrong_origin', null, 0];
  const shown = (e) => !!(e.offsetWidth || e.offsetHeight);
  const skip = ['hidden', 'submit', 'button', 'checkbox', 'radio', 'image', 'reset'];
  const fields = Array.from(document.querySelectorAll(
    'input, textarea, select, [contenteditable]:not([contenteditable=false]), '
    + '[role=textbox], [role=combobox], [role=searchbox]')).filter((e) =>
    !(e.tagName === 'INPUT' && skip.includes(e.type)) && shown(e));
  if (fields.length) return ['unsupported', null, fields.length];
  const denied = ['remove', 'forget', 'sign[ -]?out', 'log[ -]?out', 'delete', 'allow',
    'authori[sz]e', 'grant', 'consent', 'accept', 'approve', 'revoke', 'send', 'resend',
    'code', 'confirm', 'verify', 'sms', 'text', 'call', 'password', 'passkey', 'create',
    'register', 'sign[ -]?up', 'add', 'another', 'different', 'other', 'continue', 'next'];
  const deny = new RegExp('\\b(' + denied.join('|') + ')\\b');
  const actionable = 'a[href], button, [role=button], [role=link]';
  const offHost = (el) => [el.closest('a[href]')].filter(Boolean).concat(
    Array.from(el.querySelectorAll('a[href]'))).some((a) => {
    try {
      const u = new URL(a.href, location.href);
      return u.protocol !== 'https:' || !args.hosts.some((p) => hostMatch(strip(u.hostname), p));
    } catch (e) { return true; }
  });
  const names = (el) => {
    const root = el.getRootNode();
    const by = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean).map(
      (id) => { const n = root.getElementById ? root.getElementById(id) : null;
        return n ? n.innerText || n.textContent : ''; });
    const kids = Array.from(el.querySelectorAll('[aria-label], [title], [alt], [data-test-id]'))
      .map((k) => [k.getAttribute('aria-label'), k.getAttribute('title'),
        k.getAttribute('alt'), k.getAttribute('data-test-id')].join(' '));
    return [el.getAttribute('data-test-id'), el.getAttribute('aria-label'),
      el.getAttribute('title'), el.innerText, ...by, ...kids].map(lower);
  };
  const tokens = (text) => text.split(/[\s"<>()\[\],;:]+/).map(
    (t) => t.replace(/[.!?]+$/, '')).filter(Boolean);
  const candidates = new Set();
  for (const el of document.querySelectorAll(actionable + ', [data-test-id]')) {
    candidates.add(el.matches(actionable) ? el : (el.closest(actionable) || el));
  }
  const usable = (el) => {
    const css = getComputedStyle(el);
    return shown(el) && css.visibility === 'visible' && css.pointerEvents !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true'
      && !el.closest('[inert], [aria-hidden=true], fieldset:disabled');
  };
  const hits = Array.from(candidates).filter((el) => {
    if (!usable(el)) return false;
    const parts = names(el);
    const found = parts.flatMap(tokens).filter((t) => t.includes('@'));
    if (!found.includes(want) || found.some((t) => t !== want)) return false;
    return !deny.test(parts.join(' \n ')) && !offHost(el);
  });
  const tiles = hits.filter((el) => !hits.some((o) => o !== el && el.contains(o)));
  if (tiles.length === 0) return ['none', null, 0];
  if (tiles.length > 1) return ['ambiguous', null, tiles.length];
  const seen = names(tiles[0]);
  if (args.click) {
    if (tiles[0] !== args.click || JSON.stringify(seen) !== JSON.stringify(args.names)) {
      return ['changed', null, 1, []];
    }
    tiles[0].click();
    return ['ok', null, 1, []];
  }
  return ['ok', tiles[0], 1, seen];
}"""

#: What would take the click on a cell: `ok` when the point at its centre is the cell itself or
#: plain content inside it, else what is there (a control inside the cell, something covering it).
_CELL_CLICK_TARGET_JS = """
(el) => {
  const box = el.getBoundingClientRect();
  const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
  if (!hit) return 'nothing at the click point';
  if (!el.contains(hit)) return 'another element on top of the cell';
  const control = hit.closest(
    'button, a, [role=button], [role=link], [role=menuitem], [role=checkbox], [role=radio],'
    + ' input[type=button], input[type=submit], input[type=image], input[type=reset],'
    + ' input[type=checkbox], input[type=radio]');
  return control && el.contains(control) ? 'a control inside the cell' : 'ok';
}
"""

#: How long a typed cell value waits for a dropdown to appear before it is committed as plain text.
_CELL_DROP_WAIT_MS = 1500

#: Whether a visible text editor is open inside the `grid`-th data grid.
_CELL_EDITING_JS = """
(n) => {
  const g = document.querySelectorAll('oj-data-grid')[n - 1];
  if (!g) return false;
  return [...g.querySelectorAll('input, textarea, [contenteditable=true]')].some((e) =>
    e.offsetParent && !/^(button|submit|checkbox|radio|image|reset|hidden)$/.test(e.type || ''));
}
"""

#: Where focus is, for the `grid`-th data grid: `cell`, `editor`, `other` (a control inside the
#: grid) or `outside`.
_CELL_FOCUS_JS = """
(n) => {
  const g = document.querySelectorAll('oj-data-grid')[n - 1];
  const a = document.activeElement;
  if (!g || !a || !g.contains(a)) return 'outside';
  const editable = 'input:not([type=button]):not([type=submit]):not([type=checkbox])'
    + ':not([type=radio]):not([type=image]):not([type=reset]):not([type=hidden]), textarea,'
    + ' [contenteditable=true]';
  if (a.matches(editable)) return 'editor';
  return a.classList.contains('oj-datagrid-cell') ? 'cell' : 'other';
}
"""

#: Finds one cell of an Oracle JET data grid (`oj-data-grid`), whose cells are plain divs with no
#: roles or labels. Columns count the frozen ones first (Project, Task, ...), then the scrolling
#: ones (the days); a column is a 1-based number or its header text, a row a 1-based number or
#: its row header text. The cell is the one drawn where that row and that column meet.
_CELL_JS = r"""
(args) => {
  const grids = [...document.querySelectorAll('oj-data-grid')];
  const grid = grids[args.grid - 1];
  if (!grid) return {error: `Expected data grid ${args.grid}, got ${grids.length} on the page`};
  const norm = (s) => (s || '').normalize('NFC').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
  // A header is nested: a frozen column's group holds the period, then Scheduled Hours, then its
  // own name (Project, Task, ...), so its label is the deepest text; a day's group starts with
  // the day, so its label is the first.
  const texts = (e) => [...e.querySelectorAll('.oj-datagrid-header-cell')]
    .map((c) => (c.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
  const first = (e) => texts(e)[0] || (e.innerText || '').replace(/\s+/g, ' ').trim();
  const last = (e) => texts(e).pop() || '';
  const scroller = (sel) => grid.querySelector(sel + ' > .oj-datagrid-scroller');
  const kids = (sc) => (sc ? [...sc.children] : []);
  const frozenCols = kids(scroller('.oj-datagrid-column-header-frozen')).map(last);
  const mainCols = kids(scroller('.oj-datagrid-column-header')).map(first);
  const rowHeads = kids(scroller('.oj-datagrid-row-header')).map(first);
  const columns = [...frozenCols, ...mainCols];
  const resolve = (want, names, what) => {
    let text = String(want).trim();
    // `text:` names a header by its text even when that is only digits (a year, a day number),
    // which would otherwise be read as a position.
    const byText = text.startsWith('text:');
    if (byText) text = text.slice(5).trim();
    if (byText && !text) {
      return {error: `Expected header text after text: for the ${what}, got none`};
    }
    if (!byText && /^[0-9]+$/.test(text)) {
      const n = parseInt(text, 10);
      if (n < 1 || n > names.length) {
        return {error: `Expected ${what} 1 to ${names.length}, got ${n}`};
      }
      return {index: n - 1};
    }
    const hits = names.map((n, i) => (norm(n) === norm(text) ? i : -1)).filter((i) => i >= 0);
    if (hits.length === 1) return {index: hits[0]};
    return {error: `Expected one ${what} named ${text}, got ${hits.length}`};
  };
  const c = resolve(args.column, columns, 'column');
  if (c.error) return c;
  const r = resolve(args.row, rowHeads, 'row');
  if (r.error) return r;
  const frozen = c.index < frozenCols.length;
  const body = frozen
    ? grid.querySelector('.oj-datagrid-databody-frozen-column')
    : grid.querySelector('.oj-datagrid-databody');
  const cells = [...(body ? body.querySelectorAll('.oj-datagrid-cell') : [])];
  // JET moves a cell it has edited to the end of its container, so the DOM order stops being
  // row-major after an edit; the cell is found by where it is drawn instead, in the row and
  // column of the headers.
  const cellOf = (e) => (e.querySelector('.oj-datagrid-header-cell') || e).getBoundingClientRect();
  const top = cellOf(kids(scroller('.oj-datagrid-row-header'))[r.index]).top;
  const headerSel = frozen ? '.oj-datagrid-column-header-frozen' : '.oj-datagrid-column-header';
  const colHeads = kids(scroller(headerSel));
  const left = cellOf(colHeads[frozen ? c.index : c.index - frozenCols.length]).left;
  const near = (a, b) => Math.abs(a - b) <= 6;
  const hits = cells.filter((e) => {
    const box = e.getBoundingClientRect();
    return near(box.top, top) && near(box.left, left);
  });
  if (hits.length !== 1) {
    return {
      error: `Expected one cell at row ${r.index + 1}, column ${c.index + 1}, got ${hits.length}`,
    };
  }
  return {
    element: hits[0],
    meta: {column: c.index + 1, columnLabel: columns[c.index], columns: columns.length,
           row: r.index + 1, rowLabel: rowHeads[r.index], rows: rowHeads.length},
  };
}
"""

#: How often and how long a commit is waited for, and how often the options of a typed cell value
#: are polled while they load.
_CELL_COMMIT_POLLS = 4
#: Most characters of a matched element's accessibility snapshot a prefix pick checks.
_PICK_SNAPSHOT_CHARS = 2000
#: How long `upload` waits for the frame post a chosen file may start (polls of this many
#: milliseconds), and how long it lets that post finish.
_UPLOAD_POST_POLL_MS = 250
_UPLOAD_POST_POLLS = 16
_UPLOAD_POST_GRACE_MS = 2000
_CELL_OPTION_POLL_MS = 500
_CELL_OPTION_POLLS = 16

#: What a cell shows: the value of its editor while it is being edited, else its text without
#: the screen-reader-only status JET keeps inside an edited cell ("1 matches found"), which is
#: not part of the value.
_CELL_TEXT_JS = """
(el) => {
  const editor = el.querySelector('input:not([type=hidden]):not([type=password])');
  if (editor && editor.value) return editor.value.replace(/\\s+/g, ' ').trim();
  const copy = el.cloneNode(true);
  copy.querySelectorAll('.oj-helper-hidden-accessible, [aria-live], [role=status]')
    .forEach((n) => n.remove());
  return (copy.textContent || '').replace(/\\s+/g, ' ').trim();
}
"""

#: Whether a row (or cell) holds, sits inside or is made of a control, which a click in its
#: middle could hit, bubble to or be forwarded by: any control or focusable thing inside or
#: around it, the element itself when it is one (a `label`, a `contenteditable` area), and
#: content in a shadow root, whether it hosts one, has one inside or sits in one (none of which
#: can be searched). The row's own click handler is not a control: a result list is built so.
_PICK_BLOCKERS_JS = r"""(e) => {
  // `role` is a token list (`button checkbox`) and `contenteditable` may be bare or
  // `plaintext-only`, so roles match by token and an editor is anything but `false`.
  const roles = ['button', 'link', 'menuitem', 'menuitemcheckbox', 'menuitemradio',
    'checkbox', 'radio', 'switch', 'tab', 'combobox', 'textbox', 'slider', 'spinbutton',
    'option', 'treeitem'].map((r) => '[role~=' + r + ']').join(', ');
  const own = 'button, a, area, input:not([type=hidden]), select, textarea, summary, label, '
    + 'iframe, object, embed, [contenteditable]:not([contenteditable=false])';
  const controls = own + ', [onclick], ' + roles;
  if (e.matches(own)) return true;
  if (e.querySelector(controls)) return true;
  if (e.querySelector('[tabindex]:not([tabindex="-1"])')) return true;
  if (e.getRootNode() instanceof ShadowRoot) return true;
  if ([e, ...e.querySelectorAll('*')].some((n) => n.shadowRoot)) return true;
  return !!(e.parentElement && e.parentElement.closest(controls));
}"""

_HEADINGS_JS = (
    "() => ({url: location.href, headings: "
    "Array.from(document.querySelectorAll('h1, h2, h3, [role=heading]'))"
    ".filter((h) => h.getClientRects().length > 0 && "
    "getComputedStyle(h).visibility !== 'hidden')"
    ".map((h) => String(h.innerText || h.textContent || '').replace(/\\s+/g, ' ').trim()"
    ".slice(0, 1000)).filter(Boolean).slice(0, 200)})"
)

_QUIET_DOM_JS = (
    "({quiet, total}) => new Promise((resolve) => { "
    "let timer = null; "
    "const finish = (value) => { observer.disconnect(); clearTimeout(timer); "
    "clearTimeout(cap); resolve(value); }; "
    "const observer = new MutationObserver(() => { clearTimeout(timer); "
    "timer = setTimeout(() => finish(true), quiet); }); "
    "observer.observe(document, {subtree: true, childList: true, attributes: true, "
    "characterData: true}); "
    "timer = setTimeout(() => finish(true), quiet); "
    "const cap = setTimeout(() => finish(false), total); })"
)

_DESCRIBE_CONTROL_JS = (
    "(e, landmarks) => { const sel = 'dialog, [role=dialog], [role=alertdialog]'; "
    "let d = e.closest(sel); let viaLandmark = false; "
    "if (!d && landmarks && landmarks.length && e.tagName !== 'LABEL' && !e.closest('label') "
    "&& !(e.parentElement && e.parentElement.closest('button, a, summary, [onclick], "
    "[role=button], [role=link], [role=menuitem], input'))) { "
    "const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase(); "
    "const pick = 'button, a, [role=button], [role=link], [role=menuitem], "
    "input[type=button], input[type=submit]'; "
    "let a = e.parentElement; "
    "while (a && a !== document.body && a !== document.documentElement) { "
    "const have = new Set(Array.from(a.querySelectorAll(pick)).flatMap((b) => "
    "[norm(b.innerText), norm(b.getAttribute('aria-label')), norm(b.value), norm(b.title)])); "
    "if (landmarks.every((l) => have.has(norm(l)))) { d = a; viaLandmark = true; break; } "
    "a = a.parentElement; } } "
    "const text = (n) => (n ? String(n.innerText || n.textContent || '').trim() : ''); "
    "const names = []; let complete = true; "
    "if (d) { const all = Array.from(d.querySelectorAll("
    '"button, a, summary, [onclick], input[type=submit], input[type=button], '
    "input[type=image], input[type=reset], "
    "[role=button], [role=link], [role=menuitem], [role=menuitemradio], "
    "[role=menuitemcheckbox], [role=tab], [role=option], [role=switch], "
    '[role=checkbox], [role=radio]")); '
    "if (viaLandmark) { if (d.matches('button, a, summary, [onclick], [role=button], [role=link], "
    "[role=menuitem], input') || (d.parentElement && d.parentElement.closest("
    "'button, a, summary, [onclick], [role=button], [role=link], [role=menuitem], label'))) "
    "complete = false; } "
    "if (all.length > 200) complete = false; "
    "if (Array.from(d.querySelectorAll('*')).some((n) => n.shadowRoot)) complete = false; "
    "for (const b of all.slice(0, 200)) { "
    "const root = b.getRootNode(); "
    "const find = (id) => (root.getElementById ? root.getElementById(id) : null); "
    "const by = (b.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean)"
    ".map((id) => text(find(id))).join(' '); "
    "const kids = Array.from(b.querySelectorAll('[aria-label], [alt], [title], svg title'))"
    ".map((k) => k.getAttribute('aria-label') || k.getAttribute('alt') || "
    "k.getAttribute('title') || k.textContent); "
    "for (const raw of [by, b.getAttribute('aria-label'), b.innerText, b.value, b.title, "
    "b.getAttribute('alt'), b.id, b.getAttribute('name'), ...kids]) { "
    "const t = String(raw || '').trim(); if (!t) continue; "
    "if (t.length > 200) complete = false; names.push(t.slice(0, 200)); } } } "
    "const f = e.form || e.closest('form'); "
    "const own = []; let ownComplete = true; "
    "{ const eroot = e.getRootNode(); "
    "const eby = (e.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean)"
    ".map((id) => text(eroot.getElementById ? eroot.getElementById(id) : null)).join(' '); "
    "const ekids = Array.from(e.querySelectorAll('[aria-label], [alt], [title], svg title'))"
    ".map((k) => k.getAttribute('aria-label') || k.getAttribute('alt') || "
    "k.getAttribute('title') || k.textContent); "
    "for (const raw of [eby, e.getAttribute('aria-label'), e.innerText, e.value, e.title, "
    "e.getAttribute('alt'), e.id, e.getAttribute('name'), ...ekids]) "
    "{ const t = String(raw || '').trim(); if (!t) continue; "
    "if (t.length > 200) ownComplete = false; own.push(t.slice(0, 200)); } "
    "if (own.length > 50) ownComplete = false; } "
    "return {ownNames: own.slice(0, 50), ownComplete: ownComplete, tag: e.tagName.toLowerCase(), "
    "type: String(e.type || '').toLowerCase(), "
    "form: !!e.form, formMethod: f ? String(f.method || 'get').toLowerCase() : '', "
    "formaction: e.hasAttribute('formaction') || e.hasAttribute('formmethod'), "
    "href: String(e.getAttribute('href') || ''), "
    "onclick: String(e.getAttribute('onclick') || ''), "
    "target: String(e.getAttribute('target') || ''), "
    "dialog: !!d, formInDialog: !!d && (!f || (d.contains(f) && !(viaLandmark && d === f))), "
    "dialogIsForm: !!d && viaLandmark && d === f, "
    "containerSize: d ? d.querySelectorAll('*').length : 0, "
    "dialogControls: names, dialogComplete: complete, dialogLandmark: viaLandmark}; }"
)


class _DeadlineExpired(BrowserActionError):
    """A `_deadline` ran out: the page did not answer in time (as opposed to refusing)."""


@contextlib.contextmanager
def _deadline(seconds: float, what: str) -> Iterator[None]:
    """Fail with a `BrowserActionError` when the block runs longer than `seconds`.

    Page evaluations through Playwright have no timeout of their own, so a page that hangs (or
    a redirect that never settles) would block a command for good. This uses a timer signal,
    which only works in the main thread of a POSIX process; elsewhere the block runs unbounded,
    as it always did. After an expiry the caller should close the connection rather than reuse
    it: whether the page did what was asked is not known.
    """
    if not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        yield
        return

    nearer, _ = signal.getitimer(signal.ITIMER_REAL)
    if 0 < nearer < seconds:  # an outer deadline that is nearer than this one still applies
        yield
        return

    live = [True]
    fired = [False]
    started = time.monotonic()

    def expired(signum: int, frame: Any) -> None:
        if not live[0]:  # the block already ended: a late alarm must not raise outside it
            return
        fired[0] = True  # a broad `except` inside the block may swallow what is raised below
        raise _DeadlineExpired(message())

    def message() -> str:
        return (
            f"Expected to {what} within {seconds:g} seconds, got no answer from the page; "
            "the action may or may not have happened, so look at the page before repeating it"
        )

    previous = signal.signal(signal.SIGALRM, expired)
    remaining, interval = signal.getitimer(signal.ITIMER_REAL)
    signal.setitimer(signal.ITIMER_REAL, max(seconds, 0.001))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)  # first: a late alarm cannot skip the restore
        live[0] = False
        signal.signal(signal.SIGALRM, previous if previous is not None else signal.SIG_DFL)
        if remaining > 0:  # an outer timer keeps what is left of its time
            left = max(remaining - (time.monotonic() - started), 0.001)
            signal.setitimer(signal.ITIMER_REAL, left, interval)
    if fired[0]:  # the alarm went off but something inside the block swallowed the error
        raise _DeadlineExpired(message())


class _NeverRaised(Exception):
    """Stands in for Playwright's error type when it cannot be imported; nothing raises it."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: `/json/version` must answer from the endpoint itself."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _log_swallowed(request: Any) -> None:
    """Append what the safety net swallowed to the file `BLUMKIN_BROWSER_NET_LOG` names.

    A diagnostic for a click that seems to do nothing: the method, the host, the last part of
    the path (no `;parameters`, no query string, no body, no header), the resource type and
    whether it was a navigation and from a child frame. Off unless set; a new file is created
    private (mode 0600), and a failure to write is ignored.
    """
    target = os.environ.get("BLUMKIN_BROWSER_NET_LOG")
    if not target:
        return
    try:
        parts = urlsplit(str(request.url))
        path = parts.path.split(";", 1)[0][-120:]
        navigation = request.is_navigation_request()
        child = request.frame.parent_frame is not None
        line = (
            f"{request.method} {parts.hostname}{path} {request.resource_type} "
            f"nav={navigation} frame={child}\n"
        )
        descriptor = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError, ValueError, _playwright_error():
        return


def _abort_every_write(route: Any) -> None:
    """Strict mode: swallow any request that writes, navigation or background; pass the rest.

    Reads (GET, HEAD) and the OPTIONS preflight carry no form data and go through; the write
    that follows a preflight is the one answered with an empty `204`, so the server never
    receives it. The page may think it succeeded, which is why this is for simple forms only.
    """
    if route.request.method in _READ_METHODS:
        route.continue_()
    else:
        _log_swallowed(route.request)
        route.fulfill(status=204)


def _navigation_net(
    page: Any, uploading: Callable[[], bool], on_pass: Callable[[], None] | None = None
) -> Callable[[Any], None]:
    """The safety net for form posts, which lets a file upload's frame post through.

    While an `upload` runs, the first multipart POST from a child frame to the page's own host
    is let through: Oracle ADF sends the chosen file that way, and answering it with an empty
    `204` leaves the page waiting for an upload that never completes. Every other write is
    swallowed as before, whatever the target, and so is a second post, a post from the main
    page, a post that is not multipart, or one made while nothing uploads.
    """

    def handler(route: Any) -> None:
        request = route.request
        if uploading() and _is_frame_post_to_page_host(request, page):
            if on_pass is not None:
                on_pass()
            route.continue_()
            return
        _abort_non_get_navigation(route)

    return handler


def _is_frame_post_to_page_host(request: Any, page: Any) -> bool:
    if request.method != "POST" or not request.is_navigation_request():
        return False
    kind = str((request.headers or {}).get("content-type", "")).casefold()
    if not kind.startswith("multipart/form-data"):
        return False
    if request.frame.parent_frame is None:
        return False
    host = urlsplit(str(request.url)).hostname
    return bool(host) and host == urlsplit(str(page.url)).hostname


def _abort_non_get_navigation(route: Any) -> None:
    """Swallow a page navigation that is not a GET (a form POST); let everything else through.

    The request is answered with an empty `204 No Content`, which tells the browser to stay
    on the current page. Aborting it instead would replace the page with an error page and
    lose whatever the user had filled in.
    """
    request = route.request
    if request.is_navigation_request() and request.method not in ("GET", "HEAD"):
        _log_swallowed(request)
        route.fulfill(status=204)
    else:
        route.continue_()


def _sign_in_handler(
    is_sign_in_host: Callable[[str], bool],
    is_task_host: Callable[[str], bool] | None = None,
) -> Callable[[Any], None]:
    """A route handler that continues sign-in traffic and hands everything else on."""

    def host_of(address: str) -> str:
        parts = urlsplit(address)
        return (parts.hostname or "") if parts.scheme in ("http", "https") else ""

    def handler(route: Any) -> None:
        request = route.request
        try:
            origin = host_of(str(request.frame.url))
        except Exception:  # a detached frame has no address; treat it as not a sign-in page
            origin = ""
        target = host_of(str(request.url))
        if is_sign_in_host(target):
            route.continue_()
            return
        top_level = False
        if is_task_host is not None:
            try:
                top_level = (
                    bool(request.is_navigation_request()) and request.frame.parent_frame is None
                )
            except Exception:  # a detached frame: treat it as top level, the safer reading
                top_level = True
        if (
            origin
            and is_sign_in_host(origin)
            and not (top_level and is_task_host is not None and not is_task_host(target))
        ):
            route.continue_()
        else:
            route.fallback()

    return handler


def _count(value: Any) -> int:
    """A count read from the page, 0 when it is not a plain whole number."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _driver_for(
    browser: Any,
    playwright: Any,
    *,
    endpoint: str,
    existing_frames: Any,
    open_page: bool,
    read_existing_frames: bool,
    timeout_s: float,
) -> PlaywrightDriver:
    try:
        contexts = list(browser.contexts)
        if not contexts:
            raise BrowserProtocolError(f"Expected a browser context at {endpoint}, got none")
        page = _first_page(contexts[0], endpoint, timeout_s, open_page=open_page)
        page.set_default_timeout(timeout_s * 1000)
    except BaseException:
        with contextlib.suppress(_playwright_error()):
            browser.close()
        raise
    if existing_frames is None and read_existing_frames:
        existing_frames = _existing_frames_for(endpoint, page, timeout_s)
    return PlaywrightDriver(
        browser=browser,
        page=page,
        playwright=playwright,
        timeout_s=timeout_s,
        existing_frames=existing_frames,
    )


def _first_page(context: Any, endpoint: str, timeout_s: float, *, open_page: bool = True) -> Any:
    """The first open page, opening a blank one when the browser has none.

    With `open_page` off a browser without a page is reported (`BrowserNoPageError`), not
    changed.

    A browser started with no page (or whose last tab was closed) cannot always make one
    through the attached context ("Browser context management is not supported"); DevTools can,
    over its own loopback endpoint, so that is tried next, waiting through Playwright for the
    new page to be reported (a plain sleep would never let the event arrive) before giving up
    with a clear error.
    """
    pages = list(context.pages)
    if pages:
        return pages[0]
    if not open_page:
        raise BrowserNoPageError(
            f"Expected an open page in the browser at {endpoint}, got none (not opening one)"
        )
    try:
        return context.new_page()
    except _playwright_error():
        pass
    probe_ms = min(timeout_s, _DISCOVERY_TIMEOUT_S) * 1000
    try:
        # The wait is set up before the tab is requested, so the event cannot be missed.
        with context.expect_event("page", timeout=probe_ms) as waiting:
            with contextlib.suppress(OSError, ValueError):
                _open_blank_target(endpoint, probe_ms / 1000)
        return waiting.value
    except _playwright_error():
        pages = list(context.pages)
        if pages:
            return pages[0]
    raise BrowserNoPageError(
        f"Expected an open page in the browser at {endpoint}, got none and could not open one"
    )


def _existing_frames_for(endpoint: str, page: Any, timeout_s: float) -> Any:
    """The source of frames Playwright cannot see on `page`, read over their own sockets."""
    from blumkin.browser.cdpws import CdpSocket, ExistingFrames, RawFrameError

    sessions: list[Any] = []

    def frame_ids(tree: Mapping[str, Any]) -> set[str]:
        ids = {str(tree["frame"]["id"])}
        for child in tree.get("childFrames", []):
            ids |= frame_ids(child)
        return ids

    def page_frame_ids() -> set[str]:
        """The ids of the frames Playwright's own connection can name on the page."""
        try:
            if not sessions:
                sessions.append(page.context.new_cdp_session(page))
            return frame_ids(sessions[0].send("Page.getFrameTree")["frameTree"])
        except (_playwright_error(), KeyError, TypeError) as exc:
            raise RawFrameError(f"Expected the page's frame tree, got {exc}") from exc

    def detach() -> None:
        for session in sessions:
            with contextlib.suppress(_playwright_error()):
                session.detach()

    probe_s = min(timeout_s, _DISCOVERY_TIMEOUT_S)
    return ExistingFrames(
        endpoint_port=urlsplit(endpoint).port or 0,
        evaluate_timeout_s=timeout_s,
        fetch_targets=lambda: _fetch_targets(endpoint, probe_s),
        on_close=detach,
        open_socket=lambda ws_url: CdpSocket.connect(ws_url, timeout_s=probe_s),
        page_frame_ids=page_frame_ids,
    )


def _fetch_targets(endpoint: str, timeout_s: float) -> list[dict[str, Any]]:
    """Read `/json/list` from a loopback DevTools endpoint: every target Chrome knows."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects)
    with opener.open(f"{endpoint}/json/list", timeout=timeout_s) as response:
        data = json.load(response)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON list from /json/list, got {type(data).__name__}")
    return [item for item in data if isinstance(item, dict)]


def _open_blank_target(endpoint: str, timeout_s: float) -> None:
    """Ask a loopback DevTools endpoint to open a blank tab (`PUT /json/new`)."""
    _require_loopback_endpoint(endpoint)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects)
    request = urllib.request.Request(f"{endpoint}/json/new?about:blank", method="PUT")
    with opener.open(request, timeout=timeout_s):
        pass


def _fetch_version(endpoint: str, timeout_s: float) -> dict[str, Any]:
    """Read `/json/version` from a loopback DevTools endpoint (validated by the caller)."""
    # An empty ProxyHandler: the DevTools endpoint is on this machine, so never send the
    # request through a proxy named in the environment.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects)
    with opener.open(f"{endpoint}/json/version", timeout=timeout_s) as response:
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object from /json/version, got {type(data).__name__}")
    return data


def field_display(read_back: str) -> str:
    """A field's value as one cleaned, shortened line, for showing the caller."""
    return _clean(read_back, 200)


def field_matches(read_back: str, wanted: str) -> bool:
    """Whether a field holds what was set: the whole values, with whitespace collapsed."""
    return " ".join(read_back.split()) == " ".join(wanted.split())


def _is_timeout(exc: BaseException) -> bool:
    """Whether a Playwright error is a timeout (a field that never appeared)."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
    except ImportError:
        return False
    return isinstance(exc, PlaywrightTimeout)


def _clean(text: str, limit: int) -> str:
    """Page-supplied text as one printable line of at most `limit` characters."""
    printable = "".join(char if char.isprintable() else " " for char in text)
    return " ".join(printable.split())[:limit]


def _option_key(text: str) -> str:
    """A dropdown option or typed value in the form they are compared in."""
    return re.sub(r"[\W_]+", "", text.casefold())


def _printable(text: str) -> str:
    """`text` without its control, bidirectional and other unprintable characters."""
    return "".join(ch for ch in text if ch.isprintable())


def _playwright_error() -> type[Exception]:
    """Playwright's error type, imported on first use so `blumkin` starts without Playwright.

    Any failure resolving the class (missing install, blocked import, broken
    package) returns :class:`_NeverRaised` so callers can still surface the
    original error and run cleanup without the lookup replacing it.
    """
    try:
        from playwright.sync_api import Error
    except Exception:
        return _NeverRaised
    return Error


def _raw_frame_error() -> type[Exception]:
    """The error a frame read over its own socket raises (imported on use: no import cycle)."""
    from blumkin.browser.cdpws import RawFrameError

    return RawFrameError


def _require_loopback_endpoint(endpoint: str) -> None:
    problem = (
        f"Expected a loopback http DevTools endpoint (http://127.0.0.1:PORT), got {endpoint!r}"
    )
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError as exc:
        raise BrowserAttachError(problem) from exc
    if (
        parts.scheme != "http"
        or parts.hostname not in ("127.0.0.1", "::1")
        or parts.username is not None
        or parts.password is not None
        or port is None
        or not 1 <= port <= 65535
    ):
        raise BrowserAttachError(problem)


def _require_loopback_websocket(version: dict[str, Any], endpoint: str) -> str:
    """Return the advertised WebSocket URL once it is proven to be on the endpoint's loopback
    host and port. The caller connects to this exact URL, so the answer that was checked is
    the one that is used (Playwright would otherwise look `/json/version` up a second time)."""
    advertised = str(version.get("webSocketDebuggerUrl") or "")
    wanted = urlsplit(endpoint)
    problem = (
        f"Expected the DevTools WebSocket on {wanted.hostname}:{wanted.port}, got {advertised!r}"
    )
    try:
        parts = urlsplit(advertised)
        port = parts.port
    except ValueError as exc:
        raise BrowserAttachError(problem) from exc
    if parts.scheme != "ws" or parts.hostname != wanted.hostname or port != wanted.port:
        raise BrowserAttachError(problem)
    return advertised


def _sync_playwright() -> Any:
    """Start-up handle for Playwright's sync API, imported only when a browser command runs."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        # ImportError for a missing package; SyntaxError / other Exception for a
        # damaged install - both need reinstall guidance, not "browser not running".
        raise BrowserDependencyError(
            f"Expected the playwright package to be importable, got {exc}; "
            "run `uv sync --reinstall-package playwright` in the blumkin clone "
            "(or `uv tool install -e . --reinstall` / "
            "`uv tool upgrade blumkin --reinstall-package playwright`) "
            "to restore it, then retry the browser command"
        ) from exc
    return sync_playwright()


def _web_host(url: str) -> str:
    """The host of an http(s) address, else "" (about:, data:, blob:, ftp: and the like)."""
    parts = urlsplit(url)
    return (parts.hostname or "") if parts.scheme in ("http", "https") else ""


def _all_addressed(addresses: Sequence[str]) -> bool:
    """True when there is at least one frame and every frame has a web address."""
    return bool(addresses) and all(_web_host(address) for address in addresses)
