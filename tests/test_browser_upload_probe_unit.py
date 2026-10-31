"""The upload and match probes run in a real Chrome (opt-in: BLUMKIN_BROWSER_TESTS=1, #607)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from blumkin.browser.cdp import (
    _MATCH_INFO_JS,
    _UPLOAD_COUNT_JS,
    _UPLOAD_INFO_JS,
    _UPLOAD_STATE_JS,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("BLUMKIN_BROWSER_TESTS") != "1", reason="needs BLUMKIN_BROWSER_TESTS=1"
)

_PAGE = """
<form>
<table>
<tr><td>Old row: receipt.pdf</td></tr>
<tr><td><label>File <input id=a type=file
  onchange="this.closest('tr').querySelector('span').textContent = this.files[0].name">
  </label><span></span></td></tr>
<tr><td><label>Drop <input id=b type=file style="opacity:0;position:absolute"></label></td></tr>
<tr><td><label>Gone <input id=c type=file></label></td></tr>
</table>
</form>
<div><label>Type <input id=t1></label><label style="display:none">Type <input id=t2></label>
<label>Type <input id=t3 style="display:none"></label></div>
"""


def _state(page: object, selector: str, path: Path) -> tuple[bool, dict[str, object], object]:
    handle = page.locator(selector).element_handle()  # type: ignore[attr-defined]
    hidden = bool(handle.evaluate(_UPLOAD_INFO_JS))
    before = handle.evaluate(_UPLOAD_COUNT_JS, path.name)
    page.locator(selector).set_input_files(str(path))  # type: ignore[attr-defined]
    page.wait_for_timeout(200)  # type: ignore[attr-defined]
    return hidden, handle.evaluate(_UPLOAD_STATE_JS, path.name), before


def test_the_probes_tell_shown_from_unknown_from_missing_in_a_real_page(tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.discovery import resolve_browser

    receipt = tmp_path / "Receipt.pdf"
    receipt.write_bytes(b"x")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.set_content(_PAGE)

            hidden, state, before = _state(page, "#a", receipt)
            # The old row already says "receipt.pdf" (in another case); only this row's count
            # is compared, and it rose from 0 to 1.
            assert hidden is False
            assert (before["near"], state["near"]) == (0, 1)  # type: ignore[index]
            assert state["files"] == ["Receipt.pdf"]  # type: ignore[index]

            hidden, state, before = _state(page, "#b", receipt)
            assert hidden is True  # opacity 0
            assert state["near"] == before["near"]  # type: ignore[index]  # nothing shown

            handle = page.locator("#c").element_handle()
            page.evaluate("document.getElementById('c').closest('tr').remove()")
            gone = handle.evaluate(_UPLOAD_STATE_JS, receipt.name)
            assert gone["connected"] is False and gone["files"] == []

            base = page.get_by_label("Type")
            assert (base.count(), base.filter(visible=True).count()) == (3, 1)
            visibility = [
                base.nth(index).element_handle().evaluate(_MATCH_INFO_JS)["visible"]
                for index in range(3)
            ]
            assert visibility == [True, False, False]
        finally:
            browser.close()
