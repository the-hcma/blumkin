"""An upload by the whole label reaches the one input named so (BLUMKIN_BROWSER_TESTS=1, #704)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BLUMKIN_BROWSER_TESTS") != "1", reason="needs BLUMKIN_BROWSER_TESTS=1"
)

# The shape the live Manage Attachments dialog has (checked on 2026-11-10 with `browser inspect`):
# `File Name` matches four elements by substring, and only the file input is named exactly so.
_DIALOG = """
<input aria-label="File Name or URL" style="display:none">
<input aria-label="File Name" type="file" id="real">
<input aria-label="File Name or URL" style="display:none">
<textarea aria-label="File Name or URL" style="display:none"></textarea>
<input aria-label="Memo" id="memo-box">
<input aria-label="Memo for payroll" type="file" id="payroll">
"""


def _driver(playwright: Any) -> tuple[Any, Any, Any]:
    from blumkin.browser.cdp import PlaywrightDriver
    from blumkin.browser.discovery import resolve_browser

    browser = playwright.chromium.launch(
        executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
    )
    page = browser.new_page()
    page.set_content(_DIALOG)
    return browser, page, PlaywrightDriver(browser=browser, page=page, playwright=None, timeout_s=2)


def _files(page: Any, element: str) -> list[str]:
    script = f"Array.from(document.getElementById('{element}').files).map((f) => f.name)"
    return list(page.evaluate(script))


def test_the_whole_label_reaches_the_one_file_input_among_its_substring_matches(
    tmp_path: Path,
) -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.cdp import AmbiguousFieldError, FieldTarget

    receipt = tmp_path / "Taxi.pdf"
    receipt.write_bytes(b"x")
    with sync_playwright() as playwright:
        browser, page, driver = _driver(playwright)
        try:
            with pytest.raises(AmbiguousFieldError):  # by substring: four matches, as before
                driver.upload("File Name", receipt)

            driver.upload("File Name", receipt, FieldTarget(exact=True))

            assert _files(page, "real") == ["Taxi.pdf"]
        finally:
            browser.close()


def test_nth_still_counts_every_substring_match_so_a_saved_flow_keeps_working(
    tmp_path: Path,
) -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.cdp import FieldTarget

    receipt = tmp_path / "Taxi.pdf"
    receipt.write_bytes(b"x")
    with sync_playwright() as playwright:
        browser, page, driver = _driver(playwright)
        try:
            driver.upload("File Name", receipt, FieldTarget(nth=2))  # the second match, as before

            assert _files(page, "real") == ["Taxi.pdf"]
        finally:
            browser.close()


def test_a_whole_name_never_reaches_an_input_whose_name_only_contains_it(tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.cdp import BrowserActionError, FieldTarget

    receipt = tmp_path / "Taxi.pdf"
    receipt.write_bytes(b"x")
    with sync_playwright() as playwright:
        browser, page, driver = _driver(playwright)
        try:
            # `Memo` names a plain text box; the file input `Memo for payroll` only contains it.
            with pytest.raises(BrowserActionError, match="upload to the field labelled 'Memo'"):
                driver.upload("Memo", receipt, FieldTarget(exact=True))

            assert _files(page, "payroll") == []  # nothing was attached to the unrelated input
        finally:
            browser.close()
