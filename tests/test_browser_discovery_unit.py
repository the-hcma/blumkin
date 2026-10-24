"""Browser discovery: Chrome, Edge and Chromium with a Chrome fallback (issue #465)."""

from __future__ import annotations

from pathlib import Path

import pytest

from blumkin.browser.discovery import (
    BrowserConfigError,
    BrowserKind,
    BrowserNotFoundError,
    find_browser,
    parse_browser_kind,
    resolve_browser,
)

_MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_MAC_EDGE = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"


def _installed(*paths: str):
    present = {Path(p) for p in paths}
    return lambda candidate: candidate in present


def _which_none(name: str) -> str | None:
    return None


def _which_for(**found: str):
    return lambda name: found.get(name)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("chrome", BrowserKind.CHROME),
        ("Edge", BrowserKind.EDGE),
        (" chromium ", BrowserKind.CHROMIUM),
    ],
)
def test_parse_browser_kind_accepts_the_three_names_case_insensitively(
    text: str, kind: BrowserKind
) -> None:
    assert parse_browser_kind(text) is kind


def test_parse_browser_kind_rejects_an_unknown_name_with_the_accepted_set() -> None:
    with pytest.raises(BrowserConfigError) as excinfo:
        parse_browser_kind("firefox")
    message = str(excinfo.value)
    assert "chrome, chromium, edge" in message
    assert "'firefox'" in message


def test_find_browser_uses_the_mac_application_bundles() -> None:
    path = find_browser(
        BrowserKind.EDGE,
        platform="darwin",
        which=_which_none,
        is_executable=_installed(_MAC_EDGE),
    )
    assert path == Path(_MAC_EDGE)


def test_find_browser_uses_path_lookup_on_linux() -> None:
    path = find_browser(
        BrowserKind.CHROMIUM,
        platform="linux",
        which=_which_for(**{"chromium-browser": "/usr/bin/chromium-browser"}),
        is_executable=_installed("/usr/bin/chromium-browser"),
    )
    assert path == Path("/usr/bin/chromium-browser")


def test_find_browser_returns_none_when_nothing_is_installed() -> None:
    assert (
        find_browser(
            BrowserKind.CHROME,
            platform="linux",
            which=_which_none,
            is_executable=_installed(),
        )
        is None
    )


def test_resolve_browser_prefers_the_configured_browser_when_installed() -> None:
    resolved = resolve_browser(
        "edge",
        platform="darwin",
        which=_which_none,
        is_executable=_installed(_MAC_CHROME, _MAC_EDGE),
    )
    assert resolved.kind is BrowserKind.EDGE
    assert resolved.path == Path(_MAC_EDGE)
    assert resolved.fallback_from is None


def test_resolve_browser_falls_back_to_chrome_when_the_configured_one_is_missing() -> None:
    resolved = resolve_browser(
        "edge",
        platform="darwin",
        which=_which_none,
        is_executable=_installed(_MAC_CHROME),
    )
    assert resolved.kind is BrowserKind.CHROME
    assert resolved.fallback_from is BrowserKind.EDGE


def test_resolve_browser_uses_chrome_when_nothing_is_configured() -> None:
    resolved = resolve_browser(
        None,
        platform="darwin",
        which=_which_none,
        is_executable=_installed(_MAC_CHROME, _MAC_EDGE),
    )
    assert resolved.kind is BrowserKind.CHROME
    assert resolved.fallback_from is None


def test_resolve_browser_fails_clearly_when_chrome_is_missing_too() -> None:
    with pytest.raises(BrowserNotFoundError) as excinfo:
        resolve_browser(
            "edge",
            platform="linux",
            which=_which_none,
            is_executable=_installed(),
        )
    message = str(excinfo.value)
    assert "Chrome" in message
    assert "edge" in message


def test_resolve_browser_rejects_an_unknown_configured_name() -> None:
    with pytest.raises(BrowserConfigError):
        resolve_browser(
            "firefox",
            platform="linux",
            which=_which_none,
            is_executable=_installed(),
        )
