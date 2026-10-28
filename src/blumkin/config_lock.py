"""One advisory lock for every read-modify-write of `config.toml` (issue #545).

`config.toml` is edited by several commands (`auth setup`, the automatic email fill-in,
`browser init-task`). Each reads the file, changes it and writes it back, so two at once can
lose or corrupt an edit. They all take this lock for the whole read-to-write span; it is a
separate private file next to the config so it works whatever the editors of the config itself
do with the file (rewrite it in place, replace it).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path

LOCK_NAME = ".config.toml.lock"


@contextlib.contextmanager
def config_file_lock(config_path: Path) -> Iterator[None]:
    """Hold the exclusive lock for the config file at `config_path`; blocks until it is free."""
    fd = os.open(config_path.with_name(LOCK_NAME), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)
