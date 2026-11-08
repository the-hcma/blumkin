"""The guard's dialog scan: a long row of text does not hide a dialog's controls from it."""

from __future__ import annotations

import os

import pytest

from blumkin.browser.cdp import _DESCRIBE_CONTROL_JS

_ROW = "File Receipts Category 2026-11-07_probe-cab_1.00-USD_e144191e.pdf Probe Cab: test ride"
_PAGE = (
    '<form method="post"><div id="d" role="dialog">'
    + "".join(f'<div onclick="void 0">{_ROW} {n}</div>' for n in range(6))
    + '<button id="ok" type="button">OK</button>'
    + '<div onclick="void 0">'
    + "x" * 3000
    + "</div></div></form>"
)
_LONG_THEN_SUBMIT = (
    '<form method="post"><div role="dialog"><div onclick="void 0">'
    + "word " * 60
    + 'Submit report</div><button id="ok" type="button">OK</button></div></form>'
)


def _describe(html: str, landmarks: list[str]) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.discovery import resolve_browser

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(html)
            handle = page.locator("#ok").element_handle()
            return handle.evaluate(_DESCRIBE_CONTROL_JS, landmarks)  # type: ignore[no-any-return]
        finally:
            browser.close()


def _needs_a_browser() -> None:
    if os.environ.get("BLUMKIN_BROWSER_TESTS") != "1":
        pytest.skip("set BLUMKIN_BROWSER_TESTS=1 to run real-browser tests")


def test_a_row_of_text_over_two_hundred_characters_still_leaves_the_dialog_checkable() -> None:
    _needs_a_browser()
    raw = _describe(_LONG_THEN_SUBMIT, [])

    assert raw["dialogComplete"] is True
    # The tail of the long text was read, so a denied word late in it is still seen.
    assert any("Submit report" in str(text) for text in raw["dialogControls"])  # type: ignore[attr-defined]


def test_a_text_far_beyond_any_row_still_marks_the_dialog_unchecked() -> None:
    _needs_a_browser()

    assert _describe(_PAGE, [])["dialogComplete"] is False
