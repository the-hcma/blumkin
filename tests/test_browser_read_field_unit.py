"""`read_field` in a real Chrome (opt-in: BLUMKIN_BROWSER_TESTS=1, #617)."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BLUMKIN_BROWSER_TESTS") != "1", reason="needs BLUMKIN_BROWSER_TESTS=1"
)

_PAGE = """
<label>Card <input id=a value="4111 1111"></label>
<label>Pass <input type=password value="hunter2"></label>
<label>Code <input autocomplete="one-time-code" value="123456"></label>
<label>Tokens <input autocomplete="section-b current-password" value="s3cret"></label>
<label>Long <input id=long></label>
<label>Twin <input value="x"></label><label>Twin <input value="y"></label>
"""


def test_read_field_reports_each_outcome_in_a_real_page() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.cdp import FieldTarget, PlaywrightDriver
    from blumkin.browser.discovery import resolve_browser

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(_PAGE)
            driver = PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=1)

            found = driver.read_field("Card")
            assert (found.status, found.value) == ("read", "4111 1111")
            assert driver.read_field("Pass").status == "unreadable"
            assert driver.read_field("Code").status == "unreadable"
            assert driver.read_field("Tokens").status == "unreadable"
            assert driver.read_field("Twin").status == "ambiguous"
            assert driver.read_field("Twin", FieldTarget(nth=3)).status == "missing"
            page.evaluate("document.getElementById('long').value = 'a'.repeat(100001)")
            assert driver.read_field("Long").status == "unreadable"
            assert driver.read_field("Nothing here").status == "missing"
        finally:
            browser.close()
