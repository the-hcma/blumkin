"""Covering what must not be shown in a screenshot (#806): what the driver hands the browser.

The browser draws the bars itself, in the same render as the pixels (checked against a real
browser in `test_browser_mask_real_browser.py`); these tests check which elements the driver asks
it to cover, and when it refuses instead.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from blumkin.browser.cdp import BrowserActionError, PlaywrightDriver


def _png(width: int = 200, height: int = 100) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


class _Element:
    def __init__(self, box: dict[str, float]) -> None:
        self._box = box

    def bounding_box(self) -> dict[str, float]:
        return self._box


class _Locator:
    def __init__(self, role: str, name: str | None, matches: int | list[int]) -> None:
        self.role = role
        self.name = name
        self._matches = matches  # a list: successive answers, the last one repeats
        self.first = _Element({"x": 40.0, "y": 20.0, "width": 100.0, "height": 60.0})

    def count(self) -> int:
        if isinstance(self._matches, list):
            return self._matches.pop(0) if len(self._matches) > 1 else self._matches[0]
        return self._matches


class _Page:
    def __init__(self) -> None:
        self.shots: list[dict[str, Any]] = []
        self.size = (200, 100)
        #: (role, name) -> how many elements the page holds for it.
        self.matches: dict[tuple[str, str | None], int | list[int]] = {}
        self.role_calls: list[tuple[str, str | None, bool]] = []
        self.hidden_included: list[bool] = []
        self.context = SimpleNamespace()

    def get_by_role(
        self,
        role: str,
        name: str | None = None,
        exact: bool = False,
        include_hidden: bool = False,
    ) -> _Locator:
        self.role_calls.append((role, name, exact))
        self.hidden_included.append(include_hidden)
        return _Locator(role, name, self.matches.get((role, name), 0))

    def screenshot(self, **kwargs: Any) -> bytes:
        self.shots.append(kwargs)
        return _png(*self.size)

    def evaluate(self, script: str, arg: Any = None) -> Any:
        if script == "window.devicePixelRatio":
            return 1.0
        if script == "[window.innerWidth, window.innerHeight]":
            return list(self.size)
        raise AssertionError(f"unexpected script {script!r}")


def _driver(page: _Page) -> PlaywrightDriver:
    return PlaywrightDriver(
        browser=SimpleNamespace(), page=page, playwright=SimpleNamespace(), timeout_s=1
    )


def test_every_mask_is_handed_to_the_browser_as_a_locator_to_cover_in_black() -> None:
    page = _Page()
    page.matches = {("heading", None): 2, ("cell", None): 5}

    _driver(page).screenshot_png(masks=[("heading", None), ("cell", None)])

    (shot,) = page.shots
    assert [(m.role, m.name) for m in shot["mask"]] == [("heading", None), ("cell", None)]
    assert shot["mask_color"] == "#000000"


def test_a_mask_with_a_name_asks_for_that_exact_name() -> None:
    page = _Page()
    page.matches = {("heading", "Ada Lovelace"): 1}

    _driver(page).screenshot_png(masks=[("heading", "Ada Lovelace")])

    assert page.role_calls == [("heading", "Ada Lovelace", True)]


def test_a_mask_that_matches_nothing_refuses_the_capture_before_any_picture_is_taken() -> None:
    page = _Page()
    page.matches = {("heading", None): 1}  # the second mask has nothing to cover

    with pytest.raises(BrowserActionError, match="to mask"):
        _driver(page).screenshot_png(masks=[("heading", None), ("cell", "Nobody")])

    assert page.shots == []


def test_the_refusal_does_not_name_the_page_only_what_the_caller_asked_for() -> None:
    page = _Page()

    with pytest.raises(BrowserActionError) as excinfo:
        _driver(page).screenshot_png(masks=[("cell", "Total")])

    assert "cell:Total" in str(excinfo.value)


def test_without_masks_the_picture_is_taken_without_a_mask_option_and_no_element_is_looked_up() -> (
    None
):
    page = _Page()

    data = _driver(page).screenshot_png()

    assert data == _png()
    assert "mask" not in page.shots[0] and "mask_color" not in page.shots[0]
    assert page.role_calls == []


def test_masks_apply_to_a_full_page_picture_too() -> None:
    page = _Page()
    page.matches = {("cell", None): 300}  # as many as the page holds: no limit to leak past

    _driver(page).screenshot_png(full_page=True, masks=[("cell", None)])

    assert page.shots[0]["full_page"] is True and len(page.shots[0]["mask"]) == 1


def test_a_captured_element_is_cut_from_a_viewport_picture_that_carries_the_masks() -> None:
    page = _Page()
    page.matches = {("dialog", None): 1, ("heading", None): 1}
    # `_require_in_view` reads the box of the first dialog; the viewport picture is then masked.

    data = _driver(page).screenshot_png(role="dialog", masks=[("heading", None)])

    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (100, 60)  # the dialog's own size, cut from the viewport picture
    assert [(m.role, m.name) for m in page.shots[0]["mask"]] == [("heading", None)]
    assert page.shots[0]["full_page"] is False


def test_a_mask_that_stopped_matching_while_the_picture_was_taken_is_not_kept() -> None:
    page = _Page()
    page.matches = {("heading", "Ada Lovelace"): [1, 0]}  # its name changed during the capture

    with pytest.raises(BrowserActionError, match="count changed"):
        _driver(page).screenshot_png(masks=[("heading", "Ada Lovelace")])

    assert len(page.shots) == 1  # the picture was taken, and thrown away


def test_a_mask_that_matches_more_after_the_picture_is_refused_too() -> None:
    page = _Page()
    page.matches = {("cell", None): [3, 5]}

    with pytest.raises(BrowserActionError, match="count changed"):
        _driver(page).screenshot_png(masks=[("cell", None)])


def test_masks_that_match_the_same_before_and_after_are_kept() -> None:
    page = _Page()
    page.matches = {("cell", None): [4, 4]}

    assert _driver(page).screenshot_png(masks=[("cell", None)]) == _png()


def test_only_a_change_in_how_many_elements_match_is_caught_not_a_swap() -> None:
    """The check counts: a swap of one matching element for another leaves the count as it was."""
    page = _Page()
    page.matches = {("heading", "Ada Lovelace"): [1, 1]}  # a different element, the same count

    assert _driver(page).screenshot_png(masks=[("heading", "Ada Lovelace")]) == _png()


def test_masks_look_for_elements_marked_aria_hidden_too_because_they_can_still_be_painted() -> None:
    page = _Page()
    page.matches = {("heading", None): 1, ("cell", "Total"): 1}

    _driver(page).screenshot_png(masks=[("heading", None), ("cell", "Total")])

    assert page.hidden_included == [True, True]  # both the role-only and the named lookup
