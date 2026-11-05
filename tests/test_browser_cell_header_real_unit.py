"""A header that is only digits, named by its text, in a real Chrome (BLUMKIN_BROWSER_TESTS=1)."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BLUMKIN_BROWSER_TESTS") != "1", reason="needs BLUMKIN_BROWSER_TESTS=1"
)

# A minimal stand-in for a JET data grid: two row headers, two scrolling columns whose headers are
# the years 2026 and 2027, and the cells drawn where a row and a column meet.
_PAGE = """
<oj-data-grid style="position: relative; display: block; width: 600px; height: 200px">
  <div class="oj-datagrid-row-header">
    <div class="oj-datagrid-scroller">
      <div style="position:absolute;left:0;top:40px;width:80px;height:30px">
        <div class="oj-datagrid-header-cell">Alpha</div></div>
      <div style="position:absolute;left:0;top:80px;width:80px;height:30px">
        <div class="oj-datagrid-header-cell">Beta</div></div>
    </div>
  </div>
  <div class="oj-datagrid-column-header">
    <div class="oj-datagrid-scroller">
      <div style="position:absolute;left:100px;top:0;width:80px;height:30px">
        <div class="oj-datagrid-header-cell">2026</div></div>
      <div style="position:absolute;left:200px;top:0;width:80px;height:30px">
        <div class="oj-datagrid-header-cell">2027</div></div>
    </div>
  </div>
  <div class="oj-datagrid-databody">
    <div class="oj-datagrid-cell"
         style="position:absolute;left:100px;top:40px;width:80px;height:30px">a26</div>
    <div class="oj-datagrid-cell"
         style="position:absolute;left:200px;top:40px;width:80px;height:30px">a27</div>
    <div class="oj-datagrid-cell"
         style="position:absolute;left:100px;top:80px;width:80px;height:30px">b26</div>
    <div class="oj-datagrid-cell"
         style="position:absolute;left:200px;top:80px;width:80px;height:30px">b27</div>
  </div>
</oj-data-grid>
"""


def test_a_digits_only_header_is_named_by_its_text_with_the_text_prefix() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.cdp import BrowserActionError, PlaywrightDriver
    from blumkin.browser.discovery import resolve_browser

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(_PAGE)
            driver = PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=3)

            element, meta = driver._find_cell("Alpha", "text:2027", 1)
            assert element.inner_text().strip() == "a27"
            assert (meta["column"], meta["columnLabel"]) == (2, "2027")

            element, _meta = driver._find_cell(
                "2", "text: 2026", 1
            )  # a row position, a text column
            assert element.inner_text().strip() == "b26"

            # Without the prefix a number is a position, as it always was.
            element, meta = driver._find_cell("Alpha", "1", 1)
            assert element.inner_text().strip() == "a26" and meta["column"] == 1
            with pytest.raises(BrowserActionError, match="column 1 to 2, got 2027"):
                driver._find_cell("Alpha", "2027", 1)
            with pytest.raises(BrowserActionError, match="one column named 2030, got 0"):
                driver._find_cell("Alpha", "text:2030", 1)
        finally:
            browser.close()
