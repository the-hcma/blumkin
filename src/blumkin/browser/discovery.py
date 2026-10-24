"""Find the user's Chrome, Edge or Chromium (issue #465).

All three are Chromium-based, so the same DevTools Protocol code drives them. The
browser a task launches comes from the blumkin config (`[browser] default`); when that
is unset, or names a browser that is not installed, Chrome is used, and a clear error
says so when Chrome is missing too. blumkin never downloads a browser.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class BrowserConfigError(ValueError):
    """The configured browser name is not one blumkin supports."""


class BrowserKind(StrEnum):
    CHROME = "chrome"
    CHROMIUM = "chromium"
    EDGE = "edge"


class BrowserNotFoundError(RuntimeError):
    """Neither the configured browser nor Chrome is installed."""


DEFAULT_BROWSER = BrowserKind.CHROME


@dataclass(frozen=True, slots=True)
class ResolvedBrowser:
    kind: BrowserKind
    path: Path
    #: Set when the configured browser was missing and Chrome was used instead.
    fallback_from: BrowserKind | None = None


def find_browser(
    kind: BrowserKind,
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    is_executable: Callable[[Path], bool] | None = None,
) -> Path | None:
    """Return the installed executable for `kind`, or None when it is not installed."""
    check = is_executable or _is_executable
    current = sys.platform if platform is None else platform
    if current == "darwin":
        for candidate in _MAC_PATHS[kind]:
            if check(candidate):
                return candidate
        return None
    for name in _LINUX_NAMES[kind]:
        found = which(name)
        if found and check(Path(found)):
            return Path(found)
    return None


def parse_browser_kind(value: str) -> BrowserKind:
    """Parse a configured browser name, case-insensitively."""
    text = value.strip().lower()
    for kind in BrowserKind:
        if kind.value == text:
            return kind
    accepted = ", ".join(sorted(kind.value for kind in BrowserKind))
    raise BrowserConfigError(f"Expected a browser name ({accepted}), got {value!r}")


def resolve_browser(
    preferred: str | None,
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    is_executable: Callable[[Path], bool] | None = None,
) -> ResolvedBrowser:
    """Pick the browser to launch: the configured one, else Chrome."""
    wanted = parse_browser_kind(preferred) if preferred else DEFAULT_BROWSER
    found = find_browser(wanted, platform=platform, which=which, is_executable=is_executable)
    if found is not None:
        return ResolvedBrowser(kind=wanted, path=found)
    if wanted is not DEFAULT_BROWSER:
        fallback = find_browser(
            DEFAULT_BROWSER, platform=platform, which=which, is_executable=is_executable
        )
        if fallback is not None:
            return ResolvedBrowser(kind=DEFAULT_BROWSER, path=fallback, fallback_from=wanted)
        raise BrowserNotFoundError(
            f"Expected {wanted.value} or Chrome to be installed, found neither; "
            "install Chrome or set [browser] default to an installed browser"
        )
    raise BrowserNotFoundError(
        "Expected Chrome to be installed, found none; "
        "install Chrome or set [browser] default to an installed browser"
    )


_LINUX_NAMES: dict[BrowserKind, tuple[str, ...]] = {
    BrowserKind.CHROME: ("google-chrome", "google-chrome-stable"),
    BrowserKind.CHROMIUM: ("chromium", "chromium-browser"),
    BrowserKind.EDGE: ("microsoft-edge", "microsoft-edge-stable"),
}


_MAC_PATHS: dict[BrowserKind, tuple[Path, ...]] = {
    BrowserKind.CHROME: (
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ),
    BrowserKind.CHROMIUM: (
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        Path.home() / "Applications/Chromium.app/Contents/MacOS/Chromium",
    ),
    BrowserKind.EDGE: (
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ),
}


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)
