"""`pick` in a real Chrome (BLUMKIN_BROWSER_TESTS=1, #608): rows are chosen, never controls."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BLUMKIN_BROWSER_TESTS") != "1", reason="needs BLUMKIN_BROWSER_TESTS=1"
)

# A list-of-values dialog: result rows are table rows, not clickable controls. One row holds a
# button of its own, and two rows share a text; choosing records `window.chosen`.
_PAGE = """
<table>
  <tr onclick="window.chosen = 'boston'"><td>Boston</td><td>MA</td></tr>
  <tr onclick="window.chosen = 'austin-1'"><td>Austin</td><td>TX</td></tr>
  <tr onclick="window.chosen = 'austin-2'"><td>Austin</td><td>TX</td></tr>
  <tr onclick="window.chosen = 'row-with-button'"><td>Denver</td><td>CO</td>
    <td><button onclick="window.pressed = true; event.stopPropagation()">Remove</button></td></tr>
</table>
<script>window.chosen = null; window.pressed = false;</script>
"""


def _driver(playwright: object, browser: object, page: object) -> object:
    from blumkin.browser.cdp import PlaywrightDriver

    return PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=3)  # type: ignore[arg-type]


def test_pick_clicks_the_one_row_with_that_text_and_refuses_the_ambiguous_and_the_busy() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.cdp import BrowserActionError, ControlInfo
    from blumkin.browser.discovery import resolve_browser

    def vet(info: ControlInfo) -> None:
        assert info.tag == "tr"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(_PAGE)
            driver = _driver(playwright, browser, page)

            driver.checked_pick("Boston MA", "row", vet)  # type: ignore[attr-defined]
            assert page.evaluate("window.chosen") == "boston"

            page.evaluate("window.chosen = null")
            with pytest.raises(BrowserActionError):
                driver.checked_pick("Austin TX", "row", vet)  # type: ignore[attr-defined]
            assert page.evaluate("window.chosen") is None

            driver.checked_pick("Austin TX", "row", vet, nth=2)  # type: ignore[attr-defined]
            assert page.evaluate("window.chosen") == "austin-2"

            page.evaluate("window.chosen = null")
            with pytest.raises(BrowserActionError, match="no controls"):
                driver.checked_pick("Denver CO Remove", "row", vet)  # type: ignore[attr-defined]
            assert page.evaluate("window.chosen") is None
            assert page.evaluate("window.pressed") is False
        finally:
            browser.close()
