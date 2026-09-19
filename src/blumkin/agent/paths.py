"""Filesystem/socket paths for the blumkin-agent background process.

Mirrors `secret_store`'s own belt-and-braces permission tightening: the
agent's runtime directory is created `0700` and re-tightened on every call
in case it pre-existed looser (`Path.mkdir`'s `mode` argument is masked by
umask and silently ignored if the directory already exists).
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path


def binary_path() -> Path:
    """Path to the compiled `blumkin-agent` binary bundled with this package.

    Placed at `blumkin/agent/bin/blumkin-agent` by the `hatchling` build
    hook (`hatch_build.py`) that runs `cargo build --release` for
    `rust-agent/`. Resolved relative to this module's own file rather than
    via `importlib.resources` so it works identically for an editable
    install (`uv tool install -e .`) and a built wheel - both simply place
    (or leave, for editable) the file at this same path inside the package.
    """
    return Path(__file__).resolve().parent / "bin" / "blumkin-agent"


def is_supported_platform() -> bool:
    """True when this platform can host a blumkin-agent at all.

    `blumkin-agent` is a compiled Rust binary (`rust-agent/`) bundled only
    for macOS today (the next layer on top of this foundation,
    `LocalAuthentication`, is a macOS-only API - see the "Platform support"
    section of issue #328), so this is `sys.platform == "darwin"` rather
    than a generic POSIX check: on Linux/Windows there is no bundled binary
    to spawn at all (`hatch_build.py` skips building one), so claiming
    support there would just fail differently, later, and less clearly.
    """
    return sys.platform == "darwin"


def runtime_dir() -> Path:
    """Private, per-user directory the agent's socket lives under.

    Scoped by the real uid (not just `$USER`, which a same-named account on
    a different uid could spoof) so two accounts that happen to share
    `$TMPDIR` can never collide on the same socket path. `BLUMKIN_AGENT_RUNTIME_DIR`
    is an override for tests, not something an operator needs to set.

    The directory name is predictable under a base that is often
    world-writable (`/tmp`), so a same-privilege local attacker could
    pre-create it (or a symlink through it) before this process ever runs -
    `_ensure_private_owned_dir` refuses that rather than silently binding
    into a directory this process does not own (mirrors
    `secret_store._refuse_symlinked_path_components`; see PR #329 review).
    """
    base = Path(os.environ.get("BLUMKIN_AGENT_RUNTIME_DIR", tempfile.gettempdir()))
    directory = base / f"blumkin-agent-{os.getuid()}"
    _ensure_private_owned_dir(directory)
    return directory


def socket_path() -> Path:
    """Unix domain socket path the agent listens on and clients connect to."""
    return runtime_dir() / "agent.sock"


def _ensure_private_owned_dir(directory: Path) -> None:
    """Create (or adopt) `directory` at 0700, refusing a planted symlink or
    a directory this process does not own.

    `directory.mkdir(exist_ok=True)` alone is not enough: it is a silent
    no-op against an existing path regardless of who owns it or whether it
    is a symlink, and a failed `chmod` was previously swallowed. Raises
    `RuntimeError` (uncaught by `agent.client`'s `OSError` handling, by
    design - a hostile runtime dir should fail loudly, not silently
    degrade to "no agent").
    """
    if directory.is_symlink():
        raise RuntimeError(f"{directory} is a symlink - refusing to follow it")
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    # Re-check after the (possibly no-op) mkdir: a racing attacker could
    # have swapped the path for a symlink between the check above and now.
    stat_result = directory.lstat()
    if stat.S_ISLNK(stat_result.st_mode):
        raise RuntimeError(f"{directory} is a symlink - refusing to follow it")
    if not stat.S_ISDIR(stat_result.st_mode):
        raise RuntimeError(f"{directory} is not a directory")
    if stat_result.st_uid != os.getuid():
        raise RuntimeError(
            f"{directory} is owned by uid {stat_result.st_uid}, not this "
            f"process's uid {os.getuid()}"
        )
    os.chmod(directory, 0o700)
