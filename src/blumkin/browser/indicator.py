"""Make the browser window blumkin drives recognizable (issue #590).

The window a task drives looks like any other Chrome window. Each task gets its own colour
(derived from the task name, so it is the same every time and needs nothing stored), and while
blumkin is attached the page shows a small non-interactive pill, `blumkin - TASK`, in that
colour, with a thin line of it across the top edge. The pill lives in a closed shadow root on a
`aria-hidden` host: it takes no clicks (`pointer-events: none`), adds nothing to the page text
`browser read` returns and nothing to the ARIA snapshot, and is set through CSSOM, which a
page's content security policy does not block. It stays until the page navigates; every command
puts it back, and closing the browser closes the window with it.
"""

from __future__ import annotations

import colorsys
import hashlib
from collections.abc import Sequence
from typing import Any

#: The element id of the pill's host, so it is only ever added once to a page.
INDICATOR_ID = "blumkin-controlled"

INDICATOR_JS = (
    "(args) => { const id = args.id; if (document.getElementById(id)) return; "
    "if (location.protocol !== 'https:' && location.protocol !== 'http:') return; "
    "const h = location.hostname.toLowerCase().replace(/\\.+$/, ''); "
    "const match = (p) => p.startsWith('*.') ? (h.endsWith(p.slice(1)) && h.length > p.length - 1)"
    " : h === p; "
    "if (args.blocked.some(match)) return; "
    "if (args.hosts.length && !args.hosts.some(match)) return; "
    "const host = document.createElement('div'); host.id = id; "
    "host.setAttribute('aria-hidden', 'true'); "
    "host.style.cssText = 'all: initial; position: fixed; inset: 0; z-index: 2147483647; "
    "pointer-events: none;'; "
    "const root = host.attachShadow({mode: 'closed'}); "
    "const line = document.createElement('div'); "
    "line.style.cssText = 'position: fixed; top: 0; left: 0; right: 0; height: 3px; "
    "background: ' + args.colour + '; pointer-events: none;'; "
    "const pill = document.createElement('div'); pill.textContent = 'blumkin - ' + args.task; "
    "pill.style.cssText = 'position: fixed; bottom: 6px; left: 6px; padding: 2px 8px; "
    "border-radius: 10px; background: ' + args.colour + '; color: #fff; "
    "font: 11px/16px system-ui, sans-serif; pointer-events: none;'; "
    "root.appendChild(line); root.appendChild(pill); "
    "(document.documentElement || document.body).appendChild(host); }"
)


def indicator_args(
    task: str, hosts: Sequence[str] = (), blocked: Sequence[str] = ()
) -> dict[str, Any]:
    """The arguments `INDICATOR_JS` takes for `task`.

    The script checks the page it runs on, in the same evaluation that adds the pill: it adds
    nothing off the `hosts` (any http(s) page when none are given) and never on a `blocked`
    host such as an identity provider, so a redirect that lands between a check in Python and
    the evaluation cannot get the task's name onto a page it should not be on.
    """
    return {
        "blocked": list(blocked),
        "colour": task_colour(task),
        "hosts": list(hosts),
        "id": INDICATOR_ID,
        "task": task,
    }


def contrast_with_white(colour: str) -> float:
    """The WCAG contrast ratio of white text on `colour` (`#rrggbb`)."""

    def linear(channel: int) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
    luminance = 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)
    return 1.05 / (luminance + 0.05)


def task_colour(task: str) -> str:
    """A colour (`#rrggbb`) the same for a task name every time, dark enough for white text.

    The lightness is lowered until white text on it reaches the 4.5:1 contrast of WCAG AA.
    """
    hue = int.from_bytes(hashlib.sha256(task.encode("utf-8")).digest()[:2], "big") % 360
    lightness = 0.38
    while True:
        red, green, blue = colorsys.hls_to_rgb(hue / 360, lightness, 0.7)
        colour = f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"
        if contrast_with_white(colour) >= 4.5 or lightness <= 0.1:
            return colour
        lightness -= 0.01
