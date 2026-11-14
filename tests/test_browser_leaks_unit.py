"""`closing_leaked_drivers` closes what a failed real-browser test leaves open (#738)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from browser_leaks import closing_leaked_drivers


class _Browser:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _Playwright:
    def __init__(self) -> None:
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1


def _driver(browser: Any, playwright: Any) -> Any:
    from blumkin.browser.cdp import PlaywrightDriver

    page = SimpleNamespace(context=SimpleNamespace(), on=lambda *a: None)
    return PlaywrightDriver(browser=browser, page=page, playwright=playwright, timeout_s=1)


def test_a_driver_left_open_is_closed_on_the_way_out() -> None:
    browser, playwright = _Browser(), _Playwright()
    with closing_leaked_drivers() as opened:
        driver = _driver(browser, playwright)
        assert opened == [driver]
        assert browser.closed == 0
    assert browser.closed == 1 and playwright.stopped == 1


def test_a_driver_the_test_closed_is_not_closed_again() -> None:
    browser, playwright = _Browser(), _Playwright()
    with closing_leaked_drivers():
        _driver(browser, playwright).close()
    assert browser.closed == 1 and playwright.stopped == 1


def test_the_drivers_are_closed_even_when_the_test_raises() -> None:
    browser, playwright = _Browser(), _Playwright()
    with pytest.raises(RuntimeError, match="boom"), closing_leaked_drivers():
        _driver(browser, playwright)
        raise RuntimeError("boom")
    assert browser.closed == 1 and playwright.stopped == 1


def test_one_driver_that_cannot_close_does_not_keep_the_others_open() -> None:
    class _Stuck(_Browser):
        def close(self) -> None:
            raise RuntimeError("connection lost")

    good_browser, good_playwright = _Browser(), _Playwright()
    stuck_playwright = _Playwright()
    with closing_leaked_drivers():
        _driver(good_browser, good_playwright)
        _driver(_Stuck(), stuck_playwright)
    assert good_browser.closed == 1 and good_playwright.stopped == 1
    # The driver whose browser would not close still stops its Playwright session.
    assert stuck_playwright.stopped == 1


def test_the_driver_class_is_restored_afterwards() -> None:
    from blumkin.browser.cdp import PlaywrightDriver

    before = PlaywrightDriver.__init__
    with closing_leaked_drivers():
        assert PlaywrightDriver.__init__ is not before
    assert PlaywrightDriver.__init__ is before
