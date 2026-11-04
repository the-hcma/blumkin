"""`pick` in a real Chrome (BLUMKIN_BROWSER_TESTS=1, #608): rows are chosen, never controls."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BLUMKIN_BROWSER_TESTS") != "1", reason="needs BLUMKIN_BROWSER_TESTS=1"
)

# A list-of-values dialog: result rows are table rows with their own click handlers, not
# controls. The rest are things dressed as rows, or rows that hold, or sit in, a control.
_PAGE = """
<table>
  <tr onclick="window.chosen = 'boston'"><td>Boston</td><td>MA</td>
    <td><input type=hidden value=7></td></tr>
  <tr onclick="window.chosen = 'austin-1'"><td>Austin</td><td>TX</td></tr>
  <tr onclick="window.chosen = 'austin-2'"><td>Austin</td><td>TX</td></tr>
  <tr onclick="window.chosen = 'denver'"><td>Denver</td><td>CO</td>
    <td><button onclick="window.pressed = true; event.stopPropagation()">Remove</button></td></tr>
  <tr onclick="window.chosen = 'tab'"><td tabindex=0>Reno</td><td>NV</td></tr>
  <tr><td><a href="#x">Linked</a></td><td>Row</td></tr>
</table>
<a href="#y"><table><tr onclick="window.chosen = 'inside-link'"><td>Inside</td><td>Link</td></tr>
</table></a>
<label><table><tr onclick="window.chosen = 'inside-label'"><td>Inside</td><td>Label</td></tr>
</table></label>
<button role=row onclick="window.pressed = true">Add payee</button>
<a href="#z" role=row onclick="window.pressed = true">Go there</a>
<label role=row for=save>Label row</label>
<button id=save onclick="window.pressed = true">Save</button>
<div role=row contenteditable=true onclick="window.chosen = 'editable'">Editable row</div>
<div id=host role=row aria-label="Host row" onclick="window.chosen = 'host'"></div>
<div id=host2></div>
<div role=treeitem id=tree>
  <table><tr onclick="window.chosen = 'in-tree'"><td>In</td><td>Tree</td></tr></table>
</div>
<script>
  window.chosen = null; window.pressed = false;
  document.getElementById('host').attachShadow({mode: 'open'}).innerHTML =
    '<button onclick="window.pressed = true">Inner</button>';
  const root = document.getElementById('host2').attachShadow({mode: 'open'});
  root.innerHTML = '<div role=row onclick="window.chosen = 1">Shadow row</div>';
  document.getElementById('tree').addEventListener('click', () => { window.pressed = true; });
</script>
"""


def test_pick_clicks_one_plain_row_and_refuses_everything_that_is_or_holds_a_control() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.cdp import BrowserActionError, PlaywrightDriver
    from blumkin.browser.discovery import resolve_browser
    from blumkin.browser.guard import GuardedDriver, GuardPolicy, SubmitBlockedError

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(_PAGE)
            raw = PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=3)
            driver = GuardedDriver(raw, GuardPolicy())

            driver.pick("Boston MA")
            assert page.evaluate("window.chosen") == "boston"

            page.evaluate("window.chosen = null")
            with pytest.raises(BrowserActionError):
                driver.pick("Austin TX")
            assert page.evaluate("window.chosen") is None
            driver.pick("Austin TX", nth=2)
            assert page.evaluate("window.chosen") == "austin-2"

            for text in (
                "Denver CO Remove",
                "Reno NV",
                "Linked Row",
                "Inside Link",
                "Inside Label",
                "Label row",
                "Editable row",
                "Host row",
                "In Tree",
                "Shadow row",
            ):
                page.evaluate("window.chosen = null")
                with pytest.raises(SubmitBlockedError):
                    driver.pick(text)
                assert page.evaluate("window.chosen") is None, text
            assert page.evaluate("window.pressed") is False
            for text in ("Add payee", "Go there"):
                with pytest.raises(SubmitBlockedError):
                    driver.pick(text)
            assert page.evaluate("window.pressed") is False
        finally:
            browser.close()
