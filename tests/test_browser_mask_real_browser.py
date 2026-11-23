"""Covering what must not be shown in a screenshot, in a real browser (#806).

The browser draws the bars itself, in the same render as the pixels, so these check what a
person would see: the right pixels are covered (a fixed header on a scrolled page, an element far
below the viewport, a high-density display, hundreds of matches), the rest of the page is not, and
the page is left as it was. They run with `BLUMKIN_BROWSER_TESTS=1` (the browser job picks the
file up by that string, and the hermetic run skips it); `test_browser_mask_unit.py` covers the
driver's own rules with fakes.
"""

from __future__ import annotations

import io
import os
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from blumkin.browser.cdp import PlaywrightDriver

pytestmark = pytest.mark.skipif(
    os.environ.get("BLUMKIN_BROWSER_TESTS") != "1", reason="needs BLUMKIN_BROWSER_TESTS=1"
)

_RED = (255, 0, 0)
_BLACK = (0, 0, 0)
_GREEN = (0, 170, 0)

_CELLS = "".join(
    '<div role="cell" style="height:4px;background:#ff0000"></div>'
    '<div style="height:4px;background:#ffffff"></div>'
    for _ in range(250)
)
_PAGE = (
    '<!doctype html><body style="margin:0;background:#ffffff">'
    '<header style="position:fixed;top:0;left:0;width:300px;height:40px;'
    'background:#ff0000">Ada Lovelace</header>'
    '<h1 style="margin:0;position:absolute;left:40px;top:60px;width:200px;height:40px;'
    'background:#ff0000;font:20px sans-serif">Ada Lovelace</h1>'
    '<p style="margin:0;position:absolute;left:40px;top:150px;width:200px;height:40px;'
    'background:#00aa00">other</p>'
    '<h1 style="margin:0;position:absolute;left:40px;top:1000px;width:200px;height:40px;'
    'background:#ff0000;font:20px sans-serif">Ada Lovelace</h1>'
    '<div style="position:absolute;left:300px;top:1100px;width:90px">' + _CELLS + "</div>"
    '<div style="height:1500px"></div>'
)


def _driver_on(scale: float) -> Any:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.discovery import resolve_browser

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            context = browser.new_context(
                viewport={"width": 400, "height": 300}, device_scale_factor=scale
            )
            page = context.new_page()
            page.set_content(_PAGE)
            yield (
                PlaywrightDriver(
                    browser=browser,
                    page=page,
                    playwright=SimpleNamespace(stop=lambda: None),
                    timeout_s=5.0,
                ),
                page,
            )
        finally:
            browser.close()


@pytest.fixture
def _plain() -> Any:
    yield from _driver_on(1.0)


@pytest.fixture
def _retina() -> Any:
    yield from _driver_on(2.0)


def _image(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def _count(image: Image.Image, colour: tuple[int, int, int], box: tuple[int, int, int, int]) -> int:
    left, top, right, bottom = box
    return sum(
        1
        for y in range(top, bottom, 2)
        for x in range(left, right, 2)
        if image.getpixel((x, y)) == colour
    )


def test_a_masked_heading_is_covered_and_the_rest_of_the_page_is_not(_plain: Any) -> None:
    driver, _ = _plain
    unmasked = _image(driver.screenshot_png())
    assert unmasked.getpixel((100, 80)) == _RED  # the heading is there to see

    covered = _image(driver.screenshot_png(masks=[("heading", "Ada Lovelace")]))

    assert covered.getpixel((100, 80)) == _BLACK
    assert covered.getpixel((100, 170)) == _GREEN  # the paragraph is untouched


def test_the_bar_lands_on_the_right_pixels_on_a_high_density_display(_retina: Any) -> None:
    driver, _ = _retina

    covered = _image(driver.screenshot_png(masks=[("heading", None)]))

    assert covered.getpixel((200, 160)) == _BLACK  # CSS (100, 80) at ratio 2
    assert covered.getpixel((200, 340)) == _GREEN  # the paragraph, CSS (100, 170)


def test_a_full_page_picture_covers_an_element_far_below_the_viewport(_plain: Any) -> None:
    driver, _ = _plain
    unmasked = _image(driver.screenshot_png(full_page=True))
    assert unmasked.getpixel((100, 1020)) == _RED

    covered = _image(driver.screenshot_png(full_page=True, masks=[("heading", "Ada Lovelace")]))

    assert covered.getpixel((100, 1020)) == _BLACK  # the second heading, 1000 px down
    assert covered.getpixel((100, 80)) == _BLACK  # and the first


def test_a_fixed_header_is_covered_where_it_is_drawn_on_a_scrolled_full_page_picture(
    _plain: Any,
) -> None:
    driver, page = _plain
    page.evaluate("window.scrollTo(0, 500)")  # the fixed header is now drawn 500 px down
    unmasked = _image(driver.screenshot_png(full_page=True))
    assert _count(unmasked, _RED, (0, 500, 300, 540)) > 0  # it is there to see, where it sits

    covered = _image(driver.screenshot_png(full_page=True, masks=[("banner", None)]))

    assert _count(covered, _RED, (0, 500, 300, 540)) == 0
    assert _count(covered, _BLACK, (0, 500, 300, 540)) > 0


def test_hundreds_of_matches_are_all_covered_with_none_left_over(_plain: Any) -> None:
    driver, _ = _plain
    box = (300, 1100, 390, 3100)  # all 250 cells: 8 px each, from y = 1100
    unmasked = _image(driver.screenshot_png(full_page=True))
    assert _count(unmasked, _RED, box) > 100  # 250 cells' worth of red

    covered = _image(driver.screenshot_png(full_page=True, masks=[("cell", None)]))

    assert _count(covered, _RED, box) == 0


def test_a_mask_for_nothing_on_the_page_refuses_the_capture(_plain: Any) -> None:
    from blumkin.browser.cdp import BrowserActionError

    driver, _ = _plain

    with pytest.raises(BrowserActionError, match="to mask"):
        driver.screenshot_png(masks=[("heading", "Somebody Else")])


def test_the_page_is_left_as_it_was_apart_from_one_overlay_that_lives_for_the_capture(
    _plain: Any,
) -> None:
    driver, page = _plain
    before = page.evaluate("document.documentElement.outerHTML")
    page.evaluate(
        """() => { window.__changes = []; new MutationObserver(records => records.forEach(r => {
            r.addedNodes.forEach(n => window.__changes.push('added ' + n.nodeName));
            r.removedNodes.forEach(n => window.__changes.push('removed ' + n.nodeName));
            if (r.type === 'attributes') window.__changes.push('attribute ' + r.attributeName);
        })).observe(document, {subtree: true, childList: true, attributes: true}); }"""
    )

    driver.screenshot_png(masks=[("heading", None)])
    page.wait_for_timeout(100)

    assert sorted(page.evaluate("window.__changes")) == ["added X-PW-GLASS", "removed X-PW-GLASS"]
    assert page.evaluate("document.documentElement.outerHTML") == before  # nothing is left behind


def test_headings_in_a_modal_dialog_and_a_popover_are_covered_too(_plain: Any) -> None:
    """Top-layer content renders above ordinary content; the bar must still sit above it."""
    driver, page = _plain
    page.set_content(
        '<!doctype html><body style="margin:0;background:#ffffff">'
        '<dialog id=d style="padding:0;border:0;margin:0;position:fixed;left:20px;top:20px;'
        'width:150px;height:60px;background:#ff0000">'
        '<h2 style="margin:0;width:150px;height:60px;background:#ff0000">Modal</h2></dialog>'
        '<div popover id=p style="margin:0;padding:0;border:0;position:fixed;left:200px;top:20px;'
        'width:150px;height:60px;background:#ff0000">'
        '<h3 style="margin:0;width:150px;height:60px;background:#ff0000">Popover</h3></div>'
        "<script>document.getElementById('d').showModal();"
        "document.getElementById('p').showPopover();</script>"
    )
    unmasked = _image(driver.screenshot_png())
    assert unmasked.getpixel((50, 50)) == _RED and unmasked.getpixel((250, 50)) == _RED

    covered = _image(driver.screenshot_png(masks=[("heading", "Modal"), ("heading", "Popover")]))

    assert covered.getpixel((50, 50)) == _BLACK  # the modal dialog's heading
    assert covered.getpixel((250, 50)) == _BLACK  # the popover's heading


def test_an_aria_hidden_heading_that_is_still_painted_is_covered_by_a_role_mask(
    _plain: Any,
) -> None:
    driver, page = _plain
    page.set_content(
        '<!doctype html><body style="margin:0;background:#ffffff">'
        '<h1 style="margin:0;position:absolute;left:10px;top:10px;width:150px;height:30px;'
        'background:#ff0000">Public</h1>'
        '<h1 aria-hidden="true" style="margin:0;position:absolute;left:10px;top:60px;'
        'width:150px;height:30px;background:#ff0000">Ada Lovelace</h1>'
    )
    public, hidden = (10, 10, 160, 40), (10, 60, 160, 90)
    unmasked = _image(driver.screenshot_png())
    assert _count(unmasked, _RED, hidden) > 0  # the hidden-from-ARIA heading is painted

    by_role = _image(driver.screenshot_png(masks=[("heading", None)]))
    by_name = _image(driver.screenshot_png(masks=[("heading", "Ada Lovelace")]))

    assert _count(by_role, _RED, public) == 0 and _count(by_role, _RED, hidden) == 0
    assert _count(by_name, _RED, hidden) == 0  # found by its name though ARIA hides it
