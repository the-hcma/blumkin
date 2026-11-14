"""The macOS wheel keeps the `macosx_14_0_universal2` tag the release workflow pins.

`hatch_build.py` sets `ARCHFLAGS` for a universal2 build and `infer_tag` makes hatchling name the
wheel from the platform it runs on, which on a newer runner would otherwise raise the minimum
macOS version of the published wheel. The release workflow sets `MACOSX_DEPLOYMENT_TARGET=14.0`
and refuses a wheel with any other tag; this pins what hatchling does with those two variables.
"""

from __future__ import annotations

import importlib.util
import platform
from pathlib import Path

import pytest
from hatchling.builders.macos import process_macos_plat_tag


def _hook_archflags() -> str:
    """The `ARCHFLAGS` value the build hook sets, read from the hook (it is not importable)."""
    path = Path(__file__).resolve().parent.parent / "hatch_build.py"
    spec = importlib.util.spec_from_file_location("hatch_build_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module._UNIVERSAL2_ARCHFLAGS)


# What `sysconfig` reports on an Apple Silicon runner that runs a newer macOS than 14.
_RUNNER_PLATFORM = "macosx_15_0_arm64"


@pytest.fixture(autouse=True)
def _apple_silicon_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """hatchling cuts the platform tag at the host's own `platform.machine()`: model the runner."""
    monkeypatch.setattr(platform, "machine", lambda: "arm64")


def test_the_pinned_deployment_target_names_the_universal2_wheel_for_macos_14(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MACOSX_DEPLOYMENT_TARGET", "14.0")
    monkeypatch.setenv("ARCHFLAGS", _hook_archflags())

    assert process_macos_plat_tag(_RUNNER_PLATFORM, compat=False) == "macosx_14_0_universal2"


def test_without_the_pin_the_tag_follows_the_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MACOSX_DEPLOYMENT_TARGET", raising=False)
    monkeypatch.setenv("ARCHFLAGS", _hook_archflags())

    # The reason the workflow pins it: the minimum macOS would rise with the runner image.
    assert process_macos_plat_tag(_RUNNER_PLATFORM, compat=False) == "macosx_15_0_universal2"
