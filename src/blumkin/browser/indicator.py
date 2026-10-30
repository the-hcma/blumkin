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

#: The element id of the pill's host, so it is only ever added once to a page.
INDICATOR_ID = "blumkin-controlled"

INDICATOR_JS = (
    "(args) => { const id = args.id; if (document.getElementById(id)) return; "
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
    "border-radius: 10px; background: ' + args.colour + '; color: #fff; opacity: 0.9; "
    "font: 11px/16px system-ui, sans-serif; pointer-events: none;'; "
    "root.appendChild(line); root.appendChild(pill); "
    "(document.documentElement || document.body).appendChild(host); }"
)


def indicator_args(task: str) -> dict[str, str]:
    """The arguments `INDICATOR_JS` takes for `task`."""
    return {"colour": task_colour(task), "id": INDICATOR_ID, "task": task}


def task_colour(task: str) -> str:
    """A dark, saturated colour (`#rrggbb`) that is the same for a task name every time."""
    hue = int.from_bytes(hashlib.sha256(task.encode("utf-8")).digest()[:2], "big") % 360
    red, green, blue = colorsys.hls_to_rgb(hue / 360, 0.38, 0.7)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"
