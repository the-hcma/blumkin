"""systemd user unit for `blumkin loop run` (issue #501)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

UNIT_NAME = "blumkin-loop.service"


class LoopServiceError(ValueError):
    """The systemd unit could not be installed or located."""


def install_user_unit(*, blumkin_path: str | None = None, unit_dir: Path | None = None) -> Path:
    """Write `blumkin-loop.service` under the user's systemd unit directory."""
    executable = blumkin_path or shutil.which("blumkin")
    if not executable:
        raise LoopServiceError(
            "Expected blumkin on PATH (or pass --blumkin-path) to install the loop service"
        )
    executable = os.path.abspath(executable)
    target_dir = unit_dir or _default_unit_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / UNIT_NAME
    path.write_text(unit_text(executable), encoding="utf-8")
    path.chmod(0o644)
    return path


def unit_text(blumkin_path: str) -> str:
    """Contents of the systemd user unit."""
    quoted = _quote_exec(blumkin_path)
    return (
        "[Unit]\n"
        "Description=blumkin loop and cron runner (unattended browser tasks)\n"
        "Documentation=https://github.com/the-hcma/blumkin/issues/499\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={quoted} loop run\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "# Unattended: never wait on a TTY prompt.\n"
        "Environment=BLUMKIN_NONINTERACTIVE=1\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _default_unit_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "systemd" / "user"


def _quote_exec(path: str) -> str:
    """Quote an ExecStart executable so spaces stay one argument."""
    if any(ch in path for ch in ' \t"\\'):
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return path
