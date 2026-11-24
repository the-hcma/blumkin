"""The leaked-driver cleanup and the objects a test module lends to its drivers (#824)."""

from __future__ import annotations

from typing import Any

from browser_leaks import borrowed, closing_leaked_drivers

from blumkin.browser import cdp


class _Closable:
    def __init__(self) -> None:
        self.closed = 0
        self.stopped = 0
        self.contexts = ["one"]

    def close(self) -> None:
        self.closed += 1

    def stop(self) -> None:
        self.stopped += 1


def _driver(browser: Any, playwright: Any) -> cdp.PlaywrightDriver:
    return cdp.PlaywrightDriver(browser=browser, page=_Page(), playwright=playwright, timeout_s=1.0)


class _Page:
    context = _Closable()

    def __getattr__(self, name: str) -> Any:
        return lambda *a, **k: None


def test_a_lent_object_is_passed_through_but_never_closed_or_stopped() -> None:
    owner = _Closable()
    lent = borrowed(owner)

    lent.close()
    lent.stop()

    assert (owner.closed, owner.stopped) == (0, 0)
    assert lent.contexts == ["one"]  # everything else reaches the owner


def test_the_cleanup_closes_a_driver_that_owns_its_browser_and_leaves_a_lent_one_alone() -> None:
    owned_browser, owned_session = _Closable(), _Closable()
    shared_browser, shared_session = _Closable(), _Closable()

    with closing_leaked_drivers():
        _driver(owned_browser, owned_session)
        _driver(borrowed(shared_browser), borrowed(shared_session))

    assert (owned_browser.closed, owned_session.stopped) == (1, 1)
    assert (shared_browser.closed, shared_session.stopped) == (0, 0)
