"""A popup the page does not mark as a dialog, found by its landmarks in a real Chrome (#624)."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BLUMKIN_BROWSER_TESTS") != "1", reason="needs BLUMKIN_BROWSER_TESTS=1"
)

# An Oracle ADF style page: the whole report is one form with its own Submit, and the
# attachments popup is a plain container (no role=dialog) with an Add/Delete toolbar.
_PAGE = """
<form id=report onsubmit="window.submitted = true; return false">
  <button type=button onclick="window.saved = true">Save</button>
  <button type=submit>Submit</button>
  <div id=popup>
    <a href="#" role=button>Add</a> <a href="#" role=button>Delete</a>
    <table><tr><td>receipt.pdf</td></tr></table>
    <a href="#" id=ok role=button onclick="window.okPressed = true; return false">OK</a>
    <a href="#" role=button onclick="window.cancelPressed = true; return false">Cancel</a>
  </div>
  <div id=other>
    <a href="#" role=button onclick="window.otherPressed = true; return false">OK</a>
  </div>
</form>
<form id=whole onsubmit="window.wholeSubmitted = true; return false">
  <a href="#" role=button>Add</a> <a href="#" role=button>Delete</a>
  <button type=submit>OK</button>
</form>
<div id=wrapped title="Send" onclick="window.wrappedSubmit = true">
  <a href="#" role=button>Add</a> <a href="#" role=button>Delete</a>
  <a href="#" role=button onclick="return false">OK</a>
</div>
<div id=labelled>
  <a href="#" role=button>Add</a> <a href="#" role=button>Delete</a>
  <label role=button for=elsewhere>OK</label>
</div>
<button type=submit id=elsewhere form=report>Elsewhere</button>
<div id=big>
  <button type=button>Add</button> <button type=button>Delete</button>
  <button type=submit form=report>Submit</button>
  <a href="#" role=button onclick="window.bigOk = true; return false">OK</a>
</div>
"""


def _driver(page, browser, landmarks):  # noqa: ANN001, ANN202
    from blumkin.browser.cdp import PlaywrightDriver
    from blumkin.browser.guard import GuardedDriver, GuardPolicy

    raw = PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=3)
    policy = GuardPolicy(
        allowed_controls=frozenset({"Save", "Cancel"}),
        dialog_confirm=frozenset({"OK"}),
        dialog_landmarks=landmarks,
    )
    return GuardedDriver(raw, policy)


def test_ok_is_clicked_in_the_popup_with_the_landmarks_and_nowhere_else() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.discovery import resolve_browser
    from blumkin.browser.guard import SubmitBlockedError

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(_PAGE)
            without = _driver(page, browser, ())
            with pytest.raises(SubmitBlockedError, match="not in a dialog"):
                without.click("OK", nth=1)

            driver = _driver(page, browser, ("Add", "Delete"))
            driver.click("OK", nth=1)
            assert page.evaluate("window.okPressed") is True

            # The second OK is outside the popup; its smallest container with Add and Delete
            # is the whole report form, which holds Submit.
            with pytest.raises(SubmitBlockedError, match="finalizing control"):
                driver.click("OK", nth=2)
            assert page.evaluate("window.otherPressed") is None

            # Refused: the report form is itself the container, a container that is a control
            # with a handler, and a label whose target lies outside the popup.
            for nth in (3, 4, 5):
                with pytest.raises(SubmitBlockedError):
                    driver.click("OK", nth=nth)
            assert page.evaluate("window.wholeSubmitted") is None
            assert page.evaluate("window.wrappedSubmit") is None
        finally:
            browser.close()
