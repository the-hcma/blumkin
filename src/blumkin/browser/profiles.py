"""Per-task browser profiles and the session records kept inside them (issue #465).

Each task gets its own browser profile directory under `<config dir>/browser-profiles/`,
created `0700` with a marker file so blumkin only ever attaches to, lists or deletes
profiles it created. A running browser is recorded in a `0600` session file next to the
profile data; cookies from the user's manual sign-in live only in that directory.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
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


LOCK_FILENAME = ".blumkin-launch.lock"


MARKER_FILENAME = ".blumkin-profile.json"


class NotBlumkinProfileError(BrowserProfileError):
    """The directory is not a profile blumkin created."""


PROFILES_DIRNAME = "browser-profiles"


class ProfileBusyError(BrowserProfileError):
    """Another launch or delete is already working on the profile."""


class ProfileInUseError(BrowserProfileError):
    """The profile is busy: its browser is running, or a launch for it is in progress."""


class ProfileBrowserRunningError(ProfileInUseError):
    """The profile's browser is still running (and can be closed)."""


SESSION_FILENAME = ".blumkin-session.json"


def create_profile(config_dir: Path, task: str) -> BrowserProfile:
    """Create the profile for `task`, or return the existing one blumkin made.

    The directory is built under a staging name (marker included) and renamed into
    place, so a crash or a concurrent create never leaves an unmarked profile behind.
    """
    validate_task_name(task)
    root = ensure_private_dir(config_dir / PROFILES_DIRNAME)
    path = root / task
    if path.is_symlink():
        raise BrowserProfileError(f"Expected a real directory at {path}, got a symlink")
    if path.exists():
        return require_blumkin_profile(path)
    _remove_stale_staging(root, task)
    staging = Path(tempfile.mkdtemp(prefix=f".{task}.creating-", dir=root))
    try:
        staging.chmod(0o700)
        marker = {"created_at": _utc_now(), "task": task}
        write_private(staging / MARKER_FILENAME, json.dumps(marker, sort_keys=True))
        os.rename(staging, path)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        if path.exists():
            return require_blumkin_profile(path)
        raise
    return BrowserProfile(task=task, path=path)


def delete_profile(
    config_dir: Path,
    task: str,
    *,
    is_browser: Callable[[int, Path], bool] | None = None,
    pid_alive: Callable[[int], bool] | None = None,
) -> None:
    """Delete the profile for `task` (cookies included); refuse while its browser runs."""
    validate_task_name(task)
    profile = load_profile(config_dir, task)
    if profile is None:
        raise BrowserProfileError(
            f"Expected an existing browser profile for task {task!r}, got none"
        )
    try:
        with profile_lock(profile):
            if (
                read_session(profile.path, pid_alive=pid_alive) is not None
                or running_browser_pid(profile.path, is_browser=is_browser) is not None
            ):
                raise ProfileBrowserRunningError(
                    f"Expected the browser for task {task!r} to be closed before deleting its "
                    "profile, got a running browser"
                )
            shutil.rmtree(profile.path)
    except ProfileBusyError as exc:
        raise ProfileInUseError(
            f"Expected no launch of task {task!r} in progress before deleting its profile, "
            "got one still running"
        ) from exc


def ensure_private_dir(path: Path) -> Path:
    if path.is_symlink():
        raise BrowserProfileError(f"Expected a real directory at {path}, got a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        path.chmod(0o700)
    return path


def is_valid_task_name(task: str) -> bool:
    """True when `task` is a name `create_profile` accepts."""
    return _TASK_NAME.fullmatch(task) is not None


def list_profiles(config_dir: Path) -> list[BrowserProfile]:
    """Return the profiles blumkin created, sorted by task name."""
    root = config_dir / PROFILES_DIRNAME
    if not root.is_dir():
        return []
    found: list[BrowserProfile] = []
    for child in sorted(root.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_dir() and not child.is_symlink() and _has_marker(child):
            found.append(BrowserProfile(task=child.name, path=child))
    return found


def load_profile(config_dir: Path, task: str) -> BrowserProfile | None:
    """Return the profile for `task`, or None when there is none."""
    try:
        validate_task_name(task)
    except BrowserProfileError:
        return None
    path = config_dir / PROFILES_DIRNAME / task
    if path.is_dir() and not path.is_symlink() and _has_marker(path):
        return BrowserProfile(task=task, path=path)
    return None


def process_is_browser_for(
    pid: int,
    profile_dir: Path,
    *,
    assume_on_unknown: bool = True,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> bool:
    """True when `pid` is a live process started on `profile_dir`.

    A session file outlives its browser (a crash, a reboot), and the recorded pid may
    since belong to an unrelated process; so liveness alone is not enough. The process
    command line must name the profile directory. When `ps` cannot answer (missing or
    timing out), `assume_on_unknown` decides: True (the default, right for deleting a
    profile) assumes a live pid is the browser; False (right for deciding to reuse a
    session) assumes it is not.
    """
    if not _pid_alive(pid):
        return False
    try:
        completed = (run or subprocess.run)(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            check=False,
            text=True,
            timeout=_PS_TIMEOUT_S,
        )
    except OSError, subprocess.TimeoutExpired:
        return assume_on_unknown
    command = completed.stdout.strip()
    if completed.returncode != 0 or not command:
        return False
    return re.search(_user_data_dir_pattern(profile_dir), command) is not None


@contextlib.contextmanager
def profile_lock(profile: BrowserProfile) -> Iterator[None]:
    """Hold the profile's launch lock; a second holder fails fast instead of racing."""
    fd = os.open(profile.path / LOCK_FILENAME, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProfileBusyError(
                f"Expected no other launch or delete for task {profile.task!r} in progress, "
                "got another still running"
            ) from exc
        yield
    finally:
        os.close(fd)


def read_session(
    profile_dir: Path,
    *,
    pid_alive: Callable[[int], bool] | None = None,
    strict: bool = False,
) -> BrowserSession | None:
    """Return the recorded session when its browser process is still alive.

    `strict` is for deciding to reuse the session: a process that cannot be confirmed as the
    browser (no working `ps`) does not count. The default is for deciding to refuse a delete.
    """
    try:
        raw = json.loads((profile_dir / SESSION_FILENAME).read_text(encoding="utf-8"))
        session = BrowserSession(
            browser=str(raw["browser"]),
            pid=int(raw["pid"]),
            port=int(raw["port"]),
            started_at=str(raw["started_at"]),
        )
    except OSError, ValueError, KeyError, TypeError, OverflowError:
        return None
    if not 1 < session.pid <= _MAX_PID or not 1 <= session.port <= 65535:
        return None
    alive = pid_alive or (
        lambda pid: process_is_browser_for(pid, profile_dir, assume_on_unknown=not strict)
    )
    return session if alive(session.pid) else None


def remove_session(profile_dir: Path) -> None:
    """Forget the recorded session (the browser keeps running if it still is)."""
    with contextlib.suppress(FileNotFoundError):
        (profile_dir / SESSION_FILENAME).unlink()


def require_blumkin_profile(path: Path) -> BrowserProfile:
    """Return the profile at `path`, refusing any directory blumkin did not create."""
    if path.is_symlink() or not _has_marker(path):
        raise NotBlumkinProfileError(
            f"Expected a browser profile created by blumkin at {path}, got a directory without "
            "its marker"
        )
    return BrowserProfile(task=path.name, path=path)


def running_browser_pid(
    profile_dir: Path,
    *,
    is_browser: Callable[[int, Path], bool] | None = None,
    strict: bool = False,
) -> int | None:
    """The pid of a browser running on `profile_dir` even with no session record.

    Chrome keeps `SingletonLock`, a symlink to `<host>-<pid>`, in its user data directory
    while it runs. The pid counts only if that process is really a browser on this profile.
    """
    try:
        target = os.readlink(profile_dir / "SingletonLock")
    except OSError:
        return None
    _, _, tail = target.rpartition("-")
    try:
        pid = int(tail)
    except ValueError:
        return None
    check = is_browser or (
        lambda pid, directory: process_is_browser_for(pid, directory, assume_on_unknown=not strict)
    )
    return pid if pid > 1 and check(pid, profile_dir) else None


def validate_task_name(task: str) -> None:
    """Raise `BrowserProfileError` unless `task` is a valid task name."""
    if not _TASK_NAME.fullmatch(task):
        raise BrowserProfileError(
            "Expected a task name of 1-64 lowercase letters, digits, '-' or '_' starting with a "
            f"letter or digit, got {task!r}"
        )


def write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_session(profile_dir: Path, session: BrowserSession) -> None:
    """Record the running browser for the profile (mode 0600)."""
    payload = {
        "browser": session.browser,
        "pid": session.pid,
        "port": session.port,
        "started_at": session.started_at,
    }
    write_private(profile_dir / SESSION_FILENAME, json.dumps(payload, sort_keys=True))


_MAX_PID = 2**22


_PS_TIMEOUT_S = 5.0


_STALE_STAGING_S = 3600.0


_TASK_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _has_marker(path: Path) -> bool:
    marker = path / MARKER_FILENAME
    return marker.is_file() and not marker.is_symlink()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError, OverflowError:
        return False
    except PermissionError:
        return True
    return True


def _remove_stale_staging(root: Path, task: str) -> None:
    """Remove staging directories a crashed create left behind (never a recent one)."""
    cutoff = time.time() - _STALE_STAGING_S
    for leftover in root.glob(f".{task}.creating-*"):
        with contextlib.suppress(OSError):
            if leftover.is_dir() and leftover.stat().st_mtime < cutoff:
                shutil.rmtree(leftover)


def _user_data_dir_pattern(profile_dir: Path) -> str:
    # Followed by the next flag or the end of the command line, so `/x/a` never matches
    # a browser on `/x/ab`; the path itself may contain spaces.
    return rf"--user-data-dir={re.escape(str(profile_dir))}(?=\s+--|\s*$)"


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
