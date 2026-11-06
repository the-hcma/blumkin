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
<div id=nested>
  <a href="#" role=button>Add</a> <a href="#" role=button>Delete</a>
  <button type=submit aria-label="Continue" form=report>
    <span role=button tabindex=0>OK</span></button>
</div>
<div id=big>
  <button type=button>Add</button> <button type=button>Delete</button>
  <button type=submit form=report>Submit</button>
  <a href="#" role=button onclick="window.bigOk = true; return false">OK</a>
</div>
<div id=search>
  <a href="#" role=button>Reset</a> <a href="#" role=button>Search</a>
  <a href="#" role=button onclick="window.searchOk = true; return false">OK</a>
</div>
"""


def _driver(page, browser, landmarks, *, in_form=False):  # noqa: ANN001, ANN202
    from blumkin.browser.cdp import PlaywrightDriver
    from blumkin.browser.guard import GuardedDriver, GuardPolicy

    raw = PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=3)
    policy = GuardPolicy(
        allowed_controls=frozenset({"Save", "Cancel"}),
        dialog_confirm=frozenset({"OK"}),
        dialog_confirm_in_form=in_form,
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
            for nth in (3, 4, 5, 6):
                with pytest.raises(SubmitBlockedError):
                    driver.click("OK", nth=nth)
            assert page.evaluate("window.wholeSubmitted") is None
            assert page.evaluate("window.wrappedSubmit") is None
        finally:
            browser.close()


def test_each_popup_is_found_by_its_own_group_of_landmarks() -> None:
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
            only_attachments = _driver(page, browser, (("Add", "Delete"),))
            search_ok = page.get_by_role("button", name="OK", exact=True).count()  # the last one
            with pytest.raises(SubmitBlockedError):
                only_attachments.click("OK", nth=search_ok)
            assert page.evaluate("window.searchOk") is None

            both = _driver(page, browser, (("Add", "Delete"), ("Reset", "Search")))
            both.click("OK", nth=search_ok)
            assert page.evaluate("window.searchOk") is True
            both.click("OK", nth=1)
            assert page.evaluate("window.okPressed") is True
        finally:
            browser.close()


_POST_PAGE = """
<form id=report method=post onsubmit="window.submitted = true; return false">
  <button type=submit>Submit</button>
  <div id=popup>
    <a href="#" role=button>Add</a> <a href="#" role=button>Delete</a>
    <button type=submit onclick="window.okPressed = true">OK</button>
  </div>
</form>
<form id=whole method=post onsubmit="window.wholeSubmitted = true; return false">
  <a href="#" role=button>Add</a> <a href="#" role=button>Delete</a>
  <button type=submit>OK</button>
</form>
"""


def test_the_opt_in_clicks_a_popup_in_a_post_form_but_never_the_form_itself() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.discovery import resolve_browser
    from blumkin.browser.guard import SubmitBlockedError

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(_POST_PAGE)
            off = _driver(page, browser, (("Add", "Delete"),))
            with pytest.raises(SubmitBlockedError):
                off.click("OK", nth=1)

            on = _driver(page, browser, (("Add", "Delete"),), in_form=True)
            on.click("OK", nth=1)
            assert page.evaluate("window.okPressed") is True
            with pytest.raises(SubmitBlockedError):
                on.click("OK", nth=2)
            assert page.evaluate("window.wholeSubmitted") is None
        finally:
            browser.close()


_ONE_FORM_PAGE = """
<form id=report method=post onsubmit="return false">
  <button type=submit>Submit</button>
  <div id=attachments>
    <a href="#" role=button>Add</a> <a href="#" role=button>Delete</a>
    <button type=submit onclick="window.attachOk = true">OK</button>
  </div>
  <div id=search>
    <a href="#" role=button>Reset</a> <a href="#" role=button>Search</a>
    <button type=submit onclick="window.searchOk = true">OK</button>
  </div>
</form>
"""


def test_two_popups_in_one_form_are_each_found_by_their_own_group() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.discovery import resolve_browser

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(_ONE_FORM_PAGE)
            driver = _driver(page, browser, (("Add", "Delete"), ("Reset", "Search")), in_form=True)

            driver.click("OK", nth=2)
            driver.click("OK", nth=1)

            assert page.evaluate("window.searchOk") is True
            assert page.evaluate("window.attachOk") is True
        finally:
            browser.close()


_LIST_PAGE = """
<ul role=grid>
  <li role=row><div role=gridcell tabindex=0
      onclick="window.picked = 'probe'">blumkin-probe Created By Henrique 11/06/2026</div></li>
  <li role=row><div role=gridcell tabindex=0
      onclick="window.picked = 'other'">blumkin-other Created By Henrique 11/06/2026</div></li>
</ul>
"""


def test_a_prefix_pick_chooses_the_gridcell_whose_name_starts_with_the_text() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.discovery import resolve_browser

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(_LIST_PAGE)
            driver = _driver(page, browser, ())

            driver.pick("blumkin-probe", "gridcell", None, True)

            assert page.evaluate("window.picked") == "probe"
        finally:
            browser.close()


_LABELLED_PAGE = """
<span id=hidden aria-label="Template Submit now"></span>
<ul role=grid>
  <li role=row><div role=gridcell tabindex=0 aria-labelledby=hidden
      onclick="window.picked = 'labelled'">Template</div></li>
</ul>
"""


def test_a_prefix_pick_refuses_a_name_that_only_a_label_elsewhere_makes_final() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.discovery import resolve_browser
    from blumkin.browser.guard import SubmitBlockedError

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(_LABELLED_PAGE)
            driver = _driver(page, browser, ())

            with pytest.raises(SubmitBlockedError):
                driver.pick("Template", "gridcell", None, True)

            assert page.evaluate("window.picked") is None
        finally:
            browser.close()
