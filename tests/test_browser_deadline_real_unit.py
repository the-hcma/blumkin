"""A page evaluation that never returns, in a real Chrome (BLUMKIN_BROWSER_TESTS=1, #623)."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BLUMKIN_BROWSER_TESTS") != "1", reason="needs BLUMKIN_BROWSER_TESTS=1"
)


def test_a_hung_evaluation_hits_the_deadline_and_the_driver_still_closes() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.cdp import BrowserActionError, PlaywrightDriver
    from blumkin.browser.discovery import resolve_browser

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content("<p>hello</p>")
            driver = PlaywrightDriver(
                browser=browser,
                page=page,
                playwright=SimpleNamespace(stop=lambda: None),
                timeout_s=1,
            )

            started = time.monotonic()
            with pytest.raises(BrowserActionError, match="may or may not have happened"):
                with driver._bounded(1.0, "wait for a page that never answers"):
                    page.evaluate("new Promise(() => {})")
            assert time.monotonic() - started < 5

            with pytest.raises(BrowserActionError, match="stopped answering"):
                driver.aria_snapshot()
            started = time.monotonic()
            driver.close()
            assert time.monotonic() - started < 2
        finally:
            browser.close()
