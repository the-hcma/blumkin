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


def test_text_with_nothing_after_it_matches_no_header_and_text_text_reaches_a_text_header() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.cdp import BrowserActionError, PlaywrightDriver
    from blumkin.browser.discovery import resolve_browser

    page_html = _PAGE.replace(">2026<", ">-<").replace(">2027<", ">text:2027<")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(page_html)
            driver = PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=3)

            for empty in ("text:", "text:   "):
                with pytest.raises(BrowserActionError, match="header text after text:"):
                    driver._find_cell("Alpha", empty, 1)  # never the one header with no letters
            element, meta = driver._find_cell("Alpha", "text:text:2027", 1)
            assert element.inner_text().strip() == "a27" and meta["column"] == 2
        finally:
            browser.close()


def test_an_open_editor_is_one_in_edit_mode_or_holding_focus_not_any_visible_input() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser import cdp
    from blumkin.browser.discovery import resolve_browser

    html = (
        "<oj-data-grid>"
        "<div class='oj-datagrid-cell' id='a'><input id='ia' type='text'></div>"
        "<div class='oj-datagrid-cell' id='b'><input id='ib' type='text'></div>"
        "<div class='oj-datagrid-cell' id='c'><input id='ic' type='checkbox'></div>"
        "</oj-data-grid>"
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(html)
            # A visible input in a cell that is not in edit mode, with no focus: not an editor.
            assert page.evaluate(cdp._CELL_EDITING_JS, 1) is False
            # A cell in edit mode holds an editor, focused or not.
            page.evaluate("document.getElementById('b').classList.add('oj-datagrid-cell-edit')")
            assert page.evaluate(cdp._CELL_EDITING_JS, 1) is True
            page.evaluate("document.getElementById('b').classList.remove('oj-datagrid-cell-edit')")
            assert page.evaluate(cdp._CELL_EDITING_JS, 1) is False
            # A focused input is an editor wherever it sits.
            page.focus("#ia")
            assert page.evaluate(cdp._CELL_EDITING_JS, 1) is True
            # A checkbox is never an editor.
            page.evaluate("document.getElementById('ia').blur()")
            page.focus("#ic")
            assert page.evaluate(cdp._CELL_EDITING_JS, 1) is False
        finally:
            browser.close()
