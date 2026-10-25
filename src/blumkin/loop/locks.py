"""Per-instance file locks so the same task instance never overlaps (issue #501)."""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path

from blumkin.browser.profiles import BrowserProfileError, ensure_private_dir, is_valid_task_name

_LOCK_DIRNAME = "locks"
_STORE_DIRNAME = "loop"


class LoopLockError(ValueError):
    """An instance lock path is not acceptable."""


@contextlib.contextmanager
def instance_lock(config_dir: Path, task: str, instance: str) -> Iterator[None]:
    """Hold an exclusive lock for `(task, instance)` for the duration of the block."""
    if not is_valid_task_name(task) or not is_valid_task_name(instance):
        raise LoopLockError(
            f"Expected safe task/instance names for a lock, got {task!r}/{instance!r}"
        )
    try:
        directory = ensure_private_dir(config_dir / _STORE_DIRNAME / _LOCK_DIRNAME)
    except BrowserProfileError as exc:
        raise LoopLockError(str(exc)) from exc
    path = directory / f"{task}--{instance}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
