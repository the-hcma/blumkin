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


@pytest.fixture
def _own_browser(tmp_path: Any) -> Any:
    """A Chrome of its own, reachable by DevTools, and a web server for three made-up hosts.

    Every blumkin command attaches to a browser that is already running, so a frame that exists
    before the attach is the normal case; Playwright launching the browser itself never is.
    """
    import http.server
    import json
    import socket
    import subprocess
    import threading
    import time
    import urllib.request

    from blumkin.browser.discovery import resolve_browser

    def free_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    web, debugging = free_port(), free_port()
    bodies = {
        "app.example.com": f"<body>app<iframe src='http://other.example.org:{web}/x'></iframe>",
        "other.example.org": "<body>child</body>",
        "login.microsoftonline.com": "<body>sign in</body>",
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            host = (self.headers.get("Host") or "").split(":")[0]
            body = bodies.get(host, "<body>?</body>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", web), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    mapping = ",".join(f"MAP {host} 127.0.0.1" for host in bodies)
    chrome = subprocess.Popen(  # noqa: S603 - the browser this suite found, with fixed flags
        [
            str(resolve_browser(None).path),
            "--headless=new",
            "--no-sandbox",
            "--no-first-run",
            f"--remote-debugging-port={debugging}",
            f"--user-data-dir={tmp_path / 'profile'}",
            f"--host-resolver-rules={mapping}",
            "about:blank",
        ],  # fmt: skip
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        endpoint = f"http://127.0.0.1:{debugging}"
        deadline = time.monotonic() + 20
        while True:
            try:
                json.load(urllib.request.urlopen(f"{endpoint}/json/version", timeout=1))  # noqa: S310
                break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.fail(f"Expected Chrome to answer DevTools at {endpoint}, got none")
                time.sleep(0.2)
        yield endpoint, web
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=10)
        except subprocess.TimeoutExpired:
            chrome.kill()  # it ignored the request to end: do not leave it running
            chrome.wait(timeout=10)
        finally:
            server.shutdown()


def _wait_for_child(page: Any, address: str, seconds: float = 10.0) -> None:
    """Wait until a child frame of `page` has committed `address` (not for a fixed time)."""
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if any(str(frame.url) == address for frame in page.frames[1:]):
            return
        page.wait_for_timeout(50)
    pytest.fail(f"Expected a child frame at {address}, got {[f.url for f in page.frames]}")


def test_a_cross_process_frame_that_existed_before_the_attach_is_counted_when_it_changes(
    _own_browser: Any,
) -> None:
    """The limit the guide used to state, checked the way blumkin attaches (#807).

    One attach builds the page and lets go; a second one, as the next command's, finds the
    cross-origin frame already there. Its trip to a sign-in host and back during the watch is
    still reported by Playwright's frame events, so a capture that watches them refuses.
    """
    from playwright.sync_api import sync_playwright

    endpoint, web = _own_browser
    with sync_playwright() as first:
        browser = first.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(f"http://app.example.com:{web}/")
        _wait_for_child(page, f"http://other.example.org:{web}/x")  # the frame is there

    with sync_playwright() as second:
        browser = second.chromium.connect_over_cdp(endpoint)
        page = next(p for c in browser.contexts for p in c.pages if "app.example.com" in p.url)
        driver = PlaywrightDriver(
            browser=browser,
            page=page,
            playwright=SimpleNamespace(stop=lambda: None),
            timeout_s=5.0,
        )
        # The frame was there before this attach (the first one waited for it to commit and let
        # go); waiting for it here, not counting at once: Playwright may list it a moment late.
        _wait_for_child(page, f"http://other.example.org:{web}/x")
        # Playwright's own list, with the frame's real address (this driver is built without the
        # `/json/list` inventory, which the `existing_frames` tests in `test_browser_cdp_unit.py`
        # cover)
        assert driver.frame_hosts() == ("app.example.com", "other.example.org")
        sign_in = f"http://login.microsoftonline.com:{web}/x"
        back = f"http://other.example.org:{web}/x"
        with driver.watch_documents() as watch:
            page.evaluate(f"document.querySelector('iframe').src = '{sign_in}'")
            _wait_for_child(page, sign_in)  # committed, not assumed after a pause
            page.evaluate(f"document.querySelector('iframe').src = '{back}'")
            _wait_for_child(page, back)
            page.wait_for_timeout(100)  # an event still on its way is counted

        assert watch.changes >= 2  # out to the sign-in host, and back
