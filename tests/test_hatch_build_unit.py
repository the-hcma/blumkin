"""Graceful-degradation branches of the `hatch_build.py` build hook (issue #328).

Nothing else in the suite imports `hatch_build` (it only runs as part of a
real `uv build`/`pip install`), so these are the only tests exercising the
"ship without the binary" promise in the module docstring: a non-macOS
platform, a missing `cargo`, and the universal2 -> native-arch fallback.
`AgentBuildHook` is constructed without calling hatchling's own
`BuildHookInterface.__init__` (which needs a full `ProjectMetadata` /
`BuilderConfigBound` from a real build) - only the handful of attributes
`initialize`/`_build_binary` actually read are set on a bare instance.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_HATCH_BUILD_PATH = Path(__file__).resolve().parent.parent / "hatch_build.py"
_spec = importlib.util.spec_from_file_location("hatch_build", _HATCH_BUILD_PATH)
assert _spec is not None and _spec.loader is not None
hatch_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hatch_build)


class _FakeApp:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def display_info(self, message: str) -> None:
        self.infos.append(message)

    def display_warning(self, message: str) -> None:
        self.warnings.append(message)


class _FakeMetadata:
    version = "0.0.0"


@pytest.fixture
def _project_root() -> Iterator[Path]:
    root = tempfile.mkdtemp(dir="/tmp")
    try:
        yield Path(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _make_hook(root: Path) -> Any:
    hook = hatch_build.AgentBuildHook.__new__(hatch_build.AgentBuildHook)
    # `root`/`app`/`metadata` are read-only properties backed by
    # name-mangled private attributes set in `BuildHookInterface.__init__`
    # (which needs a full hatchling `ProjectMetadata`/`BuilderConfigBound`
    # this test has no reason to construct) - set them directly instead.
    hook._BuildHookInterface__root = str(root)  # noqa: SLF001
    hook._BuildHookInterface__app = _FakeApp()  # noqa: SLF001
    hook._BuildHookInterface__metadata = _FakeMetadata()  # noqa: SLF001
    return hook


def test_initialize_skips_the_build_on_a_non_darwin_platform(
    monkeypatch: pytest.MonkeyPatch, _project_root: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    hook = _make_hook(_project_root)
    build_data: dict = {}

    hook.initialize("0.0.0", build_data)

    assert build_data == {}
    assert hook.app.infos


def test_initialize_ships_without_a_binary_when_cargo_is_missing(
    monkeypatch: pytest.MonkeyPatch, _project_root: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    hook = _make_hook(_project_root)
    build_data: dict = {}

    hook.initialize("0.0.0", build_data)

    assert build_data == {}
    assert hook.app.warnings


def test_build_binary_falls_back_to_native_when_lipo_is_missing(
    monkeypatch: pytest.MonkeyPatch, _project_root: Path
) -> None:
    """`lipo` (unlike `cargo`) is never checked with `shutil.which` up front,
    so its absence surfaces as `FileNotFoundError` from `subprocess.run`
    rather than `CalledProcessError` - this must degrade the same way, not
    escape and hard-fail the install (see PR #329 review).
    """
    hook = _make_hook(_project_root)
    native_binary = _project_root / "native-binary"

    def _raise_file_not_found(*_args: object, **_kwargs: object) -> Path:
        raise FileNotFoundError("lipo")

    monkeypatch.setattr(hook, "_build_universal2", _raise_file_not_found)
    monkeypatch.setattr(hook, "_build_native", lambda *_a, **_k: native_binary)

    result = hook._build_binary("cargo")

    assert result == native_binary
    assert hook.app.warnings


def test_build_binary_falls_back_to_native_when_universal2_fails_to_build(
    monkeypatch: pytest.MonkeyPatch, _project_root: Path
) -> None:
    hook = _make_hook(_project_root)
    native_binary = _project_root / "native-binary"

    def _raise_called_process_error(*_args: object, **_kwargs: object) -> Path:
        raise subprocess.CalledProcessError(1, ["cargo"])

    monkeypatch.setattr(hook, "_build_universal2", _raise_called_process_error)
    monkeypatch.setattr(hook, "_build_native", lambda *_a, **_k: native_binary)

    result = hook._build_binary("cargo")

    assert result == native_binary
    assert hook.app.warnings


def test_build_binary_leaves_archflags_unset_on_the_native_fallback_path(
    monkeypatch: pytest.MonkeyPatch, _project_root: Path
) -> None:
    """The fallback native binary is single-arch, so it must keep its

    natural host-arch tag rather than being mistagged `universal2`. Pre-set
    a two-arch `ARCHFLAGS`, as a dev/CI box building universal Python
    itself might export, to pin that an *inherited* value is cleared on the
    fallback path - not just that this hook never sets one on a clean
    environment (see PR #329 review).
    """
    monkeypatch.setenv("ARCHFLAGS", hatch_build._UNIVERSAL2_ARCHFLAGS)
    hook = _make_hook(_project_root)
    native_binary = _project_root / "native-binary"

    def _raise_called_process_error(*_args: object, **_kwargs: object) -> Path:
        raise subprocess.CalledProcessError(1, ["cargo"])

    monkeypatch.setattr(hook, "_build_universal2", _raise_called_process_error)
    monkeypatch.setattr(hook, "_build_native", lambda *_a, **_k: native_binary)

    hook._build_binary("cargo")

    assert "ARCHFLAGS" not in os.environ


def test_build_binary_sets_archflags_on_a_successful_universal2_build(
    monkeypatch: pytest.MonkeyPatch, _project_root: Path
) -> None:
    """Pins the success path: this `ARCHFLAGS` assignment is what makes

    hatchling's `infer_tag` machinery emit a `universal2` platform tag
    instead of the host's native arch (see the `_UNIVERSAL2_ARCHFLAGS`
    module comment) - nothing else in the suite reaches it, since the other
    `_build_binary` tests all make `_build_universal2` raise (see PR #329
    review).
    """
    monkeypatch.delenv("ARCHFLAGS", raising=False)
    hook = _make_hook(_project_root)
    universal_binary = _project_root / "universal-binary"
    monkeypatch.setattr(hook, "_build_universal2", lambda *_a, **_k: universal_binary)

    result = hook._build_binary("cargo")

    assert result == universal_binary
    assert os.environ["ARCHFLAGS"] == hatch_build._UNIVERSAL2_ARCHFLAGS
    os.environ.pop("ARCHFLAGS", None)


def test_cargo_target_dir_honors_a_relocated_cargo_target_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-relocated target dir - `CARGO_TARGET_DIR`, or `[build]
    target-dir` in `~/.cargo/config.toml`, which cargo reads itself and a
    `dict(os.environ)` copy cannot see - must not leave `_build_native`/
    `_build_universal2` looking for the binary in cargo's *default*
    location; `cargo metadata` is the one source of truth that resolves
    both cases (see PR #329 review).
    """
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo not available in this environment")
    crate_dir = Path(__file__).resolve().parent.parent / "rust-agent"
    relocated = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        env = dict(os.environ)
        env["CARGO_TARGET_DIR"] = str(relocated)

        target_dir = hatch_build._cargo_target_dir(cargo, crate_dir, env)

        assert target_dir == relocated
    finally:
        shutil.rmtree(relocated, ignore_errors=True)


def test_cargo_target_dir_falls_back_to_the_conventional_path_when_cargo_is_unusable() -> None:
    """`cargo metadata` failing (missing `cargo`, or a `crate_dir` with no

    `Cargo.toml`) must degrade to the previous hardcoded assumption rather
    than raising and hard-failing the whole build.
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as no_crate_here:
        target_dir = hatch_build._cargo_target_dir("cargo", Path(no_crate_here), dict(os.environ))

    assert target_dir == Path(no_crate_here) / "target"


def test_initialize_bundles_the_binary_and_marks_the_wheel_platform_specific(
    monkeypatch: pytest.MonkeyPatch, _project_root: Path
) -> None:
    """Pins the success path: every other test here takes a degrade branch
    and returns before `build_data["pure_python"]`/`["infer_tag"]` are set,
    so nothing else would catch either key being dropped or renamed - which
    would silently ship a `py3-none-any` wheel without the daemon (see PR
    #329 review).
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/cargo")
    hook = _make_hook(_project_root)
    fake_binary = _project_root / "fake-blumkin-agent"
    fake_binary.write_bytes(b"#!/bin/sh\n")
    monkeypatch.setattr(hook, "_build_binary", lambda _cargo: fake_binary)
    build_data: dict = {}

    hook.initialize("0.0.0", build_data)

    target_binary = _project_root / "src" / "blumkin" / "agent" / "bin" / "blumkin-agent"
    assert target_binary.is_file()
    assert (target_binary.stat().st_mode & 0o777) == 0o755
    assert build_data["pure_python"] is False
    assert build_data["infer_tag"] is True
