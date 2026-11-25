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

from blumkin.browser.cdp import BrowserActionError, MaskError, PlaywrightDriver


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


# ------------------------------------------------------------------ masked recordings (#806)


class _Session:
    """A DevTools session: what the driver asks it to do, and the frames it is made to send."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sent.append((method, params or {}))
        return {"data": ""}

    def emit_frame(self, number: int, pixels: bytes = b"UNMASKED-PIXELS") -> None:
        import base64

        self.handlers["Page.screencastFrame"](
            {
                "data": base64.b64encode(pixels).decode(),
                "metadata": {"timestamp": 1000.0 + number},
                "sessionId": number,
            }
        )


def _jpeg(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (200, 100), color).save(buffer, format="JPEG")
    return buffer.getvalue()


class _CastPage(_Page):
    def __init__(self) -> None:
        super().__init__()
        self.session = _Session()
        self.context = SimpleNamespace(new_cdp_session=lambda page: self.session)
        self.main_frame = object()
        self.next_picture = _jpeg((10, 10, 10))

    def on(self, event: str, handler: Any) -> None:
        return None

    def remove_listener(self, event: str, handler: Any) -> None:
        return None

    def wait_for_timeout(self, ms: float) -> None:
        return None

    def screenshot(self, **kwargs: Any) -> bytes:
        self.shots.append(kwargs)
        return self.next_picture


def _masked_cast(page: _CastPage, masks: Any = (("heading", None),)) -> PlaywrightDriver:
    driver = _driver(page)
    driver.screencast_start(max_width=1280, quality=60, masks=masks)
    return driver


def test_a_masked_recording_never_returns_a_pixel_of_the_screencast() -> None:
    page = _CastPage()
    page.matches = {("heading", None): 1}
    driver = _masked_cast(page)
    page.session.emit_frame(1)
    page.session.emit_frame(2)

    frames = driver.screencast_pump(50)

    assert len(frames) == 1 and frames[0].data == page.next_picture  # the masked screenshot
    assert b"UNMASKED" not in frames[0].data
    (shot,) = page.shots
    assert shot["type"] == "jpeg" and shot["quality"] == 60
    assert [(m.role, m.name) for m in shot["mask"]] == [("heading", None)]
    acks = [params for method, params in page.session.sent if method == "Page.screencastFrameAck"]
    assert len(acks) == 2  # both screencast frames were acknowledged so the browser sends more


def test_a_masked_page_that_did_not_change_is_not_photographed() -> None:
    page = _CastPage()
    page.matches = {("heading", None): 1}
    driver = _masked_cast(page)

    assert driver.screencast_pump(50) == []
    assert page.shots == []  # no screencast frame, no change, no picture (and no flicker)


def test_the_same_masked_picture_twice_is_kept_once_and_a_changed_one_is_kept() -> None:
    page = _CastPage()
    page.matches = {("heading", None): 1}
    driver = _masked_cast(page)

    page.session.emit_frame(1)
    first = driver.screencast_pump(50)
    page.session.emit_frame(2)
    repeat = driver.screencast_pump(50)
    page.next_picture = _jpeg((200, 0, 0))
    page.session.emit_frame(3, pixels=b"A-CHANGED-PAGE")  # a real change paints different pixels
    changed = driver.screencast_pump(50)

    assert (len(first), len(repeat), len(changed)) == (1, 0, 1)
    assert changed[0].data == page.next_picture


def test_a_masked_keyframe_is_a_masked_screenshot_not_a_devtools_capture() -> None:
    page = _CastPage()
    page.matches = {("heading", None): 1}
    driver = _masked_cast(page)

    frame = driver.screencast_keyframe()

    assert frame.data == page.next_picture
    assert "Page.captureScreenshot" not in [method for method, _ in page.session.sent]
    assert [(m.role, m.name) for m in page.shots[0]["mask"]] == [("heading", None)]


def test_a_mask_that_matches_nothing_gives_no_frame_and_no_keyframe() -> None:
    page = _CastPage()  # the page holds no heading
    driver = _masked_cast(page)
    page.session.emit_frame(1)

    with pytest.raises(MaskError, match="heading"):
        driver.screencast_pump(50)
    with pytest.raises(MaskError, match="heading"):
        driver.screencast_keyframe()

    assert page.shots == []  # nothing was photographed


def test_without_masks_the_screencast_frames_are_returned_as_they_always_were() -> None:
    page = _CastPage()
    driver = _driver(page)
    driver.screencast_start(max_width=1280, quality=60)
    page.session.emit_frame(1)

    frames = driver.screencast_pump(50)

    assert [f.data for f in frames] == [b"UNMASKED-PIXELS"] and page.shots == []


def _after_each_shot_the_bars_repaint(page: _CastPage) -> None:
    """Drawing and removing the bars repaints the page: the browser sends frames for it.

    The frames show the page as it is then, so they differ from one state of the page to
    another (`page.state`), as real pixels do.
    """
    original = page.screenshot
    counter = [0]
    page.state = "A"  # type: ignore[attr-defined]

    def shoot(**kwargs: Any) -> bytes:
        taken = original(**kwargs)
        counter[0] += 1
        state = page.state  # type: ignore[attr-defined]
        page.session.emit_frame(100 + 2 * counter[0], pixels=f"{state}-WITH-BARS".encode())
        page.session.emit_frame(101 + 2 * counter[0], pixels=f"{state}-WITHOUT-BARS".encode())
        return taken

    page.screenshot = shoot  # type: ignore[method-assign]


def test_an_idle_masked_page_is_not_photographed_for_ever_by_its_own_bars() -> None:
    page = _CastPage()
    page.matches = {("heading", None): 1}
    _after_each_shot_the_bars_repaint(page)
    driver = _masked_cast(page)
    driver.screencast_keyframe()

    kept = sum(len(driver.screencast_pump(50)) for _ in range(12))

    assert kept == 0
    assert len(page.shots) <= 3  # the keyframe, one look to tell the bars from a change, no more


def test_a_real_change_is_kept_and_a_return_to_an_earlier_look_is_not_mistaken_for_bars() -> None:
    page = _CastPage()
    page.matches = {("heading", None): 1}
    _after_each_shot_the_bars_repaint(page)
    driver = _masked_cast(page)
    driver.screencast_keyframe()
    for _ in range(4):
        driver.screencast_pump(50)  # idle: the bars' frames are learned as noise

    page.next_picture = _jpeg((0, 120, 0))  # the page changes to B
    page.state = "B"  # type: ignore[attr-defined]
    page.session.emit_frame(1, pixels=b"B-PAINTED")
    to_b = driver.screencast_pump(50)
    for _ in range(4):
        driver.screencast_pump(50)
    page.next_picture = _jpeg((10, 10, 10))  # and back to how it was: A again
    page.state = "A"  # type: ignore[attr-defined]
    page.session.emit_frame(2, pixels=b"A-WITHOUT-BARS")  # the same pixels the bars once left
    back_to_a = driver.screencast_pump(50)

    assert len(to_b) == 1 and len(back_to_a) == 1


def test_a_page_that_never_stops_changing_is_photographed_on_every_read() -> None:
    page = _CastPage()
    page.matches = {("heading", None): 1}
    driver = _masked_cast(page)
    driver.screencast_keyframe()
    kept = 0
    for number in range(6):
        page.next_picture = _jpeg((number * 30, 0, 0))
        page.session.emit_frame(number, pixels=f"PAINT-{number}".encode())
        kept += len(driver.screencast_pump(50))

    assert kept == 6
