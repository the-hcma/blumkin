"""Is the running blumkin behind the checkout it is used next to? (issue #547)

An installed blumkin (pipx, uv tool) can be older than the repository the user is working in:
a command the docs or skills describe is then simply missing, and `--version` gives the same
number for both. This compares the running build with a blumkin git checkout found through
`BLUMKIN_REPOSITORY` or by walking up from the working directory, offline: the commit it was
built from against the checkout's HEAD, and the checkout's top-level commands against the ones
this CLI has. A CLI that runs from the checkout itself is never compared with it (`upgrade`
covers a stale editable install). Nothing here raises.
"""

from __future__ import annotations

import ast
import subprocess
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blumkin.version import PACKAGE_NAME, git_commit, source_tree

GIT_TIMEOUT_S = 5
REPOSITORY_ENV = "BLUMKIN_REPOSITORY"

_COMMAND_SUFFIXES = ("-command", "-cmd", "-group", "-grp")
_UNSET: Any = object()


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
        """One line saying what is wrong, or None when the install is fine."""
        parts: list[str] = []
        if self.relation == "behind":
            count = self.behind_by or 0
            parts.append(
                f"{count} commit{'s' if count != 1 else ''} behind the checkout at {self.path} "
                f"(build {self.build_commit}, checkout {self.head})"
            )
        elif self.relation == "diverged":
            parts.append(
                f"built from a different line of history than the checkout at {self.path} "
                f"(build {self.build_commit}, checkout {self.head})"
            )
        elif self.relation == "unknown":
            parts.append(
                f"build commit {self.build_commit!r} is not in the checkout at {self.path}, "
                "so it cannot be compared"
                if self.head is not None and self.build_commit not in ("", "unknown")
                else f"cannot compare this build with the checkout at {self.path}"
            )
        if self.missing_commands:
            parts.append(
                "this CLI lacks commands the checkout has: " + ", ".join(self.missing_commands)
            )
        if not parts:
            return None
        advice = (
            "run `blumkin upgrade`" if self.relation != "unknown" or self.missing_commands else ""
        )
        return "; ".join(parts) + (f" - {advice}" if advice else "")


def checkout_commands(repository: Path) -> tuple[str, ...]:
    """The top-level command names declared in the checkout's `cli.py`, read without importing."""
    try:
        tree = ast.parse((repository / "src" / "blumkin" / "cli.py").read_text(encoding="utf-8"))
    except OSError, SyntaxError, ValueError:
        return ()
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            name = _declared_name(decorator, node.name)
            if name is not None:
                names.add(name)
    return tuple(sorted(names))


def current_status(
    *,
    running_commands: Iterable[str],
    start: Path | None = None,
    environ: Mapping[str, str] | None = None,
    build_commit: str | None = None,
    source_tree: Path | None = _UNSET,
) -> RepositoryStatus | None:
    """The status against the checkout in use, or None when there is none to compare with."""
    try:
        running_from = _running_source_tree() if source_tree is _UNSET else source_tree
        if running_from is not None:
            return None
        import os

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
    full_head = _git(repository, "rev-parse", "HEAD")
    head = full_head[:12] if full_head else None
    behind_by: int | None = None
    relation = "unknown"
    if full_head and build_commit and build_commit != "unknown":
        if full_head.startswith(build_commit):
            relation, behind_by = "same", 0
        elif _git(repository, "cat-file", "-t", f"{build_commit}^{{commit}}") == "commit":
            if _git_ok(repository, "merge-base", "--is-ancestor", build_commit, full_head):
                count = _git(repository, "rev-list", "--count", f"{build_commit}..{full_head}")
                relation, behind_by = "behind", int(count) if count.isdigit() else None
            elif _git_ok(repository, "merge-base", "--is-ancestor", full_head, build_commit):
                relation, behind_by = "ahead", 0
            else:
                relation = "diverged"
    return RepositoryStatus(
        behind_by=behind_by,
        build_commit=build_commit,
        head=head,
        missing_commands=missing,
        path=repository,
        relation=relation,
    )


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
    if decorator.args and isinstance(decorator.args[0], ast.Constant):
        value = decorator.args[0].value
        if isinstance(value, str):
            return value
    for keyword in decorator.keywords:
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
            if isinstance(keyword.value.value, str):
                return keyword.value.value
    name = function_name.lower().replace("_", "-")
    for suffix in _COMMAND_SUFFIXES:
        if name.endswith(suffix) and name != suffix.lstrip("-"):
            return name[: -len(suffix)]
    return name


def _git(repository: Path, *args: str) -> str:
    """Stdout of a git command in `repository`, or "" on any failure."""
    try:
        done = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repository), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _git_ok(repository: Path, *args: str) -> bool:
    try:
        return (
            subprocess.run(
                ["git", "--no-optional-locks", "-C", str(repository), *args],
                capture_output=True,
                timeout=GIT_TIMEOUT_S,
                check=False,
            ).returncode
            == 0
        )
    except OSError, subprocess.SubprocessError:
        return False


def _is_repository(path: Path) -> bool:
    """A git checkout whose pyproject names this package."""
    try:
        project = tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))
    except OSError, ValueError:
        return False
    return project.get("project", {}).get("name") == PACKAGE_NAME and (path / ".git").exists()


def _running_source_tree() -> Path | None:
    return source_tree()
