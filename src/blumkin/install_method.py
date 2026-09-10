"""Detect how the running ``blumkin`` executable is installed and managed.

``blumkin upgrade`` and ``blumkin doctor`` use this to dispatch the right
upgrade action - or explain why none applies - instead of assuming pipx. Every
install doc points at ``uv tool install`` (often the editable ``-e .`` dev
install), where a blind ``pipx upgrade`` is silently useless.

Resolution order, matching the shapes the tools actually leave on disk:

1. ``uv tool`` - the running command resolves under ``<uv-tools>/blumkin``.
2. ``pipx`` - the running command resolves inside a ``.../venvs/blumkin`` venv
   layout, or ``pipx list --json`` lists a ``blumkin`` venv whose app paths
   resolve to the running command.
3. source checkout - not tool-managed, but running from a git checkout of
   blumkin (``uv run`` / a bare ``pip install -e`` into a project venv).
4. unmanaged - a plain venv or a system install; ``upgrade`` has nothing to do.

For 1 and 2, PEP 610 ``<dist-info>/direct_url.json`` (``dir_info.editable``) is
the authoritative editable-vs-PyPI signal - a plain ``pipx install /abs/dir`` is
*not* editable.

Never raises: every probe is best-effort and falls back to ``unmanaged``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from blumkin.version import PACKAGE_NAME, is_source_checkout, running_command_path

GIT_QUERY_TIMEOUT_S = 5
METHOD_EDITABLE_PIPX = "editable-pipx"
METHOD_EDITABLE_UV = "editable-uv"
METHOD_PIPX = "pipx"
METHOD_SOURCE_CHECKOUT = "source-checkout"
METHOD_UNMANAGED = "unmanaged"
METHOD_UV_TOOL = "uv-tool"
TOOL_QUERY_TIMEOUT_S = 20


@dataclass(frozen=True, slots=True)
class Checkout:
    """The git checkout an editable / source install imports from."""

    behind_origin: int | None
    branch: str | None
    dirty: bool | None
    head: str | None
    path: Path

    def as_dict(self) -> dict[str, object]:
        """The ``checkout`` object for ``upgrade --json`` / ``doctor --json``."""
        return {
            "behind_origin": self.behind_origin,
            "branch": self.branch,
            "dirty": self.dirty,
            "head": self.head,
            "path": str(self.path),
        }


@dataclass(frozen=True, slots=True)
class Install:
    """How the ``blumkin`` the operator's shell resolves is installed."""

    checkout: Checkout | None
    managed_path: Path | None
    method: str

    @property
    def manager(self) -> str | None:
        """``"pipx"`` / ``"uv"`` for a tool-managed install, else ``None``."""
        if self.method in _PIPX_METHODS:
            return "pipx"
        if self.method in _UV_METHODS:
            return "uv"
        return None


def detect_install(
    *,
    command_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Install:
    """Return how the ``blumkin`` on PATH is installed. Never raises."""
    env = os.environ if environ is None else environ
    path = command_path if command_path is not None else running_command_path()
    try:
        return _detect(env=env, path=path)
    except Exception:
        # The probes above guard their own I/O; this is a last resort so an
        # unforeseen shape can never crash `doctor` / `upgrade`.
        return Install(checkout=None, managed_path=path, method=METHOD_UNMANAGED)


def inspect_checkout(path: Path) -> Checkout:
    """Branch, HEAD, cleanliness, and ``behind origin`` for the checkout at ``path``."""
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    porcelain = _git(path, "status", "--porcelain")
    return Checkout(
        behind_origin=_behind_origin(path),
        branch=branch or None,
        dirty=None if porcelain is None else bool(porcelain.strip()),
        head=_git(path, "rev-parse", "--short=12", "HEAD") or None,
        path=path,
    )


def metadata_stale(checkout: Path) -> tuple[str, str] | None:
    """Return ``(installed, declared)`` when the baked ``.dist-info`` version does
    not match the checkout's ``pyproject.toml`` - a checkout moved (pulled, or an
    older tag checked out) without a reinstall to re-bake the metadata.

    ``None`` when the normalised versions are equal (the checkout may still be
    ahead by commits at the same version - that is coherent), or when either
    version cannot be read.
    """
    try:
        installed = version(PACKAGE_NAME)
    except PackageNotFoundError:
        return None
    declared = _pyproject_version(checkout / "pyproject.toml")
    if declared is None:
        return None
    installed_key, declared_key = _aligned(_version_key(installed), _version_key(declared))
    if installed_key != declared_key:
        return (installed, declared)
    return None


def suggested_commands(install: Install) -> list[str]:
    """``upgrade_steps`` rendered as shell-quoted, copy-pasteable command lines.

    Both this and ``cli._editable_upgrade_steps`` build from ``upgrade_steps`` so
    the printed / ``action_taken`` text and the argv actually run cannot drift.
    """
    return [shlex.join(step) for step in upgrade_steps(install)]


def upgrade_steps(install: Install) -> list[list[str]]:
    """argv lists that would advance ``install`` - empty for a package upgrade.

    An editable / source install needs a ``git pull`` then a reinstall to
    re-bake the ``.dist-info`` metadata and entry points: a ``--force`` tool
    reinstall for an ``-e`` pipx / uv-tool install, or ``uv sync`` for a bare
    source checkout that is a uv project (has ``uv.lock``).
    """
    if install.checkout is None:
        return []
    path = str(install.checkout.path)
    steps: list[list[str]] = [["git", "-C", path, "pull", "--ff-only"]]
    if install.manager == "uv":
        steps.append(["uv", "tool", "install", "-e", path, "--force"])
    elif install.manager == "pipx":
        steps.append(["pipx", "install", "-e", path, "--force"])
    elif (install.checkout.path / "uv.lock").is_file():
        steps.append(["uv", "sync", "--project", path])
    return steps


_PIPX_METHODS = frozenset({METHOD_EDITABLE_PIPX, METHOD_PIPX})
_UV_METHODS = frozenset({METHOD_EDITABLE_UV, METHOD_UV_TOOL})


def _aligned(
    left: tuple[int, ...], right: tuple[int, ...]
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    width = max(len(left), len(right))
    return (left + (0,) * (width - len(left)), right + (0,) * (width - len(right)))


def _app_paths(raw: object) -> set[Path]:
    """Resolved app paths from a ``pipx list --json`` ``main_package.app_paths``.

    pipx records a flat list of path strings; tolerate a nested ``[bin, man]``
    pair shape too so a pipx format change fails closed (empty set -> caller
    declines to claim pipx) rather than open.
    """
    if not isinstance(raw, list):
        return set()
    flat = (
        item
        for entry in raw
        for item in (entry if isinstance(entry, list) else [entry])
        if isinstance(item, str)
    )
    return {_resolve(Path(item)) for item in flat}


def _behind_origin(path: Path) -> int | None:
    upstream = _git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if not upstream:
        branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
        upstream = f"origin/{branch}" if branch and branch != "HEAD" else None
    if not upstream:
        return None
    count = _git(path, "rev-list", "--count", f"HEAD..{upstream}")
    if count is None or not count.isdigit():
        return None
    return int(count)


def _detect(*, env: Mapping[str, str], path: Path) -> Install:
    target = _resolve(path)
    return (
        _detect_uv_tool(env=env, path=path, target=target)
        or _detect_pipx(path=path, target=target)
        or (
            Install(
                checkout=inspect_checkout(_repository_root()),
                managed_path=path,
                method=METHOD_SOURCE_CHECKOUT,
            )
            if is_source_checkout()
            else Install(checkout=None, managed_path=path, method=METHOD_UNMANAGED)
        )
    )


def _detect_pipx(*, path: Path, target: Path) -> Install | None:
    venv = _pipx_venv(target)
    if venv is None:
        return None
    source = _editable_source(venv)
    if source is not None:
        return Install(
            checkout=inspect_checkout(source), managed_path=path, method=METHOD_EDITABLE_PIPX
        )
    return Install(checkout=None, managed_path=path, method=METHOD_PIPX)


def _detect_uv_tool(*, env: Mapping[str, str], path: Path, target: Path) -> Install | None:
    tool_root = _uv_tools_dir(env) / PACKAGE_NAME
    if not target.is_relative_to(_resolve(tool_root)):
        return None
    # `target` resolves inside uv's own tools dir for blumkin - authoritative
    # (`uv tool upgrade` / reinstall is the right action either way). direct_url
    # only refines editable-vs-PyPI.
    source = _editable_source(tool_root)
    if source is not None:
        return Install(
            checkout=inspect_checkout(source), managed_path=path, method=METHOD_EDITABLE_UV
        )
    return Install(checkout=None, managed_path=path, method=METHOD_UV_TOOL)


def _dist_infos(venv_root: Path) -> Iterator[Path]:
    for site in (
        *venv_root.glob("lib/python*/site-packages"),
        venv_root / "Lib" / "site-packages",  # Windows layout
    ):
        yield from site.glob(f"{PACKAGE_NAME}-*.dist-info")


def _editable_source(venv_root: Path) -> Path | None:
    """The project dir of an editable install, from PEP 610 ``direct_url.json``.

    The authoritative editable signal - pip (and so pipx / uv) writes
    ``dir_info.editable`` there. Returns the source dir only when that flag is
    truthy and the ``file://`` URL points at an existing directory; a plain
    ``pipx install /abs/dir`` (non-editable) has ``editable: false`` and yields
    ``None``, so it is not silently converted to an ``-e`` install.
    """
    for dist_info in _dist_infos(venv_root):
        data = _read_json(dist_info / "direct_url.json")
        if not _mapping(data.get("dir_info")).get("editable"):
            continue
        url = data.get("url")
        if isinstance(url, str) and url.startswith("file://"):
            source = Path(unquote(urlsplit(url).path))
            if source.is_absolute() and source.is_dir():
                return source
    return None


def _git(path: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=GIT_QUERY_TIMEOUT_S,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _mapping(value: object) -> dict[str, Any]:
    """``value`` when it is a dict, else ``{}`` - so ``.get`` chains never raise
    on an unexpected ``pipx list --json`` / TOML shape (a list, a scalar)."""
    return value if isinstance(value, dict) else {}


def _pipx_venv(target: Path) -> Path | None:
    """The pipx venv directory that owns ``target``, or None.

    By disk layout first (``<pipx-home>/venvs/blumkin/{bin,Scripts}/...`` with a
    ``pyvenv.cfg``) - so a real pipx install is still recognised when the
    ``pipx`` binary is not on PATH (trimmed PATH, absolute-path call). Otherwise
    via ``pipx list --json``, requiring an app-path match so a shadowed pipx
    blumkin is not claimed as the one on PATH.
    """
    if target.parent.name in ("bin", "Scripts"):
        root = target.parent.parent
        if (
            root.name == PACKAGE_NAME
            and root.parent.name == "venvs"
            and (root / "pyvenv.cfg").is_file()
        ):
            return root
    pipx_bin = shutil.which("pipx")
    if pipx_bin is None:
        return None
    listed = _mapping(_run_json([pipx_bin, "list", "--json"]))
    metadata = _mapping(_mapping(_mapping(listed.get("venvs")).get(PACKAGE_NAME)).get("metadata"))
    app_paths = _app_paths(_mapping(metadata.get("main_package")).get("app_paths"))
    return target.parent.parent if target in app_paths else None


def _pyproject_version(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    except OSError, tomllib.TOMLDecodeError:
        return None
    value = _mapping(loaded.get("project")).get("version")
    return value if isinstance(value, str) else None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            parsed = json.load(handle)
    except OSError, ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _run_json(cmd: list[str]) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            text=True,
            timeout=TOOL_QUERY_TIMEOUT_S,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if completed.returncode != 0:
        return None
    try:
        parsed = json.loads(completed.stdout or "")
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _uv_tools_dir(env: Mapping[str, str]) -> Path:
    override = env.get("UV_TOOL_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    xdg_data = env.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share"
    return base / "uv" / "tools"


def _version_key(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in raw.split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)
