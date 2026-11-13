"""A key press retries with a fresh, vetted handle when the page re-rendered the field (#741)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError

from blumkin.browser.cdp import BrowserActionError, FieldTarget, PlaywrightDriver

_DETACHED = "ElementHandle.press: Element is not attached to the DOM"


class _Handle:
    """An element handle whose `press` fails the first `fail` times, then records the key."""

    def __init__(self, name: str, fail: int, message: str, log: list[str]) -> None:
        self.name, self.fail, self.message, self.log = name, fail, message, log

    def press(self, key: str, timeout: int) -> None:
        if self.fail:
            self.fail -= 1
            raise PlaywrightError(self.message)
        self.log.append(f"{self.name}:{key}")


def _driver(handles: list[_Handle]) -> PlaywrightDriver:
    """A driver whose field lookup hands out `handles` one after another."""
    driver = PlaywrightDriver.__new__(PlaywrightDriver)
    queue = iter(handles)
    locator = SimpleNamespace(element_handle=lambda timeout: next(queue))
    driver._timeout_ms = 1000  # type: ignore[attr-defined]
    driver._timeout_s = 1  # type: ignore[attr-defined]
    driver._require_open = lambda: None  # type: ignore[method-assign]
    driver._scope = lambda target: object()  # type: ignore[method-assign]
    driver._field = lambda scope, label, target, prefer_visible: locator  # type: ignore[method-assign]
    driver._describe = lambda handle: handle.name  # type: ignore[method-assign]
    driver._still_allowed = lambda scope, target: None  # type: ignore[method-assign]
    return driver


def _press(driver: PlaywrightDriver, vetted: list[Any]) -> None:
    driver.press_field("Task Number", "ArrowDown", FieldTarget(), vetted.append)


def test_a_detached_handle_is_replaced_and_the_fresh_one_is_vetted_and_pressed() -> None:
    log: list[str] = []
    stale = _Handle("stale", 1, _DETACHED, log)
    fresh = _Handle("fresh", 0, "", log)
    vetted: list[Any] = []

    _press(_driver([stale, fresh]), vetted)

    assert log == ["fresh:ArrowDown"]  # the key went to the new element only
    assert vetted == ["stale", "fresh"]  # and each element was vetted before its press


def test_a_handle_that_keeps_detaching_fails_after_the_bounded_retries() -> None:
    log: list[str] = []
    handles = [_Handle(f"h{i}", 1, _DETACHED, log) for i in range(5)]
    vetted: list[Any] = []

    with pytest.raises(BrowserActionError, match="not attached to the DOM"):
        _press(_driver(handles), vetted)

    assert log == [] and vetted == ["h0", "h1", "h2"]  # three attempts, then it gives up


def test_any_other_press_error_is_not_retried() -> None:
    log: list[str] = []
    handles = [_Handle("a", 1, "ElementHandle.press: Timeout 30000ms exceeded", log)]
    vetted: list[Any] = []

    with pytest.raises(BrowserActionError, match="Timeout 30000ms"):
        _press(_driver(handles + [_Handle("b", 0, "", log)]), vetted)

    assert vetted == ["a"] and log == []


def test_a_refused_fresh_handle_is_never_pressed() -> None:
    log: list[str] = []
    driver = _driver([_Handle("stale", 1, _DETACHED, log), _Handle("fresh", 0, "", log)])

    def vet(info: Any) -> None:
        if info == "fresh":
            raise BrowserActionError("refused: the control looks like a submit")

    with pytest.raises(BrowserActionError, match="refused"):
        driver.press_field("Task Number", "ArrowDown", FieldTarget(), vet)

    assert log == []  # the vet applies to the element that would get the key
