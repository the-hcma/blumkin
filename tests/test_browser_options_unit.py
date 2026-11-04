"""`list_options` and a narrowed `press` in a real Chrome (BLUMKIN_BROWSER_TESTS=1, #596)."""

from __future__ import annotations

import os
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BLUMKIN_BROWSER_TESTS") != "1", reason="needs BLUMKIN_BROWSER_TESTS=1"
)

# A combobox whose list is virtualized: ten rows are in the DOM at a time and the window moves
# as the list scrolls. Choosing a row would set `window.chosen`; nothing here may do that.
_PAGE = """
<style>#lb { height: 120px; overflow-y: auto; border: 1px solid #000; }
#lb div[role=option] { height: 24px; }</style>
<label id=l>Time Reporting Code</label>
<div role=combobox tabindex=0 aria-labelledby=l aria-controls=lb aria-haspopup=listbox
  aria-expanded=false id=cb>pick</div>
<div id=lb role=listbox style="display:none"></div>
<label>Pay <select id=sel><option>Regular</option><option>Holiday</option></select></label>
<label>Cell <input id=c1></label><label>Cell <input id=c2></label>
<script>
const names = Array.from({length: 33}, (_, i) => 'Code ' + String(i + 1).padStart(2, '0'));
window.chosen = null; window.keys = [];
const lb = document.getElementById('lb');
const cb = document.getElementById('cb');
const spacer = document.createElement('div'); spacer.style.position = 'relative';
spacer.style.height = (names.length * 24) + 'px'; lb.appendChild(spacer);
function render() {
  const first = Math.floor(lb.scrollTop / 24);
  spacer.innerHTML = '';
  names.slice(first, first + 10).forEach((n, i) => {
    const d = document.createElement('div'); d.setAttribute('role', 'option');
    d.textContent = n; d.style.position = 'absolute'; d.style.top = ((first + i) * 24) + 'px';
    d.onclick = () => { window.chosen = n; };
    spacer.appendChild(d);
  });
}
lb.addEventListener('scroll', render);
cb.addEventListener('click', () => { lb.style.display = 'block'; render(); });
cb.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { lb.style.display = 'none'; }
  window.keys.push(e.key);
});
document.getElementById('c2').addEventListener('keydown', (e) => window.keys.push('c2:' + e.key));
</script>
"""


def test_list_options_reads_a_virtualized_list_to_the_end_and_chooses_nothing() -> None:
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
            driver = PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=3)

            listing = driver.list_options("Time Reporting Code")

            assert listing.options == tuple(f"Code {i:02d}" for i in range(1, 34))
            assert listing.complete and listing.closed
            assert page.evaluate("window.chosen") is None
            assert page.locator("#lb").is_hidden()

            native = driver.list_options("Pay")
            assert native.options == ("Regular", "Holiday") and native.complete
        finally:
            browser.close()


def test_a_list_cut_short_by_the_cap_says_so() -> None:
    from playwright.sync_api import sync_playwright

    import blumkin.browser.cdp as cdp
    from blumkin.browser.cdp import PlaywrightDriver
    from blumkin.browser.discovery import resolve_browser

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        original = cdp._MAX_OPTIONS
        try:
            cdp._MAX_OPTIONS = 12
            page = browser.new_page()
            page.set_content(_PAGE)
            driver = PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=3)

            listing = driver.list_options("Time Reporting Code")

            assert len(listing.options) == 12 and not listing.complete
            assert listing.closed
        finally:
            cdp._MAX_OPTIONS = original
            browser.close()


def test_press_with_a_narrowing_reaches_the_nth_of_a_repeated_label() -> None:
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
            driver = PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=3)

            driver.raw_press("Cell", "Tab", FieldTarget(nth=2))

            assert page.evaluate("window.keys") == ["c2:Tab"]
            info = driver.describe_field("Cell", FieldTarget(nth=1))
            assert info.tag == "input"
        finally:
            browser.close()


def test_grid_view_matches_what_chrome_really_writes() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.aria_view import grid_view
    from blumkin.browser.discovery import resolve_browser

    html = (
        '<div role=grid aria-label="Time card">'
        "<div role=row><div role=columnheader>Day</div><div role=columnheader>Project</div></div>"
        "<div role=row><div role=rowheader>Mon</div><div role=gridcell>"
        '<input aria-label="Project" value="Alpha"></div></div>'
        "<div role=row><div role=rowheader>Tue</div><div role=gridcell>"
        '<input aria-label="Project" value="Beta"></div></div></div>'
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(html)
            view = grid_view(page.locator("body").aria_snapshot())

            assert view["columns"] == ["Day", "Project"]
            assert [r["label"] for r in view["rows"]] == ["Mon", "Tue"]
            second = view["rows"][1]["cells"][0]["fields"][0]
            assert second["nth"] == 2
            # The nth a caller reads here reaches the second row's field in `get_by_role`.
            nth = page.get_by_role("textbox", name="Project").nth(second["nth"] - 1)
            assert nth.input_value() == "Beta"
        finally:
            browser.close()


_LISTBOX_PAGE = """
<label id=l>Actions</label>
<div role=listbox aria-labelledby=l id=lb style="height:60px;overflow-y:auto">
 <div role=option onclick="window.chosen='a'" style="height:30px">Edit</div>
 <div role=option onclick="window.chosen='b'" style="height:30px">Copy</div>
 <div role=option onclick="window.chosen='c'" style="height:30px">Archive</div>
 <div role=option onclick="window.chosen='d'" style="height:30px">Delete</div>
</div>
<script>window.chosen = null; window.keys = [];
document.addEventListener('keydown', (e) => window.keys.push(e.key));</script>
"""


def test_a_listbox_that_is_already_on_the_page_is_read_in_place_never_clicked() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.cdp import PlaywrightDriver
    from blumkin.browser.discovery import resolve_browser

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(_LISTBOX_PAGE)
            driver = PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=3)

            listing = driver.list_options("Actions")

            assert listing.options == ("Edit", "Copy", "Archive", "Delete")
            assert page.evaluate("window.chosen") is None
            assert page.evaluate("window.keys") == []  # not even an Escape
        finally:
            browser.close()


def test_a_native_select_over_the_cap_says_the_list_is_cut() -> None:
    from playwright.sync_api import sync_playwright

    import blumkin.browser.cdp as cdp
    from blumkin.browser.cdp import PlaywrightDriver
    from blumkin.browser.discovery import resolve_browser

    options = "".join(f"<option>Item {i}</option>" for i in range(20))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        original = cdp._MAX_OPTIONS
        try:
            cdp._MAX_OPTIONS = 5
            page = browser.new_page()
            page.set_content(f"<label>Pick <select>{options}</select></label>")
            driver = PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=3)

            listing = driver.list_options("Pick")

            assert len(listing.options) == 5 and not listing.complete
        finally:
            cdp._MAX_OPTIONS = original
            browser.close()


def test_press_checks_and_presses_the_same_element() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.cdp import FieldTarget, PlaywrightDriver
    from blumkin.browser.discovery import resolve_browser

    seen: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(_PAGE)
            driver = PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=3)

            def vet(info: object) -> None:
                seen.append(getattr(info, "tag", ""))
                # The page re-renders after the check: the second cell moves to the front.
                page.evaluate(
                    "document.body.insertBefore(document.getElementById('c2').parentElement, "
                    "document.body.firstChild)"
                )

            driver.press_field("Cell", "Tab", FieldTarget(nth=2), vet)

            assert seen == ["input"]
            assert page.evaluate("window.keys") == ["c2:Tab"]
        finally:
            browser.close()


def _driver_page(playwright: Any, html: str) -> tuple[Any, Any, Any]:
    from blumkin.browser.cdp import PlaywrightDriver
    from blumkin.browser.discovery import resolve_browser

    browser = playwright.chromium.launch(
        executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
    )
    page = browser.new_page()
    page.set_content(html)
    return browser, page, PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=3)


def test_a_list_that_opens_part_way_down_is_read_from_the_top() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser, page, driver = _driver_page(playwright, _PAGE)
        try:
            page.evaluate(
                "document.getElementById('cb').addEventListener('click', () => "
                "{ setTimeout(() => { document.getElementById('lb').scrollTop = 24 * 20; }, 0); })"
            )

            listing = driver.list_options("Time Reporting Code")

            assert listing.options[0] == "Code 01" and len(listing.options) == 33
            assert listing.complete
        finally:
            browser.close()


def test_a_combobox_the_page_already_opened_is_read_and_left_open() -> None:
    from playwright.sync_api import sync_playwright

    html = _PAGE.replace("aria-expanded=false id=cb>", "aria-expanded=true id=cb>").replace(
        'id=lb role=listbox style="display:none"', 'id=lb role=listbox style="display:block"'
    )
    html = html.replace("lb.style.display = 'block'; render();", "lb.style.display = 'none';")
    with sync_playwright() as playwright:
        browser, page, driver = _driver_page(playwright, html)
        try:
            page.evaluate("window.dispatchEvent(new Event('scroll'))")
            listing = driver.list_options("Time Reporting Code")

            # The click handler of this page would have closed the list: it was not clicked.
            assert page.locator("#lb").is_visible()
            assert listing.closed
        finally:
            browser.close()


def test_options_are_listed_by_the_name_select_would_match() -> None:
    from playwright.sync_api import sync_playwright

    html = """
    <label id=l>Code</label>
    <div role=combobox tabindex=0 aria-labelledby=l aria-controls=lb aria-expanded=false id=cb
      onclick="document.getElementById('lb').style.display='block'">x</div>
    <div id=lb role=listbox style="display:none">
      <div role=option aria-label="Regular Pay">RP</div>
      <div role=option aria-label="Holiday"></div>
      <div role=option>Plain</div>
    </div>"""
    with sync_playwright() as playwright:
        browser, _page, driver = _driver_page(playwright, html)
        try:
            listing = driver.list_options("Code")

            assert listing.options == ("Regular Pay", "Holiday", "Plain")
        finally:
            browser.close()


def test_a_popup_whose_scrollbar_is_on_a_wrapper_is_read_to_the_end() -> None:
    from playwright.sync_api import sync_playwright

    rows = "".join(f"<div role=option style='height:24px'>Row {i:02d}</div>" for i in range(30))
    html = f"""
    <label id=l>Code</label>
    <div role=combobox tabindex=0 aria-labelledby=l aria-controls=lb aria-expanded=false id=cb
      onclick="document.getElementById('wrap').style.display='block'">x</div>
    <div id=wrap style="display:none;height:100px;overflow-y:auto">
      <div id=lb role=listbox>{rows}</div></div>"""
    with sync_playwright() as playwright:
        browser, _page, driver = _driver_page(playwright, html)
        try:
            listing = driver.list_options("Code")

            assert len(listing.options) == 30 and listing.complete
        finally:
            browser.close()
