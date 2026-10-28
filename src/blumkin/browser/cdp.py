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
import json
import math
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from blumkin.browser import teams
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


class BrowserAttachError(RuntimeError):
    """The task browser could not be attached to."""


class BrowserDependencyError(BrowserAttachError):
    """Playwright (or another browser-driver dependency) is missing or broken."""


class BrowserDriver(Protocol):
    """What a browser task may do: the guarded surface, with no way to finalize a form."""

    def aria_snapshot(self) -> str: ...

    def click(
        self, name: str, role: str | None = None, wait_s: float = DEFAULT_CLICK_WAIT_S
    ) -> str: ...

    def close(self) -> None: ...

    def fill(self, label: str, value: str) -> None: ...

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

    def press(self, label: str, key: str) -> None: ...

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

    def title(self) -> str: ...

    def upload(self, label: str, path: Path) -> None: ...

    @property
    def url(self) -> str: ...


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


DEFAULT_ACTION_TIMEOUT_S = 30.0


DEFAULT_FRAME_WAIT_S = 30.0


DEFAULT_SCROLL_MAX_STEPS = 400


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
        page: Any,
        playwright: Any,
        timeout_s: float,
        existing_frames: Any = None,
    ) -> None:
        self._browser = browser
        self._closed = False
        self._context = page.context
        self._existing_frames = existing_frames
        self._net_installed = False
        self._navigation_handler: Any = None
        self._page = page
        self._playwright = playwright
        self._timeout_ms = timeout_s * 1000
        self._timeout_s = timeout_s

    def close(self) -> None:
        """Disconnect from the browser; it keeps running with the user's session."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._existing_frames is not None:
                with contextlib.suppress(Exception):
                    self._existing_frames.close()
            if self._navigation_handler is not None:
                with contextlib.suppress(_playwright_error()):
                    self._context.unroute("**/*", self._navigation_handler)
            if self._net_installed:
                with contextlib.suppress(_playwright_error()):
                    self._context.unroute("**/*", _abort_non_get_navigation)
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

    def block_form_submission(self) -> None:
        """Swallow every non-GET navigation while attached (a form POST), whatever triggers it.

        This is the backstop behind the guard's checks: a form can also be submitted by a
        script, an inherited handler or a field that submits on change, none of which a
        static look at one control can rule out. The route lives only on this connection, so
        it is gone once the driver detaches and never gets in the way of the user's own
        final click.
        """
        self._require_open()
        if self._net_installed:
            return
        try:
            self._context.route("**/*", _abort_non_get_navigation)
        except _playwright_error() as exc:
            raise self._action_error("install the form-submission safety net", exc) from exc
        self._net_installed = True

    def checked_click(self, name: str, role: str, vet: Callable[[ControlInfo], None]) -> None:
        """Resolve one element, let `vet` inspect that very element, then click it.

        Inspecting and clicking the same element handle leaves no window in which the page
        could swap in a different element between the check and the click.
        """
        self._require_open()
        try:
            handle = self._page.get_by_role(role, name=name, exact=True).element_handle(
                timeout=self._timeout_ms
            )
        except _playwright_error() as exc:
            raise self._action_error(f"find the {role} named {name!r}", exc) from exc
        try:
            vet(self._describe(handle))
            # A swallowed submit never commits a navigation, so do not wait for one.
            handle.click(timeout=self._timeout_ms, no_wait_after=True)
        except _playwright_error() as exc:
            raise self._action_error(f"click the {role} named {name!r}", exc) from exc

    def describe_field(self, label: str) -> ControlInfo:
        """Describe the element a label resolves to (for example to see it is a button)."""
        self._require_open()
        try:
            handle = self._page.get_by_label(label).element_handle(timeout=self._timeout_ms)
        except _playwright_error() as exc:
            raise self._action_error(f"find the field labelled {label!r}", exc) from exc
        try:
            return self._describe(handle)
        except _playwright_error() as exc:
            raise self._action_error(f"inspect the field labelled {label!r}", exc) from exc

    def fill(self, label: str, value: str) -> None:
        self._require_open()
        try:
            self._page.get_by_label(label).fill(value, timeout=self._timeout_ms)
        except _playwright_error() as exc:
            raise self._action_error(f"fill the field labelled {label!r}", exc) from exc

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
        while not _all_addressed(self._child_frames()):
            if time.monotonic() >= deadline:
                break
            self._pause()
        found: list[FrameInfo] = []
        for number, frame in enumerate(self._child_frames(), start=1):
            host = _web_host(frame.url)
            port = urlsplit(frame.url).port if host else None
            found.append(
                FrameInfo(
                    allowed=bool(host) and is_allowed(host),
                    host=host,
                    index=number,
                    origin=f"{urlsplit(frame.url).scheme}://{host}{f':{port}' if port else ''}"
                    if host
                    else "",
                )
            )
        return tuple(found)

    def goto(self, url: str) -> None:
        self._require_open()
        checked_http_url(url)
        try:
            self._page.goto(url, timeout=self._timeout_ms)
        except _playwright_error() as exc:
            raise self._action_error(f"open {url!r}", exc) from exc

    def matching_roles(self, name: str, roles: Sequence[str], wait_s: float) -> tuple[str, ...]:
        """The roles in which a control named `name` exists, waiting up to `wait_s` for one.

        Polls the page's own accessibility tree (a count, not a click), so a wrong role or a
        missing control is reported after `wait_s`, not after the 30 second action timeout.
        Two controls with that name in one role are refused as ambiguous.
        """
        self._require_open()
        deadline = time.monotonic() + clamp_wait(wait_s)
        while True:
            found: list[str] = []
            for role in roles:
                try:
                    count = int(self._page.get_by_role(role, name=name, exact=True).count())
                except _playwright_error() as exc:
                    raise self._action_error(f"look for a control named {name!r}", exc) from exc
                if count > 1:
                    raise BrowserActionError(
                        f"Expected one {role} named {name!r}, got {count} {role}s named {name!r}"
                    )
                if count == 1:
                    found.append(role)
            if found or time.monotonic() >= deadline:
                return tuple(found)
            self._pause()

    def raw_press(self, label: str, key: str) -> None:
        self._require_open()
        try:
            self._page.get_by_label(label).press(key, timeout=self._timeout_ms)
        except _playwright_error() as exc:
            raise self._action_error(f"press {key!r} in the field labelled {label!r}", exc) from exc

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

    def title(self) -> str:
        self._require_open()
        try:
            return str(self._page.title())
        except _playwright_error() as exc:
            raise self._action_error("read the page title", exc) from exc

    def upload(self, label: str, path: Path) -> None:
        self._require_open()
        resolved = path.resolve()
        if resolved != path:
            raise BrowserActionError(
                f"Expected a fully resolved path (no symlinks or '..') to upload, got {str(path)!r}"
            )
        if not resolved.is_file():
            raise BrowserActionError(f"Expected an existing file to upload, got {str(path)!r}")
        try:
            self._page.get_by_label(label).set_input_files(str(resolved), timeout=self._timeout_ms)
        except _playwright_error() as exc:
            raise self._action_error(f"upload to the field labelled {label!r}", exc) from exc

    @property
    def url(self) -> str:
        return str(self._page.url)

    def _describe(self, handle: Any) -> ControlInfo:
        raw = handle.evaluate(_DESCRIBE_CONTROL_JS)
        return ControlInfo(
            tag=str(raw["tag"]),
            input_type=str(raw["type"]),
            in_form=bool(raw["form"]),
            has_formaction=bool(raw["formaction"]),
            href=str(raw["href"]),
            inline_handler=str(raw["onclick"]),
            target=str(raw.get("target", "")),
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
            known = {str(frame.url) for frame in self._page.frames}
            try:
                frames.extend(self._existing_frames.frames(known))
            except OSError, ValueError, BrowserActionError:
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

    def _require_open(self) -> None:
        if self._closed:
            raise BrowserActionError("Expected an attached browser, got a closed driver")


class RawBrowserDriver(Protocol):
    """The unguarded surface `GuardedDriver` wraps (`PlaywrightDriver` implements it)."""

    def aria_snapshot(self) -> str: ...

    def block_form_submission(self) -> None: ...

    def checked_click(self, name: str, role: str, vet: Callable[[ControlInfo], None]) -> None: ...

    def close(self) -> None: ...

    def describe_field(self, label: str) -> ControlInfo: ...

    def fill(self, label: str, value: str) -> None: ...

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

    def matching_roles(self, name: str, roles: Sequence[str], wait_s: float) -> tuple[str, ...]: ...

    def raw_press(self, label: str, key: str) -> None: ...

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

    def title(self) -> str: ...

    def upload(self, label: str, path: Path) -> None: ...

    @property
    def url(self) -> str: ...


def attach(
    profile: BrowserProfile,
    *,
    attempts: int = ATTACH_ATTEMPTS,
    factory: Callable[[], Any] | None = None,
    fetch_version: Callable[[str, float], dict[str, Any]] | None = None,
    pid_alive: Callable[[int], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout_s: float = DEFAULT_ACTION_TIMEOUT_S,
) -> PlaywrightDriver:
    """Attach to the browser launched for `profile`, refusing profiles blumkin did not make."""
    require_blumkin_profile(profile.path)
    session = read_session(profile.path, pid_alive=pid_alive)
    if session is None:
        raise BrowserAttachError(
            f"Expected a running browser for task {profile.task!r}, got none; "
            f"run `blumkin browser launch {profile.task}` and sign in first"
        )
    return open_driver(
        session.endpoint,
        attempts=attempts,
        factory=factory,
        fetch_version=fetch_version,
        sleep=sleep,
        timeout_s=timeout_s,
    )


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
                raise BrowserAttachError(
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
                read_existing_frames=factory is None,
                timeout_s=timeout_s,
            )
        detail = str(last).splitlines()[0] if last and str(last) else "no detail"
        raise BrowserAttachError(
            f"Expected to attach to {endpoint}, got {detail} after {attempts} attempts"
        )
    except BaseException:
        with contextlib.suppress(_playwright_error()):
            playwright.stop()
        raise


_POLL_MS = 250


_SCROLL_SETTLE_MS = 250


_DESCRIBE_CONTROL_JS = (
    "(e) => ({tag: e.tagName.toLowerCase(), "
    "type: String(e.type || '').toLowerCase(), "
    "form: !!e.form, "
    "formaction: e.hasAttribute('formaction') || e.hasAttribute('formmethod'), "
    "href: String(e.getAttribute('href') || ''), "
    "onclick: String(e.getAttribute('onclick') || ''), "
    "target: String(e.getAttribute('target') || '')})"
)


class _NeverRaised(Exception):
    """Stands in for Playwright's error type when it cannot be imported; nothing raises it."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: `/json/version` must answer from the endpoint itself."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _abort_non_get_navigation(route: Any) -> None:
    """Swallow a page navigation that is not a GET (a form POST); let everything else through.

    The request is answered with an empty `204 No Content`, which tells the browser to stay
    on the current page. Aborting it instead would replace the page with an error page and
    lose whatever the user had filled in.
    """
    request = route.request
    if request.is_navigation_request() and request.method not in ("GET", "HEAD"):
        route.fulfill(status=204)
    else:
        route.continue_()


def _driver_for(
    browser: Any,
    playwright: Any,
    *,
    endpoint: str,
    existing_frames: Any,
    read_existing_frames: bool,
    timeout_s: float,
) -> PlaywrightDriver:
    try:
        contexts = list(browser.contexts)
        if not contexts:
            raise BrowserAttachError(f"Expected a browser context at {endpoint}, got none")
        pages = list(contexts[0].pages)
        page = pages[0] if pages else contexts[0].new_page()
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


def _existing_frames_for(endpoint: str, page: Any, timeout_s: float) -> Any:
    """The source of frames Playwright cannot see on `page`, read over their own sockets."""
    from blumkin.browser.cdpws import CdpSocket, ExistingFrames

    main_id: list[str] = []

    def main_frame_id() -> str:
        if not main_id:
            session = page.context.new_cdp_session(page)
            try:
                tree = session.send("Page.getFrameTree")
                main_id.append(str(tree["frameTree"]["frame"]["id"]))
            finally:
                with contextlib.suppress(_playwright_error()):
                    session.detach()
        return main_id[0]

    return ExistingFrames(
        endpoint_port=urlsplit(endpoint).port or 0,
        fetch_targets=lambda: _fetch_targets(endpoint, timeout_s),
        main_frame_id=main_frame_id,
        open_socket=lambda ws_url: CdpSocket.connect(ws_url, timeout_s=timeout_s),
    )


def _fetch_targets(endpoint: str, timeout_s: float) -> list[dict[str, Any]]:
    """Read `/json/list` from a loopback DevTools endpoint: every target Chrome knows."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects)
    with opener.open(f"{endpoint}/json/list", timeout=timeout_s) as response:
        data = json.load(response)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON list from /json/list, got {type(data).__name__}")
    return [item for item in data if isinstance(item, dict)]


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


def _all_addressed(frames: Sequence[Any]) -> bool:
    """True when there is at least one frame and every frame has a web address."""
    return bool(frames) and all(_web_host(frame.url) for frame in frames)
