"""Blumkin package version and source revision information.

Resolution order for both fields, so a wheel installed from PyPI with no
``.git`` present still answers: environment override, then the values
``scripts/embed_build_metadata`` bakes into :mod:`blumkin._build_metadata`
before ``uv build``, then ``git rev-parse`` in a checkout, then ``unknown``.
Never raises.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from blumkin import _build_metadata

PACKAGE_NAME = "blumkin"


def build_info() -> str:
    """Return ``blumkin <version> (<commit>)`` for ``--version``."""
    return format_cli_version_line(prog=PACKAGE_NAME)


def build_status_fields() -> dict[str, str]:
    """Build fields for ``doctor --json`` and ``auth status --json``."""
    package, commit = get_build_info()
    return {
        "build_commit": commit,
        "build_version": package,
        "running_from": str(running_command_path()),
    }


def build_version() -> str:
    """Return ``<version> (<commit>)`` without a program name (for ``upgrade``)."""
    package, commit = get_build_info()
    return f"{package} ({commit})"


def format_cli_version_line(*, prog: str) -> str:
    """One-line version string for ``--version`` on a console entry point."""
    package, commit = get_build_info()
    return f"{prog} {package} ({commit})"


@lru_cache(maxsize=1)
def get_build_info() -> tuple[str, str]:
    """Return ``(package_version, commit_short_or_unknown)`` once per process."""
    return (package_version(), git_commit())


def git_commit(
    *,
    environ: Mapping[str, str] | None = None,
    repository: Path | None = None,
) -> str:
    """Return the configured, embedded, or checkout commit, shortened for display.

    Only ``BLUMKIN_GIT_SHA`` overrides - deliberately *not* ``GITHUB_SHA``. GitHub
    Actions exports ``GITHUB_SHA`` for the workflow's own repo on every job, so an
    installed blumkin invoked from an unrelated workflow would otherwise report
    that repo's commit as its own. The release workflow bakes ``GITHUB_SHA`` into
    the embedded stamp at build time, which is where it belongs.
    """
    environment = os.environ if environ is None else environ
    configured_sha = environment.get("BLUMKIN_GIT_SHA", "").strip()
    if configured_sha:
        return _normalize_commit(configured_sha)

    embedded = getattr(_build_metadata, "EMBEDDED_COMMIT", "")
    if isinstance(embedded, str) and embedded.strip():
        return _normalize_commit(embedded)

    checkout = repository or _repository_root()
    if not (checkout / ".git").exists():
        return "unknown"

    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        )
    except OSError, subprocess.SubprocessError:
        return "unknown"
    return result.stdout.strip() or "unknown"


def is_source_checkout(*, repository: Path | None = None) -> bool:
    """Return whether this process runs from a git checkout of blumkin."""
    checkout = repository or _repository_root()
    return (checkout / ".git").exists()


def package_version(*, pyproject_path: Path | None = None) -> str:
    """Return the distribution version, with a source-checkout fallback."""
    embedded = getattr(_build_metadata, "EMBEDDED_VERSION", "")
    if isinstance(embedded, str) and embedded.strip():
        return embedded.strip()
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        path = pyproject_path or _repository_root() / "pyproject.toml"
        return _pyproject_version(path)


def running_command_path() -> Path:
    """Return the absolute path of the command that started this process.

    Kept as the PATH entry (for example ``~/.local/bin/blumkin``) rather than the
    venv target a console-script symlink points at, so a pipx or uv-tool install
    reports where the operator's shell actually found ``blumkin``. Bare names are
    resolved with :func:`shutil.which` so a PATH launch is not reported as
    ``$PWD/blumkin``.
    """
    argv0 = Path(sys.argv[0]).expanduser()
    if not argv0.is_absolute():
        located = shutil.which(os.fspath(argv0))
        if located:
            argv0 = Path(located)
    try:
        return argv0.absolute()
    except OSError:
        return argv0


def _normalize_commit(token: str) -> str:
    stripped = token.strip()
    if not stripped:
        return "unknown"
    if len(stripped) > 12:
        return stripped[:12]
    return stripped


def _pyproject_version(path: Path) -> str:
    try:
        with path.open("rb") as pyproject_file:
            project = tomllib.load(pyproject_file).get("project", {})
    except OSError, tomllib.TOMLDecodeError:
        return "unknown"

    version_value = project.get("version")
    return version_value if isinstance(version_value, str) else "unknown"


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]
