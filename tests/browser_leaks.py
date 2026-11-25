"""Close the browser drivers a real-browser test leaves open (#738).

A test that fails before it closes its driver leaves a Playwright sync session running, and the
next test then fails with "Playwright Sync API inside the asyncio loop": one failure showed up
as a dozen. `closing_leaked_drivers` records every `PlaywrightDriver` built inside it and calls
`close()` on each on the way out, newest first. A driver the test already closed is a no-op
(`close()` is idempotent), and one whose browser will not close still stops its Playwright
session (`close()` does that in a `finally`); an error from either is swallowed so that the
other drivers are still closed.

A driver closes the browser and the Playwright session it was given. A test module that shares
one browser and one session between its tests (a module-scoped fixture) hands its driver
`borrowed(...)` copies instead, so the cleanup after the first test does not close what the
next ones still use (#824).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any
from unittest import mock


class _Borrowed:
    """Another owner's object, lent out: everything is passed on except closing and stopping."""

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._owner, name)

    def close(self, *args: Any, **kwargs: Any) -> None:
        return None

    def stop(self, *args: Any, **kwargs: Any) -> None:
        return None


def borrowed(owner: Any) -> Any:
    """`owner` for a driver that must not close or stop it (its fixture does, once)."""
    return _Borrowed(owner)


@contextlib.contextmanager
def closing_leaked_drivers() -> Iterator[list[Any]]:
    from blumkin.browser import cdp

    opened: list[Any] = []
    original = cdp.PlaywrightDriver.__init__

    def tracked(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        opened.append(self)

    with mock.patch.object(cdp.PlaywrightDriver, "__init__", tracked):
        try:
            yield opened
        finally:
            for driver in reversed(opened):
                with contextlib.suppress(Exception):
                    driver.close()
