"""Global kill switch for unattended loop/cron runs (issue #501)."""

from __future__ import annotations

from pathlib import Path

from blumkin.loop.config import LoopConfig

DEFAULT_KILL_FILE_NAME = "KILL"


def is_engaged(config_dir: Path, loop: LoopConfig) -> bool:
    """True when config or the kill file says all runs must stop."""
    if loop.kill_switch:
        return True
    path = kill_switch_path(config_dir, loop)
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def kill_switch_path(config_dir: Path, loop: LoopConfig) -> Path:
    """Where the operator drops a file to stop every run."""
    if loop.kill_switch_file is not None:
        return loop.kill_switch_file
    return config_dir / "loop" / DEFAULT_KILL_FILE_NAME
