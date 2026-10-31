"""The marker that makes the window blumkin drives recognizable (issue #590)."""

from __future__ import annotations

import os
import re
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError

from blumkin.browser.indicator import (
    INDICATOR_ID,
    INDICATOR_JS,
    contrast_with_white,
    indicator_args,
    task_colour,
)


def test_a_task_colour_is_a_hex_colour_that_is_the_same_every_time() -> None:
    colour = task_colour("expense-report")

    assert re.fullmatch(r"#[0-9a-f]{6}", colour)
    assert task_colour("expense-report") == colour


def test_different_tasks_get_different_colours() -> None:
    names = ["expense-report", "time-sheet", "teams-transcript", "weekly-sync", "meeting-notes"]

    assert len({task_colour(name) for name in names}) == len(names)


def test_the_colour_is_dark_enough_for_white_text() -> None:
    for name in ("a", "expense-report", "time-sheet", "x" * 40, "zz-9"):
        assert contrast_with_white(task_colour(name)) >= 4.5, name
    # The ratio is the WCAG one: black on white is 21:1, white on white is 1:1.
    assert round(contrast_with_white("#000000")) == 21
    assert contrast_with_white("#ffffff") == pytest.approx(1.0)


def test_the_arguments_carry_the_task_the_colour_and_the_element_id() -> None:
    assert indicator_args("time-sheet") == {
        "blocked": [],
        "colour": task_colour("time-sheet"),
        "hosts": [],
        "id": INDICATOR_ID,
        "task": "time-sheet",
    }
    with_hosts = indicator_args("time-sheet", ("erp.example.com",), ("login.example.net",))
    assert (with_hosts["hosts"], with_hosts["blocked"]) == (
        ["erp.example.com"],
        ["login.example.net"],
    )


def test_the_script_is_inert_hidden_and_not_added_twice() -> None:
    # Takes no clicks, hides from the accessibility tree, keeps its content in a closed shadow
    # root (so the page text does not include it) and is added once per page.
    for needle in (
        "pointer-events: none",
        "aria-hidden",
        "mode: 'closed'",
        "getElementById(id)",
        "style.cssText",
    ):
        assert needle in INDICATOR_JS
    # CSSOM, not an inline `style` attribute or a script element: a page's content security
    # policy blocks those but not this.
    assert "setAttribute('style'" not in INDICATOR_JS
    assert "createElement('script')" not in INDICATOR_JS


def test_the_task_name_is_passed_as_data_never_built_into_the_script() -> None:
    assert "expense-report" not in INDICATOR_JS
    assert "args.task" in INDICATOR_JS


class _Page:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.context = object()
        self._error = error

    def evaluate(self, script: str, arg: Any = None) -> None:
        if self._error is not None:
            raise self._error
        self.calls.append((script, arg))


def _driver(page: _Page) -> Any:
    from blumkin.browser.cdp import PlaywrightDriver

    return PlaywrightDriver(
        browser=object(), page=page, playwright=object(), timeout_s=5.0, existing_frames=None
    )


def test_mark_window_evaluates_the_script_with_the_task_arguments() -> None:
    page = _Page()

    _driver(page).mark_window("time-sheet")

    assert page.calls == [(INDICATOR_JS, indicator_args("time-sheet"))]


def test_a_page_that_refuses_the_marker_is_skipped_without_failing_the_command() -> None:
    _driver(_Page(PlaywrightError("Execution context was destroyed"))).mark_window("time-sheet")


def test_a_non_playwright_failure_is_not_hidden() -> None:
    with pytest.raises(RuntimeError):
        _driver(_Page(RuntimeError("boom"))).mark_window("time-sheet")


def test_the_window_colour_is_reported_by_status_launch_and_endpoint() -> None:
    from pathlib import Path

    from blumkin.browser.commands import _launch_payload, _profile_entry
    from blumkin.browser.profiles import BrowserProfile, BrowserSession

    session = BrowserSession(browser="chrome", pid=1, port=9222, started_at="2026-01-01T00:00:00Z")
    payload = _launch_payload("time-sheet", session, already_running=False, fallback_from=None)
    entry = _profile_entry(BrowserProfile(task="time-sheet", path=Path("/nonexistent")))

    assert payload["colour"] == task_colour("time-sheet")
    assert entry["colour"] == task_colour("time-sheet")


@pytest.mark.skipif(
    os.environ.get("BLUMKIN_BROWSER_TESTS") != "1", reason="needs BLUMKIN_BROWSER_TESTS=1"
)
def test_in_a_real_browser_the_marker_is_added_once_hidden_from_text_and_inert() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.discovery import resolve_browser

    page_html = (
        "<html><head><meta http-equiv='Content-Security-Policy' "
        "content=\"default-src 'none'; style-src 'none'\"></head><body><h1>Hello</h1>"
        "<button id='go' style='position:fixed;bottom:6px;left:6px'>Go</button></body></html>"
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page(viewport={"width": 800, "height": 600})
            page.route(
                "https://**/*",
                lambda route: route.fulfill(
                    body=page_html, content_type="text/html", headers={"cache-control": "no-store"}
                ),
            )

            def count() -> int:
                return int(page.evaluate(f"document.querySelectorAll('#{INDICATOR_ID}').length"))

            # Off the allowed hosts, or on a sign-in host, or on a page that is not http(s):
            # nothing is added.
            page.goto("https://other.example.net/")
            page.evaluate(INDICATOR_JS, indicator_args("t", ("erp.example.com", "*.corp.example")))
            assert count() == 0
            page.goto("https://login.example.net/")
            page.evaluate(INDICATOR_JS, indicator_args("t", (), ("login.example.net",)))
            assert count() == 0
            page.goto("about:blank")
            page.evaluate(INDICATOR_JS, indicator_args("t"))
            assert count() == 0
            page.goto("https://login.example.net./")  # a trailing dot is the same host
            page.evaluate(INDICATOR_JS, indicator_args("t", (), ("login.example.net",)))
            assert count() == 0
            page.goto("https://erp.example.com./")
            page.evaluate(INDICATOR_JS, indicator_args("t", ("erp.example.com",)))
            assert count() == 1

            page.goto("https://erp.example.com/")
            before = page.evaluate("document.body.innerText")
            for _ in range(2):
                page.evaluate(INDICATOR_JS, indicator_args("expense-report", ("erp.example.com",)))

            assert count() == 1
            assert page.evaluate("document.body.innerText") == before
            assert "blumkin" not in page.locator("body").aria_snapshot()
            page.click("#go")  # the pill sits over the button and takes no clicks

            page.goto("https://a.corp.example/")  # a `*.` pattern matches a subdomain only
            page.evaluate(INDICATOR_JS, indicator_args("t", ("*.corp.example",)))
            assert count() == 1
            page.goto("https://corp.example/")
            page.evaluate(INDICATOR_JS, indicator_args("t", ("*.corp.example",)))
            assert count() == 0
        finally:
            browser.close()
