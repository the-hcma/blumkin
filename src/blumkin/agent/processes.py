"""Discover orphaned `blumkin-agent` and stale `blumkin mcp serve` processes.

Issue #408 (a #401 follow-up): #403/#404 fixed the *primary* agent socket
resolution and shipped `agent status`'s version-skew warning plus `agent
stop`, but `blumkin upgrade` still leaves three kinds of process invisible
to it:

- an agent listening on a *legacy* socket path (the pre-#403 `$TMPDIR`
  location) that this build's `runtime_base_dir()` no longer resolves to,
  left running the previous build - the exact split observed in the issue
- any other reachable agent socket at all, orphaned from the one socket
  this CLI build resolves to
- a `blumkin mcp serve` process, which has no socket/IPC surface of its own
  to query and so can only be found by listing processes

Deliberately macOS-only (`is_supported_platform`), like the rest of
`blumkin-agent`: the compiled binary, and the split-socket scenario this
module exists to detect, do not exist elsewhere.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from blumkin.agent import client as agent_client
from blumkin.agent.paths import is_supported_platform, runtime_base_dir
from blumkin.version import _normalize_commit


@dataclass(frozen=True, slots=True)
class AgentInstance:
    """One reachable `blumkin-agent`, found at either the current socket
    path or a legacy/orphaned one."""

    socket_path: Path
    pid: int | None
    version: str | None
    is_primary: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "socket_path": str(self.socket_path),
            "pid": self.pid,
            "version": self.version,
            "is_primary": self.is_primary,
        }


@dataclass(frozen=True, slots=True)
class McpServeProcess:
    """One running `blumkin ... mcp serve` process, found via `ps` (it has no
    socket/IPC surface of its own to query directly)."""

    pid: int
    command: str
    started_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "command": self.command,
            "started_at": self.started_at.isoformat() if self.started_at else None,
        }


def discover_agent_instances() -> list[AgentInstance]:
    """Probe the current socket plus every legacy candidate, returning every
    one that actually answers - not just the one this CLI build resolves to.

    Mirrors the `runtime_base_dir` fallback ladder (issue #402/#403) so an
    agent left running from *before* that fix (bound under plain
    `$TMPDIR`/`/tmp` instead of the Darwin `confstr` directory) is still
    found, even though `blumkin agent status` no longer resolves to it on
    its own.

    The primary path is computed the same way `_candidate_sockets` builds
    every other entry - `runtime_base_dir()` directly, not
    `agent.paths.socket_path()` - so this function has no mkdir/symlink-
    refusal side effect of its own and never raises `RuntimeError` (PR #409
    review: every caller relies on a clean `[]`/`reachable: false`, not an
    unhandled traceback, for a hostile or foreign-owned runtime dir).
    """
    if not is_supported_platform():
        return []
    primary = runtime_base_dir() / f"blumkin-agent-{os.getuid()}" / "agent.sock"
    instances: list[AgentInstance] = []
    for candidate in _candidate_sockets():
        if not _is_safe_agent_socket_dir(candidate.parent):
            continue
        response = agent_client.call_at(candidate, "status")
        if response is None:
            continue
        instances.append(
            AgentInstance(
                socket_path=candidate,
                pid=response.get("agent_pid"),
                version=_agent_instance_version(response),
                is_primary=(candidate == primary),
            )
        )
    return instances


def mcp_serve_processes() -> list[McpServeProcess]:
    """Every currently running `blumkin ... mcp serve` process, excluding
    this one.

    Found via `ps` (macOS only, mirroring `is_supported_platform`) rather
    than any IPC: unlike `blumkin-agent`, `mcp serve` speaks only stdio to
    its one host process, with no channel of its own to ask "what build are
    you" (issue #408 item 4's "no channel to ask" problem, worked around
    here by listing processes instead).
    """
    if sys.platform != "darwin":
        return []
    self_pid = os.getpid()
    processes: list[McpServeProcess] = []
    for pid, started_at, command in _list_processes():
        if pid == self_pid:
            continue
        if not _is_mcp_serve_command(command):
            continue
        processes.append(McpServeProcess(pid=pid, command=command, started_at=started_at))
    return processes


def orphaned_agent_instances() -> list[AgentInstance]:
    """`discover_agent_instances`, minus the one this CLI build resolves to."""
    return [instance for instance in discover_agent_instances() if not instance.is_primary]


#: `ps -o lstart=`'s fixed `ctime`-style format: "Www Mmm dd hh:mm:ss yyyy" -
#: exactly 5 whitespace-separated tokens, however much padding `ps` adds
#: around the field itself before the command column starts.
_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"
_LSTART_PATTERN = re.compile(r"^(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(.*)$")


def _agent_instance_version(response: dict[str, Any]) -> str | None:
    """Combine a `status` reply's `agent_version`/`agent_commit` into the
    same `<version> (<commit>)` shape `version.build_version`/
    `read_installed_version` use, so `AgentInstance.version` is directly
    comparable against `blumkin upgrade`'s `before`/`after` reads - a bare
    package version (e.g. `0.5.0`) never equals that format, which made a
    just-respawned, already-current agent look indistinguishable from a
    stale one (PR #409 review). The Rust agent reports `agent_commit`
    verbatim (a full 40-char SHA in a released build), while
    `version.git_commit`/`build_version` always truncate to 12 chars via
    `_normalize_commit` - without matching that truncation here, the two
    sides could never compare equal in production, so the just-respawned
    agent would still be wrongly shut down (PR #409 review, seventh
    pass)."""
    version = response.get("agent_version")
    commit = response.get("agent_commit")
    if isinstance(version, str) and isinstance(commit, str):
        return f"{version} ({_normalize_commit(commit)})"
    return version if isinstance(version, str) else None


def _candidate_sockets() -> list[Path]:
    """Every base directory `runtime_base_dir` could plausibly have resolved
    to, on this or an earlier blumkin build - current result first, then
    every fallback in its resolution ladder (issue #402), deduplicated."""
    uid = os.getuid()
    bases: list[Path] = [runtime_base_dir()]
    for var in ("TMPDIR", "TEMP", "TMP", "XDG_RUNTIME_DIR"):
        value = os.environ.get(var)
        if value:
            bases.append(Path(value))
    for fallback in ("/tmp", "/var/tmp", "/usr/tmp"):
        bases.append(Path(fallback))
    seen: set[str] = set()
    deduped: list[Path] = []
    for base in bases:
        key = str(base)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(base)
    return [base / f"blumkin-agent-{uid}" / "agent.sock" for base in deduped]


#: Interpreter options `python` accepts before `-m` that take their value as a
#: *separate* following token (`-X dev`, `-W ignore`), rather than folded into
#: the option itself - both must be skipped as a pair, or the operand (which
#: doesn't start with `-`) is mistaken for the end of the options run
#: (PR #409 review).
_PY_OPTIONS_WITH_OPERAND = frozenset({"-W", "-X"})

#: `blumkin`'s root click group options that may precede any subcommand,
#: e.g. `blumkin --profile work mcp serve` - `--profile`/`--tz` take a
#: *separate* following token, while `--json`/`--version` are flags (PR #409
#: review, fifth pass).
_GLOBAL_OPTIONS_WITH_OPERAND = frozenset({"--profile", "--tz"})
_GLOBAL_FLAGS = frozenset({"--json", "--version"})


def _skip_global_options(tokens: list[str]) -> list[str]:
    """Skip any leading root-group options (`--profile work`, `--json`, ...)
    so the `mcp serve` positional check isn't fooled into returning False
    just because a global option came first (PR #409 review, fifth pass)."""
    index = 0
    while index < len(tokens) and tokens[index].startswith("-"):
        token = tokens[index]
        if token in _GLOBAL_OPTIONS_WITH_OPERAND:
            index += 2
        elif token in _GLOBAL_FLAGS or "=" in token:
            index += 1
        else:
            break
    return tokens[index:]


def _is_mcp_serve_command(command: str) -> bool:
    """Whether `command` (a `ps -o command=` value) is a real
    `blumkin ... mcp serve` invocation - argv-token matching, not a raw
    substring check, so e.g. `grep blumkin mcp serve` or an editor tab
    titled `.../reserve.py` naming all three words never counts (PR #409
    review).

    Best-effort, not exhaustive: it covers the `blumkin mcp serve` console
    script, `mcp install`'s registered console-script shebang launch, and a
    `python[3] [-m] blumkin mcp serve` module launch, each with leading
    interpreter/root-group options skipped. Deliberately frozen at this
    shape (PR #409 review, Governance) rather than chasing further argv
    shapes (`uv run blumkin mcp serve`, `env FOO=1 blumkin mcp serve`,
    `sh -c` wrappers, ...) one at a time - a miss here only under-reports a
    stale `mcp serve` process; `agent stop --all`/`doctor` still surface
    the socket-based agents via a different, exact mechanism. Any further
    argv-shape coverage belongs in a follow-up PR, not more edge cases
    here."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    argv0 = Path(tokens[0]).name
    if argv0 == "blumkin":
        rest = tokens[1:]
    elif argv0.lower().startswith("python"):
        # Python permits interpreter options (`-u`, `-O`, ...) before `-m`,
        # e.g. `python3 -u -m blumkin mcp serve` (PR #409 review) - skip
        # past any of those rather than requiring `-m` at a fixed index.
        # Lowercased comparison also catches framework builds like
        # `.../Python.app/Contents/MacOS/Python` (capital `P`) (PR #409
        # review, sixth pass).
        remaining = tokens[1:]
        while remaining and remaining[0] != "-m" and remaining[0].startswith("-"):
            if remaining[0] in _PY_OPTIONS_WITH_OPERAND:
                remaining = remaining[2:]
            else:
                remaining = remaining[1:]
        if not remaining:
            return False
        if remaining[0] == "-m":
            # `python -m blumkin mcp serve` - explicit module launch.
            if len(remaining) < 2 or remaining[1] != "blumkin":
                return False
            rest = remaining[2:]
        elif Path(remaining[0]).name == "blumkin":
            # A `blumkin` console script is a `#!<python>` file, so the
            # kernel execs the interpreter with the script path as its
            # first argument, e.g. `<venv-python> <path>/blumkin mcp
            # serve` - this is the shape `mcp install`'s registered
            # `resolve_binary()` entry actually produces, not `-m blumkin`
            # (PR #409 review, sixth pass).
            rest = remaining[1:]
        else:
            return False
    else:
        return False
    # `mcp serve` always follows `ServeSpec.args()`'s `["mcp", "serve"]` shape
    # (never trailing flags) but may be preceded by root-group options like
    # `--profile work` (PR #409 review, fifth pass) - skip those, then match
    # the first two remaining tokens positionally, not an order-independent
    # membership check, so e.g. `blumkin mcp install --serve` or
    # `blumkin skills show mcp serve` never counts (PR #409 review, fourth
    # pass).
    return _skip_global_options(rest)[:2] == ["mcp", "serve"]


def _is_safe_agent_socket_dir(directory: Path) -> bool:
    """Read-only mirror of `agent.paths._ensure_private_owned_dir`'s checks,
    for candidate legacy directories this process must only ever read, never
    create or `chmod` (unlike its own runtime dir).

    Every candidate base (`$TMPDIR`, `/tmp`, `/var/tmp`, `/usr/tmp`, ...) is
    world-writable, so any local, same-privilege user could otherwise plant
    `<base>/blumkin-agent-<uid>/agent.sock` ahead of time and have their own
    process impersonate a stopped agent's `status`/`shutdown` replies (PR
    #409 review). Refuses a symlink, a non-directory, a directory this
    process does not own, or one that is group/other readable or
    writable - the same trust boundary `_ensure_private_owned_dir` enforces
    for the one directory this process is willing to create itself.
    """
    try:
        info = directory.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    if info.st_uid != os.getuid():
        return False
    return not info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)


def _list_processes() -> list[tuple[int, datetime | None, str]]:
    """`(pid, started_at, command)` for every process `ps` will show -
    best-effort, empty on any failure rather than raising."""
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,lstart=,command="],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except OSError, subprocess.SubprocessError:
        return []
    if completed.returncode != 0:
        return []
    rows: list[tuple[int, datetime | None, str]] = []
    for line in (completed.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, rest = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        match = _LSTART_PATTERN.match(rest.strip())
        if match is None:
            rows.append((pid, None, rest.strip()))
            continue
        raw_started_at, command = match.groups()
        try:
            started_at = datetime.strptime(raw_started_at, _LSTART_FORMAT)
        except ValueError:
            started_at = None
        rows.append((pid, started_at, command.strip()))
    return rows
