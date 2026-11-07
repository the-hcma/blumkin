"""`click` waits for the page to settle and says where it ended (#625), in a real Chrome."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BLUMKIN_BROWSER_TESTS") != "1", reason="needs BLUMKIN_BROWSER_TESTS=1"
)

# Clicking Save swaps the page 700 ms later, as Oracle does after "Save and Close".
_PAGE = """
<h1 id=h>Create Expense Item</h1>
<button id=save type=button onclick="swap()">Save and Close</button>
<script>
  const swap = () => setTimeout(() => {
    document.getElementById('h').textContent = 'Edit Expense Report';
  }, 700);
</script>
<h2 style="display:none">Hidden heading</h2>
"""


def test_settle_waits_for_the_swap_and_expect_heading_reports_a_miss() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.cdp import PlaywrightDriver
    from blumkin.browser.discovery import resolve_browser

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(_PAGE)
            raw = PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=3)

            before = raw.settle_page()
            assert before.headings == ("Create Expense Item",)
            assert before.matched is None and before.quiet is True

            page.click("#save")
            late = raw.settle_page()
            assert late.headings == ("Edit Expense Report",)
            assert late.url.startswith("about:") or late.url == page.url
            page.evaluate("document.getElementById('h').textContent = 'Create Expense Item'")
            page.click("#save")
            waited = raw.settle_page(expect_heading="edit expense", expect_wait_s=5)
            assert waited.matched is True
            assert waited.headings == ("Edit Expense Report",)

            page.evaluate(
                "for (let i = 0; i < 25; i++) { const h = document.createElement('h3'); "
                "h3text(h, i); document.body.appendChild(h); } "
                "function h3text(h, i) { h.textContent = i === 24 ? 'x'.repeat(250) + ' tail-word' "
                ": 'filler ' + i; }"
            )
            far = raw.settle_page(expect_heading="tail-word", expect_wait_s=1)
            assert far.matched is True
            assert len(far.headings) == 20 and all(len(text) <= 200 for text in far.headings)
            assert far.headings[0] == "Edit Expense Report"
            assert far.headings[1] == "filler 6" and far.headings[-1].startswith("xxx")

            page.evaluate(
                "for (let i = 0; i < 300; i++) { const h = document.createElement('h3'); "
                "h.textContent = i === 299 ? 'late popup' : 'more ' + i; "
                "document.body.appendChild(h); }"
            )
            many = raw.settle_page(expect_heading="late popup", expect_wait_s=1)
            middle = raw.settle_page(expect_heading="more 150", expect_wait_s=1)
            assert middle.matched is True and len(middle.headings) == 20
            assert "more 150" not in middle.headings
            assert middle.headings[0] == "Edit Expense Report"
            assert middle.headings[-1] == "late popup"
            assert many.matched is True and many.headings[-1] == "late popup"

            missed = raw.settle_page(expect_heading="Nothing here", expect_wait_s=0.5)
            assert missed.matched is False
        finally:
            browser.close()
