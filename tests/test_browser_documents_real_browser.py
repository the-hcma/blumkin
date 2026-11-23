"""Which documents a page shows, against a real browser (#807): needs a browser, nothing else.

These check the claims the guide makes about screenshots: a round trip through a sign-in page
changes the frame signature though the address reads the same, and the frame tree leaves out a
cross-origin frame that the frame events do count. They run only with `BLUMKIN_BROWSER_TESTS=1`
(the browser job picks the file up by that string, and the hermetic run skips it);
`test_browser_capture_unit.py` and `test_browser_cdp_unit.py` cover the same logic with fakes.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

from blumkin.browser.cdp import PlaywrightDriver

pytestmark = pytest.mark.skipif(
    os.environ.get("BLUMKIN_BROWSER_TESTS") != "1", reason="needs BLUMKIN_BROWSER_TESTS=1"
)

_APP = "<!doctype html><title>app</title><body>app</body>"
_LOGIN = "<!doctype html><body>sign in</body>"


def _serve(body: str) -> Any:
    """A route handler answering with `body` (a factory: Playwright passes a handler's second
    parameter the request, so a default argument cannot carry the body)."""

    def handler(route: Any) -> None:
        route.fulfill(body=body, content_type="text/html")

    return handler


@pytest.fixture
def _page() -> Any:
    """A driver on a scripted page, in a browser of its own (closing the driver closes it)."""
    from playwright.sync_api import sync_playwright

    from blumkin.browser.discovery import resolve_browser

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page(viewport={"width": 800, "height": 450})
            for pattern, body in (
                ("https://app.example.com/**", _APP),
                ("https://login.microsoftonline.com/**", _LOGIN),
                ("https://other.example.org/**", "<body>child</body>"),
            ):
                page.route(pattern, _serve(body))
            page.goto("https://app.example.com/")
            driver = PlaywrightDriver(
                browser=browser,
                page=page,
                playwright=SimpleNamespace(stop=lambda: None),
                timeout_s=5.0,
            )
            yield driver, page
        finally:
            browser.close()


def test_a_round_trip_through_a_sign_in_page_changes_the_signature_not_the_address(
    _page: Any,
) -> None:
    """The case an address compared twice cannot see: out to a sign-in page and back."""
    driver, page = _page
    before = driver.frame_signature()
    address = page.url

    page.goto("https://login.microsoftonline.com/x")
    page.goto("https://app.example.com/")

    assert page.url == address  # the address reads the same at both ends
    assert driver.frame_signature() != before  # but another document was loaded in between
    assert driver.frame_signature() == driver.frame_signature()  # and it is stable while it stays


def test_a_cross_origin_frame_that_comes_and_goes_is_counted_though_the_tree_omits_it(
    _page: Any,
) -> None:
    """Why the capture also counts frame events: the tree leaves out cross-process frames."""
    driver, page = _page
    before = driver.frame_signature()

    with driver.watch_documents() as watch:
        page.set_content("<iframe src='https://other.example.org/x'></iframe>")
        page.wait_for_timeout(300)
        listed = len(page.frames)  # Playwright sees the frame...
        in_between = driver.frame_signature()  # ...the frame tree does not list it
        page.set_content("<body>no frame now</body>")
        page.wait_for_timeout(300)

    assert listed == 2 and len(page.frames) == 1  # it came, and it went
    assert len(in_between) == len(before) == 1  # the tree never held more than the page itself
    assert watch.changes > 0  # while the events saw it come and go


def test_a_page_that_stays_put_counts_no_frame_events(_page: Any) -> None:
    driver, page = _page

    with driver.watch_documents() as watch:
        page.wait_for_timeout(200)

    assert watch.changes == 0
