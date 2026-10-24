"""Per-task browser profiles and the session records kept inside them (issue #465).

Each task gets its own browser profile directory under `<config dir>/browser-profiles/`,
created `0700` with a marker file so blumkin only ever attaches to, lists or deletes
profiles it created. A running browser is recorded in a `0600` session file next to the
profile data; cookies from the user's manual sign-in live only in that directory.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    task: str
    path: Path


class BrowserProfileError(ValueError):
    """A profile name or directory is not usable."""


@dataclass(frozen=True, slots=True)
class BrowserSession:
    browser: str
    pid: int
    port: int
    started_at: str

    @property
    def endpoint(self) -> str:
        return f"http://{DEBUG_HOST}:{self.port}"


DEBUG_HOST = "127.0.0.1"


MARKER_FILENAME = ".blumkin-profile.json"


class NotBlumkinProfileError(BrowserProfileError):
    """The directory is not a profile blumkin created."""


PROFILES_DIRNAME = "browser-profiles"


class ProfileInUseError(BrowserProfileError):
    """The profile's browser is still running."""


SESSION_FILENAME = ".blumkin-session.json"


def create_profile(config_dir: Path, task: str) -> BrowserProfile:
    """Create the profile for `task`, or return the existing one blumkin made."""
    _validate_task_name(task)
    root = _ensure_private_dir(config_dir / PROFILES_DIRNAME)
    path = root / task
    if path.is_symlink():
        raise BrowserProfileError(f"Expected a real directory at {path}, got a symlink")
    if path.exists():
        return require_blumkin_profile(path)
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    marker = {"created_at": _utc_now(), "task": task}
    _write_private(path / MARKER_FILENAME, json.dumps(marker, sort_keys=True))
    return BrowserProfile(task=task, path=path)


def delete_profile(
    config_dir: Path,
    task: str,
    *,
    pid_alive: Callable[[int], bool] | None = None,
) -> None:
    """Delete the profile for `task` (cookies included); refuse while its browser runs."""
    _validate_task_name(task)
    profile = load_profile(config_dir, task)
    if profile is None:
        raise BrowserProfileError(
            f"Expected an existing browser profile for task {task!r}, got none"
        )
    if read_session(profile.path, pid_alive=pid_alive) is not None:
        raise ProfileInUseError(
            f"Expected the browser for task {task!r} to be closed before deleting its profile, "
            "got a running browser"
        )
    shutil.rmtree(profile.path)


def list_profiles(config_dir: Path) -> list[BrowserProfile]:
    """Return the profiles blumkin created, sorted by task name."""
    root = config_dir / PROFILES_DIRNAME
    if not root.is_dir():
        return []
    found: list[BrowserProfile] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.is_symlink() and (child / MARKER_FILENAME).is_file():
            found.append(BrowserProfile(task=child.name, path=child))
    return found


def load_profile(config_dir: Path, task: str) -> BrowserProfile | None:
    """Return the profile for `task`, or None when there is none."""
    try:
        _validate_task_name(task)
    except BrowserProfileError:
        return None
    path = config_dir / PROFILES_DIRNAME / task
    if path.is_dir() and not path.is_symlink() and (path / MARKER_FILENAME).is_file():
        return BrowserProfile(task=task, path=path)
    return None


def process_is_browser_for(
    pid: int,
    profile_dir: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """True when `pid` is a live process started on `profile_dir`.

    A session file outlives its browser (a crash, a reboot), and the recorded pid may
    since belong to an unrelated process; so liveness alone is not enough. The process
    command line must name the profile directory. When `ps` cannot answer (missing or
    timing out) a live pid is assumed to be the browser, the conservative choice.
    """
    if not _pid_alive(pid):
        return False
    try:
        completed = run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            check=False,
            text=True,
            timeout=_PS_TIMEOUT_S,
        )
    except OSError, subprocess.TimeoutExpired:
        return True
    command = completed.stdout.strip()
    if completed.returncode != 0 or not command:
        return False
    return str(profile_dir) in command


def read_session(
    profile_dir: Path,
    *,
    pid_alive: Callable[[int], bool] | None = None,
) -> BrowserSession | None:
    """Return the recorded session when its browser process is still alive."""
    try:
        raw = json.loads((profile_dir / SESSION_FILENAME).read_text(encoding="utf-8"))
        session = BrowserSession(
            browser=str(raw["browser"]),
            pid=int(raw["pid"]),
            port=int(raw["port"]),
            started_at=str(raw["started_at"]),
        )
    except OSError, ValueError, KeyError, TypeError:
        return None
    alive = pid_alive or (lambda pid: process_is_browser_for(pid, profile_dir))
    return session if alive(session.pid) else None


def remove_session(profile_dir: Path) -> None:
    """Forget the recorded session (the browser keeps running if it still is)."""
    with contextlib.suppress(FileNotFoundError):
        (profile_dir / SESSION_FILENAME).unlink()


def require_blumkin_profile(path: Path) -> BrowserProfile:
    """Return the profile at `path`, refusing any directory blumkin did not create."""
    if path.is_symlink() or not (path / MARKER_FILENAME).is_file():
        raise NotBlumkinProfileError(
            f"Expected a browser profile created by blumkin at {path}, got a directory without "
            "its marker"
        )
    return BrowserProfile(task=path.name, path=path)


def write_session(profile_dir: Path, session: BrowserSession) -> None:
    """Record the running browser for the profile (mode 0600)."""
    payload = {
        "browser": session.browser,
        "pid": session.pid,
        "port": session.port,
        "started_at": session.started_at,
    }
    _write_private(profile_dir / SESSION_FILENAME, json.dumps(payload, sort_keys=True))


_PS_TIMEOUT_S = 5.0


_TASK_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _ensure_private_dir(path: Path) -> Path:
    if path.is_symlink():
        raise BrowserProfileError(f"Expected a real directory at {path}, got a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        path.chmod(0o700)
    return path


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_task_name(task: str) -> None:
    if not _TASK_NAME.fullmatch(task):
        raise BrowserProfileError(
            "Expected a task name of 1-64 lowercase letters, digits, '-' or '_' starting with a "
            f"letter or digit, got {task!r}"
        )


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)
