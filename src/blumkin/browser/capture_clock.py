"""One time axis for everything a recording writes (#821).

A recording's frames carry the browser's paint times, and the recorder writes its cards and its
end marker from a steady clock that starts at the first entry (a picture, or a card when the page
cannot be recorded). Captions and narration cues are written by other processes (`browser click`,
`browser narrate`), and used to stamp the wall clock, which can step during a recording (a time
sync, a manual change): a step then put a cue past the end of the video or a caption in the wrong
second.

The recorder writes the anchor once, when its first entry is logged: that entry's time on the
journal's axis and the steady clock at that moment. Every other writer reads it and stamps
`anchor time + steady time since the anchor`. The steady clock is shared by every process on one
boot, so the stamps agree. Before the anchor exists (a recording that has only just started), or
when it cannot be used, they fall back to the wall clock, as before.

The steady clock is `time.monotonic`: it does not step, and on macOS it does not count the time
the machine spends asleep, so a cue added right after a resume is placed as if the sleep had not
happened (the wall clock would place it after it).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

CLOCK_NAME = "clock.json"


def capture_now(
    session_dir: Path,
    *,
    wall: Callable[[], float] | None = None,
    steady: Callable[[], float] | None = None,
) -> float:
    """Now on the recording's axis: the anchor's time plus steady time since it, else wall time."""
    wall = wall or time.time  # looked up now, so a stepped clock in a test is seen
    steady = steady or time.monotonic
    anchor = read_anchor(session_dir)
    if anchor is None:
        return wall()
    began, at = anchor
    elapsed = steady() - at
    if elapsed < 0:  # another boot: the steady clock restarted, so the anchor says nothing
        return wall()
    return began + elapsed


def read_anchor(session_dir: Path) -> tuple[float, float] | None:
    """The recorder's anchor `(journal time, steady clock)`, or None when absent or damaged."""
    try:
        data = json.loads((session_dir / CLOCK_NAME).read_text())
        return float(data["t"]), float(data["steady"])
    except OSError, ValueError, KeyError, TypeError:
        return None


def write_anchor(session_dir: Path, journal_time: float, steady: float) -> None:
    """Write the anchor (owner-only), whole or not at all: a reader never sees half of it."""
    target = session_dir / CLOCK_NAME
    scratch = session_dir / f".{CLOCK_NAME}.tmp"
    descriptor = os.open(scratch, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps({"steady": steady, "t": journal_time}))
        os.replace(scratch, target)
    except BaseException:
        with contextlib.suppress(OSError):
            scratch.unlink(missing_ok=True)
        raise
