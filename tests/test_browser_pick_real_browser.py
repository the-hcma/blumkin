"""`browser pick` on the implicit grid roles, against the real driver code in a real browser (#783).

The unit tests use fakes that accept any role without looking for an element. These run the real
`PlaywrightDriver.checked_pick` (role lookup, blocker detection, the click) on a real page, and
are skipped where no Chromium, Chrome or Edge is installed (the hermetic CI has none).
"""

from __future__ import annotations

from typing import Any

import pytest

from blumkin.browser.cdp import ControlInfo, PlaywrightDriver

_PAGE = """<!doctype html><title>start</title>
<table>
  <tr><th scope=row id=plain>Product A</th><td id=data>Plain cell E</td></tr>
  <tr><th scope=row><a href="#action" id=anchor>Product B</a></th><td>x</td></tr>
  <tr><th scope=row><button id=btn>Product C</button></th><td>x</td></tr>
  <tr><td><a href="#outer"><div role="rowheader" id=wrapped>Product D</div></a></td></tr>
  <tr><th scope=row id=handler>Product F</th><td>x</td></tr>
</table>
<script>
  for (const id of ['plain', 'data', 'handler']) {
    document.getElementById(id).addEventListener('click', () => {
      document.title = 'clicked ' + id;
    });
  }
</script>"""


@pytest.fixture(scope="module")
def _real_page() -> Any:
    sync_api = pytest.importorskip("playwright.sync_api")
    manager = sync_api.sync_playwright().start()
    browser = None
    for options in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
        try:
            browser = manager.chromium.launch(headless=True, **options)
            break
        except Exception:  # not installed here: try the next one
            continue
    if browser is None:
        manager.stop()
        pytest.skip("no Chromium, Chrome or Edge is available to run the real driver in")
    page = browser.new_page()
    yield browser, page, manager
    browser.close()
    manager.stop()


def _driver(real: Any) -> tuple[PlaywrightDriver, Any]:
    browser, page, manager = real
    page.set_content(_PAGE)
    page.evaluate("document.title = 'start'")
    return PlaywrightDriver(browser=browser, page=page, playwright=manager, timeout_s=5.0), page


def _pick(real: Any, name: str, role: str) -> tuple[ControlInfo, str]:
    driver, page = _driver(real)
    seen: list[ControlInfo] = []
    driver.checked_pick(name, role, seen.append)
    page.wait_for_timeout(100)
    return seen[0], page.title()


def test_a_plain_row_header_is_found_by_its_text_and_clicked(_real_page: Any) -> None:
    info, title = _pick(_real_page, "Product F", "rowheader")
    assert info.holds_controls is False
    assert title == "clicked handler"


def test_a_plain_cell_is_found_by_its_text_and_clicked(_real_page: Any) -> None:
    info, title = _pick(_real_page, "Plain cell E", "cell")
    assert info.holds_controls is False
    assert title == "clicked data"


@pytest.mark.parametrize("name", ["Product B", "Product C"])
def test_a_row_header_that_holds_a_control_is_reported_as_holding_one(
    _real_page: Any, name: str
) -> None:
    info, _ = _pick(_real_page, name, "rowheader")
    assert info.holds_controls is True


def test_a_row_header_inside_a_link_is_reported_as_sitting_inside_a_control(
    _real_page: Any,
) -> None:
    """`<a href><div role=rowheader>`: the click would land on the link, so the guard sees it."""
    info, _ = _pick(_real_page, "Product D", "rowheader")
    assert info.holds_controls is True


def test_the_guard_refuses_what_the_driver_reports_as_holding_a_control(_real_page: Any) -> None:
    """End to end: the guard's own vet callback, fed the real description, refuses the click."""
    from blumkin.browser.guard import GuardedDriver, GuardPolicy, SubmitBlockedError

    driver, page = _driver(_real_page)
    guarded = GuardedDriver(driver, GuardPolicy())
    with pytest.raises(SubmitBlockedError, match="holds or sits inside a control|is, holds"):
        guarded.pick("Product D", "rowheader")
    assert page.title() == "start"  # nothing was clicked
    guarded.pick("Product F", "rowheader")
    page.wait_for_timeout(100)
    assert page.title() == "clicked handler"


_MENU_PAGE = """<!doctype html><title>menu</title>
<button id=more aria-haspopup=menu aria-expanded=true>More actions</button>
<div role=menu aria-label="More actions">
  <div role=menuitem>Rename</div><div role=menuitem>Cancel subscription</div>
</div>
<div role=menu aria-label="Hidden" hidden><div role=menuitem>Secret</div></div>"""


def test_role_aria_snapshot_finds_a_visible_menu_by_role_and_by_name(_real_page: Any) -> None:
    driver, page = _driver(_real_page)
    page.set_content(_MENU_PAGE)

    found = driver.role_aria_snapshot("menu")
    assert found is not None
    text, count = found
    assert count == 1 and 'menuitem "Rename"' in text and "Secret" not in text
    named = driver.role_aria_snapshot("menu", "More actions")
    assert named is not None and 'menuitem "Cancel subscription"' in named[0]
    assert driver.role_aria_snapshot("menu", "Hidden") is None
    assert driver.role_aria_snapshot("listbox") is None
