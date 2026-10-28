"""Is the running blumkin behind the checkout it is used next to? (issue #547)

An installed blumkin (pipx, uv tool) can be older than the repository the user is working in:
a command the docs or skills describe is then simply missing, and `--version` gives the same
number for both. This compares the running build with a blumkin git checkout found through
`BLUMKIN_REPOSITORY` or by walking up from the working directory, offline: the commit it was
built from against the checkout's HEAD (information), and the checkout's top-level commands
against the ones this CLI has (the actionable part: a release is always some commits behind
`main`, so being behind alone is not a problem, a missing command is). A CLI that runs from the
checkout itself is never compared with it (`upgrade` covers a stale editable install). Nothing
here raises, and git runs without the checkout's or the user's configuration, under one
deadline.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
import time
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blumkin.version import PACKAGE_NAME, git_commit, source_tree

GIT_DEADLINE_S = 3.0
REPOSITORY_ENV = "BLUMKIN_REPOSITORY"

_COMMAND_SUFFIXES = ("-command", "-cmd", "-group", "-grp")
_GIT_FLAGS = (
    "--no-optional-locks",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.http.allow=never",
    "-c",
    "protocol.https.allow=never",
    "-c",
    "protocol.ssh.allow=never",
    "-c",
    "protocol.git.allow=never",
    "-c",
    "protocol.file.allow=never",
)
_SHORT_HASH = re.compile(r"[0-9a-f]{7,40}")


class _Unset:
    """The marker for an argument that was not given (None is a real answer there)."""


_UNSET = _Unset()


@dataclass(frozen=True, slots=True)
class RepositoryStatus:
    """How the running build compares with a blumkin checkout."""

    behind_by: int | None
    build_commit: str
    head: str | None
    missing_commands: tuple[str, ...]
    path: Path
    #: "same", "behind", "ahead", "diverged", or "unknown" when the two cannot be compared.
    relation: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "behind_by": self.behind_by,
            "build_commit": self.build_commit,
            "head": self.head,
            "missing_commands": list(self.missing_commands),
            "path": str(self.path),
            "relation": self.relation,
        }

    def note(self) -> str | None:
        """One line saying what to do, or None. Only missing commands are worth a note."""
        if not self.missing_commands:
            return None
        lead = f"this build lacks commands the checkout at {self.path} has: " + ", ".join(
            self.missing_commands
        )
        if self.relation == "behind":
            count = self.behind_by or 0
            lead += f" ({count} commit{'s' if count != 1 else ''} behind it)"
        if self.relation == "ahead":
            return (
                f"{lead} - the build is newer than the checkout; update the checkout (`git pull`)"
            )
        path = shlex.quote(str(self.path))
        return (
            f"{lead} - a newer release may add them (`blumkin upgrade`), or install the "
            f"checkout: `uv tool install -e {path}` "
            f"(`pipx install --editable {path} --force` for a pipx install)"
        )


def checkout_commands(repository: Path) -> tuple[str, ...]:
    """The top-level command names declared in the checkout's `cli.py`, read without importing."""
    try:
        tree = ast.parse((repository / "src" / "blumkin" / "cli.py").read_text(encoding="utf-8"))
    except OSError, SyntaxError, ValueError:
        return ()
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            name = _declared_name(decorator, node.name)
            if name is not None:
                names.add(name)
    return tuple(sorted(names))


def current_status(
    *,
    build_commit: str | None = None,
    environ: Mapping[str, str] | None = None,
    running_commands: Iterable[str],
    running_from: Path | None | _Unset = _UNSET,
    start: Path | None = None,
) -> RepositoryStatus | None:
    """The status against the checkout in use, or None when there is none to compare with.

    `running_from` is the checkout this process runs from (None for an installed release); it
    defaults to the real answer.
    """
    try:
        tree = _running_source_tree() if isinstance(running_from, _Unset) else running_from
        if tree is not None:
            return None
        repository = find_repository(
            start=start if start is not None else Path.cwd(),
            environ=os.environ if environ is None else environ,
        )
        if repository is None:
            return None
        return repository_status(
            repository,
            build_commit=build_commit if build_commit is not None else git_commit(),
            running_commands=running_commands,
        )
    except Exception:  # noqa: BLE001 - a version banner and doctor must never fail on this
        return None


def find_repository(*, start: Path, environ: Mapping[str, str]) -> Path | None:
    """A blumkin git checkout: `BLUMKIN_REPOSITORY`, else the nearest one above `start`."""
    configured = environ.get(REPOSITORY_ENV, "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if _is_repository(candidate) else None
    here = start.resolve()
    for directory in (here, *here.parents):
        if _is_repository(directory):
            return directory
    return None


def repository_status(
    repository: Path, *, build_commit: str, running_commands: Iterable[str]
) -> RepositoryStatus:
    """Compare `build_commit` and the running commands with the checkout at `repository`."""
    present = set(running_commands)
    missing = tuple(name for name in checkout_commands(repository) if name not in present)
    git = _Git(repository)
    code, full_head = git.run("rev-parse", "HEAD")
    head = full_head[:12] if code == 0 and full_head else None
    behind_by: int | None = None
    relation = "unknown"
    commit = build_commit.strip().lower()
    if head is not None and _SHORT_HASH.fullmatch(commit):
        if full_head.startswith(commit):
            relation, behind_by = "same", 0
        elif git.run("cat-file", "-t", f"{commit}^{{commit}}") == (0, "commit"):
            # Only exit code 1 of `--is-ancestor` means "no"; any other failure is unknown.
            forward = git.run("merge-base", "--is-ancestor", commit, full_head)[0]
            if forward == 0:
                counted, count = git.run("rev-list", "--count", f"{commit}..{full_head}")
                if counted == 0 and count.isdigit():
                    relation, behind_by = "behind", int(count)
            elif forward == 1:
                backward = git.run("merge-base", "--is-ancestor", full_head, commit)[0]
                if backward == 0:
                    relation, behind_by = "ahead", 0
                elif backward == 1:
                    shallow_code, shallow = git.run("rev-parse", "--is-shallow-repository")
                    if shallow_code == 0 and shallow == "false":
                        relation = "diverged"
    return RepositoryStatus(
        behind_by=behind_by,
        build_commit=build_commit,
        head=head,
        missing_commands=missing,
        path=repository,
        relation=relation,
    )


class _Git:
    """Runs git in one repository under a shared deadline, without inherited state or config."""

    def __init__(self, repository: Path) -> None:
        self._deadline = time.monotonic() + GIT_DEADLINE_S
        self._env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self._env.update(
            {
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        self._repository = repository

    def run(self, *args: str) -> tuple[int, str]:
        """(exit code, stdout); (-1, "") when git is missing, fails to start or runs out of time."""
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            return -1, ""
        try:
            done = subprocess.run(
                ["git", *_GIT_FLAGS, "-C", str(self._repository), *args],
                capture_output=True,
                text=True,
                timeout=remaining,
                env=self._env,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            return -1, ""
        return done.returncode, done.stdout.strip()


def _declared_name(decorator: ast.expr, function_name: str) -> str | None:
    """The command name `@main.command(...)` / `@main.group(...)` gives, else None."""
    if not isinstance(decorator, ast.Call):
        return None
    target = decorator.func
    if not (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "main"
        and target.attr in ("command", "group")
    ):
        return None
    if decorator.args:
        # A name that is not a literal cannot be read without running the code: skip it
        # rather than guess a name that may be wrong.
        value = decorator.args[0]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        return None
    for keyword in decorator.keywords:
        if keyword.arg == "name":
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
            return None
    name = function_name.lower().replace("_", "-")
    for suffix in _COMMAND_SUFFIXES:
        if name.endswith(suffix) and name != suffix.lstrip("-"):
            return name[: -len(suffix)]
    return name


def _is_repository(path: Path) -> bool:
    """A git checkout whose pyproject names this package."""
    try:
        project = tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))
    except OSError, ValueError:
        return False
    return project.get("project", {}).get("name") == PACKAGE_NAME and (path / ".git").exists()


def _running_source_tree() -> Path | None:
    return source_tree()
