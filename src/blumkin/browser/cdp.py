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
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from blumkin.browser.profiles import (
    BrowserProfile,
    read_session,
    require_blumkin_profile,
)
from blumkin.prompt_injection import scan_for_injection

ATTACH_ATTEMPTS = 3


class BrowserActionError(RuntimeError):
    """A page action failed or was refused."""


class BrowserAttachError(RuntimeError):
    """The task browser could not be attached to."""


class BrowserDriver(Protocol):
    """What a browser task may do: the guarded surface, with no way to finalize a form."""

    def click(self, name: str, role: str = "button") -> None: ...

    def close(self) -> None: ...

    def fill(self, label: str, value: str) -> None: ...

    def goto(self, url: str) -> None: ...

    def press(self, label: str, key: str) -> None: ...

    def read_text(self) -> PageText: ...

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


DEFAULT_ACTION_TIMEOUT_S = 30.0


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

    def __init__(self, *, browser: Any, page: Any, playwright: Any, timeout_s: float) -> None:
        self._browser = browser
        self._closed = False
        self._context = page.context
        self._net_installed = False
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
            if self._net_installed:
                with contextlib.suppress(PlaywrightError):
                    self._context.unroute("**/*", _abort_non_get_navigation)
            with contextlib.suppress(PlaywrightError):
                self._browser.close()
        finally:
            with contextlib.suppress(PlaywrightError):
                self._playwright.stop()

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
        except PlaywrightError as exc:
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
        except PlaywrightError as exc:
            raise self._action_error(f"find the {role} named {name!r}", exc) from exc
        try:
            vet(self._describe(handle))
            # A swallowed submit never commits a navigation, so do not wait for one.
            handle.click(timeout=self._timeout_ms, no_wait_after=True)
        except PlaywrightError as exc:
            raise self._action_error(f"click the {role} named {name!r}", exc) from exc

    def describe_field(self, label: str) -> ControlInfo:
        """Describe the element a label resolves to (for example to see it is a button)."""
        self._require_open()
        try:
            handle = self._page.get_by_label(label).element_handle(timeout=self._timeout_ms)
        except PlaywrightError as exc:
            raise self._action_error(f"find the field labelled {label!r}", exc) from exc
        try:
            return self._describe(handle)
        except PlaywrightError as exc:
            raise self._action_error(f"inspect the field labelled {label!r}", exc) from exc

    def fill(self, label: str, value: str) -> None:
        self._require_open()
        try:
            self._page.get_by_label(label).fill(value, timeout=self._timeout_ms)
        except PlaywrightError as exc:
            raise self._action_error(f"fill the field labelled {label!r}", exc) from exc

    def goto(self, url: str) -> None:
        self._require_open()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise BrowserActionError(f"Expected an http or https URL to open, got {url!r}")
        try:
            self._page.goto(url, timeout=self._timeout_ms)
        except PlaywrightError as exc:
            raise self._action_error(f"open {url!r}", exc) from exc

    def raw_press(self, label: str, key: str) -> None:
        self._require_open()
        try:
            self._page.get_by_label(label).press(key, timeout=self._timeout_ms)
        except PlaywrightError as exc:
            raise self._action_error(f"press {key!r} in the field labelled {label!r}", exc) from exc

    def read_text(self) -> PageText:
        self._require_open()
        try:
            text = str(self._page.inner_text("body", timeout=self._timeout_ms))
        except PlaywrightError as exc:
            raise self._action_error("read the page text", exc) from exc
        return PageText(
            text=text,
            injection_warning=scan_for_injection(text, location="page.text").to_payload(),
        )

    def title(self) -> str:
        self._require_open()
        try:
            return str(self._page.title())
        except PlaywrightError as exc:
            raise self._action_error("read the page title", exc) from exc

    def upload(self, label: str, path: Path) -> None:
        self._require_open()
        resolved = path.resolve()
        if not resolved.is_file():
            raise BrowserActionError(f"Expected an existing file to upload, got {str(path)!r}")
        try:
            self._page.get_by_label(label).set_input_files(str(resolved), timeout=self._timeout_ms)
        except PlaywrightError as exc:
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
        )

    def _action_error(self, what: str, exc: Exception) -> BrowserActionError:
        return BrowserActionError(
            f"Expected to {what} (timeout {self._timeout_s} seconds), got: "
            f"{str(exc).splitlines()[0] if str(exc) else type(exc).__name__}"
        )

    def _require_open(self) -> None:
        if self._closed:
            raise BrowserActionError("Expected an attached browser, got a closed driver")


class RawBrowserDriver(Protocol):
    """The unguarded surface `GuardedDriver` wraps (`PlaywrightDriver` implements it)."""

    def block_form_submission(self) -> None: ...

    def checked_click(self, name: str, role: str, vet: Callable[[ControlInfo], None]) -> None: ...

    def close(self) -> None: ...

    def describe_field(self, label: str) -> ControlInfo: ...

    def fill(self, label: str, value: str) -> None: ...

    def goto(self, url: str) -> None: ...

    def raw_press(self, label: str, key: str) -> None: ...

    def read_text(self) -> PageText: ...

    def title(self) -> str: ...

    def upload(self, label: str, path: Path) -> None: ...

    @property
    def url(self) -> str: ...


def attach(
    profile: BrowserProfile,
    *,
    attempts: int = ATTACH_ATTEMPTS,
    factory: Callable[[], Any] = sync_playwright,
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


def open_driver(
    endpoint: str,
    *,
    attempts: int = ATTACH_ATTEMPTS,
    factory: Callable[[], Any] = sync_playwright,
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
            except PlaywrightError as exc:
                last = exc
                continue
            return _driver_for(browser, playwright, endpoint=endpoint, timeout_s=timeout_s)
        detail = str(last).splitlines()[0] if last and str(last) else "no detail"
        raise BrowserAttachError(
            f"Expected to attach to {endpoint}, got {detail} after {attempts} attempts"
        )
    except BaseException:
        with contextlib.suppress(PlaywrightError):
            playwright.stop()
        raise


_DESCRIBE_CONTROL_JS = (
    "(e) => ({tag: e.tagName.toLowerCase(), "
    "type: String(e.type || '').toLowerCase(), "
    "form: !!e.form, "
    "formaction: e.hasAttribute('formaction') || e.hasAttribute('formmethod'), "
    "href: String(e.getAttribute('href') || ''), "
    "onclick: String(e.getAttribute('onclick') || '')})"
)


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
    browser: Any, playwright: Any, *, endpoint: str, timeout_s: float
) -> PlaywrightDriver:
    try:
        contexts = list(browser.contexts)
        if not contexts:
            raise BrowserAttachError(f"Expected a browser context at {endpoint}, got none")
        pages = list(contexts[0].pages)
        page = pages[0] if pages else contexts[0].new_page()
        page.set_default_timeout(timeout_s * 1000)
    except BaseException:
        with contextlib.suppress(PlaywrightError):
            browser.close()
        raise
    return PlaywrightDriver(browser=browser, page=page, playwright=playwright, timeout_s=timeout_s)


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
