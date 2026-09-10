"""Hermetic install-method detection for `blumkin upgrade` / `doctor` (issue #239)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from blumkin import install_method
from blumkin.install_method import (
    METHOD_EDITABLE_PIPX,
    METHOD_EDITABLE_UV,
    METHOD_PIPX,
    METHOD_SOURCE_CHECKOUT,
    METHOD_UNMANAGED,
    METHOD_UV_TOOL,
    Checkout,
    Install,
    detect_install,
    inspect_checkout,
    metadata_stale,
    suggested_commands,
)


@pytest.fixture(autouse=True)
def _not_a_source_checkout(monkeypatch):
    """Default off; the running test process itself sits in the blumkin checkout."""
    monkeypatch.setattr(install_method, "is_source_checkout", lambda **_k: False)


def _pipx_list(app_paths: list):
    payload = {"venvs": {"blumkin": {"metadata": {"main_package": {"app_paths": app_paths}}}}}
    return subprocess.CompletedProcess(args=["pipx"], returncode=0, stdout=json.dumps(payload))


def _write_direct_url(venv_root: Path, *, editable: bool, source: Path | None = None) -> None:
    """PEP 610 record pip writes for a `file://` install (pipx / uv both do)."""
    dist_info = venv_root / "lib" / "python3.14" / "site-packages" / "blumkin-0.6.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "direct_url.json").write_text(
        json.dumps(
            {
                "url": (source or venv_root).as_uri(),
                "dir_info": {"editable": editable},
            }
        )
    )


# --- uv tool -----------------------------------------------------------------


def test_uv_tool_from_pypi(tmp_path, monkeypatch) -> None:
    tools = tmp_path / "uv" / "tools"
    (tools / "blumkin" / "bin").mkdir(parents=True)
    app = tools / "blumkin" / "bin" / "blumkin"
    app.write_text("#!/bin/sh\n")
    (tools / "blumkin" / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "blumkin", specifier = "==0.6.0" }]\n'
    )
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: None)

    result = detect_install(command_path=app, environ={"XDG_DATA_HOME": str(tmp_path), "PATH": ""})
    assert result.method == METHOD_UV_TOOL
    assert result.checkout is None
    assert result.manager == "uv"


def test_uv_tool_editable_reads_direct_url(tmp_path, monkeypatch) -> None:
    checkout = tmp_path / "src" / "blumkin"
    checkout.mkdir(parents=True)
    tools = tmp_path / "uv" / "tools"
    (tools / "blumkin" / "bin").mkdir(parents=True)
    app = tools / "blumkin" / "bin" / "blumkin"
    app.write_text("#!/bin/sh\n")
    _write_direct_url(tools / "blumkin", editable=True, source=checkout)
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: None)

    result = detect_install(command_path=app, environ={"XDG_DATA_HOME": str(tmp_path)})
    assert result.method == METHOD_EDITABLE_UV
    assert result.checkout is not None
    assert result.checkout.path == checkout


def test_uv_tool_dir_override_is_honoured(tmp_path, monkeypatch) -> None:
    tools = tmp_path / "custom-tools"
    (tools / "blumkin").mkdir(parents=True)
    app = tools / "blumkin" / "blumkin"
    app.write_text("#!/bin/sh\n")
    (tools / "blumkin" / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "blumkin" }]\n'
    )
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: None)

    result = detect_install(command_path=app, environ={"UV_TOOL_DIR": str(tools)})
    assert result.method == METHOD_UV_TOOL


def test_uv_tool_string_requirement_shape(tmp_path, monkeypatch) -> None:
    """A future `requirements = ["blumkin==0.6.0"]` shape still counts as uv-tool."""
    tools = tmp_path / "uv" / "tools"
    (tools / "blumkin").mkdir(parents=True)
    app = tools / "blumkin" / "blumkin"
    app.write_text("#!/bin/sh\n")
    (tools / "blumkin" / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = ["blumkin[mcp]==0.6.0"]\n'
    )
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: None)

    assert detect_install(command_path=app, environ={"XDG_DATA_HOME": str(tmp_path)}).method == (
        METHOD_UV_TOOL
    )


def test_uv_tools_path_is_authoritative_without_a_readable_receipt(tmp_path, monkeypatch) -> None:
    """`target` inside uv's tools dir for blumkin -> uv-tool even if the receipt is gone."""
    tools = tmp_path / "uv" / "tools"
    (tools / "blumkin").mkdir(parents=True)
    app = tools / "blumkin" / "blumkin"
    app.write_text("#!/bin/sh\n")  # no uv-receipt.toml
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: None)

    result = detect_install(command_path=app, environ={"XDG_DATA_HOME": str(tmp_path)})
    assert result.method == METHOD_UV_TOOL
    assert result.checkout is None


def test_uv_tool_detected_through_a_path_symlink(tmp_path, monkeypatch) -> None:
    """The real #239 shape: ~/.local/bin/blumkin -> <uv-tools>/blumkin/bin/blumkin."""
    tools = tmp_path / "uv" / "tools"
    (tools / "blumkin" / "bin").mkdir(parents=True)
    app = tools / "blumkin" / "bin" / "blumkin"
    app.write_text("#!/bin/sh\n")
    (tools / "blumkin" / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "blumkin" }]\n'
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    link = bindir / "blumkin"
    link.symlink_to(app)
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: None)

    assert (
        detect_install(command_path=link, environ={"XDG_DATA_HOME": str(tmp_path)}).method
        == METHOD_UV_TOOL
    )


# --- pipx ------------------------------------------------------------------


def test_pipx_from_pypi(tmp_path, monkeypatch) -> None:
    app = tmp_path / "venvs" / "blumkin" / "bin" / "blumkin"
    app.parent.mkdir(parents=True)
    app.write_text("#!/bin/sh\n")
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: "/usr/bin/pipx")
    monkeypatch.setattr(install_method.subprocess, "run", lambda *_a, **_k: _pipx_list([str(app)]))

    result = detect_install(command_path=app, environ={})
    assert result.method == METHOD_PIPX
    assert result.manager == "pipx"


def _pipx_venv(tmp_path: Path) -> Path:
    """A real pipx venv layout on disk; returns the console-script path."""
    venv = tmp_path / "pipx" / "venvs" / "blumkin"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    app = venv / "bin" / "blumkin"
    app.write_text("#!/bin/sh\n")
    return app


def test_pipx_detected_structurally_without_a_pipx_binary(tmp_path, monkeypatch) -> None:
    """A pipx install still resolving on PATH while `pipx` itself does not (#239 round 2)."""
    app = _pipx_venv(tmp_path)
    link = tmp_path / "bin" / "blumkin"
    link.parent.mkdir()
    link.symlink_to(app)
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: None)

    result = detect_install(command_path=link, environ={})
    assert result.method == METHOD_PIPX
    assert result.checkout is None


def test_pipx_editable_reads_direct_url(tmp_path, monkeypatch) -> None:
    checkout = tmp_path / "src" / "blumkin"
    checkout.mkdir(parents=True)
    app = _pipx_venv(tmp_path)
    _write_direct_url(app.parent.parent, editable=True, source=checkout)
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: None)

    result = detect_install(command_path=app, environ={})
    assert result.method == METHOD_EDITABLE_PIPX
    assert result.checkout is not None and result.checkout.path == checkout


def test_pipx_local_install_is_not_editable(tmp_path, monkeypatch) -> None:
    """`pipx install /abs/dir` (non-editable) has direct_url editable=false."""
    local = tmp_path / "unpacked-sdist"
    local.mkdir()
    app = _pipx_venv(tmp_path)
    _write_direct_url(app.parent.parent, editable=False, source=local)
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: None)

    result = detect_install(command_path=app, environ={})
    assert result.method == METHOD_PIPX  # plain pipx, no git-pull-in-the-wrong-tree
    assert result.checkout is None


def test_pipx_lists_blumkin_but_not_the_one_on_path(tmp_path, monkeypatch) -> None:
    """A uv-tool / hand-made symlink shadows a pipx blumkin - do not claim pipx."""
    on_path = tmp_path / "elsewhere" / "blumkin"
    on_path.parent.mkdir(parents=True)
    on_path.write_text("#!/bin/sh\n")
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: "/usr/bin/pipx")
    monkeypatch.setattr(
        install_method.subprocess,
        "run",
        lambda *_a, **_k: _pipx_list(["/home/u/.local/pipx/venvs/blumkin/bin/blumkin"]),
    )

    result = detect_install(command_path=on_path, environ={})
    assert result.method == METHOD_UNMANAGED


def test_pipx_tolerates_a_nested_app_paths_pair_shape(tmp_path, monkeypatch) -> None:
    """A [bin, man] pair shape must still resolve to the pipx bin path."""
    app = tmp_path / "venvs" / "blumkin" / "bin" / "blumkin"
    app.parent.mkdir(parents=True)
    app.write_text("#!/bin/sh\n")
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: "/usr/bin/pipx")
    monkeypatch.setattr(
        install_method.subprocess,
        "run",
        lambda *_a, **_k: _pipx_list([[str(app), "/usr/share/man/man1/blumkin.1"]]),
    )

    assert detect_install(command_path=app, environ={}).method == METHOD_PIPX


def test_pipx_declines_when_app_paths_are_unreadable(tmp_path, monkeypatch) -> None:
    """No confirmable app-path match -> do not claim pipx (fail closed, #239)."""
    app = tmp_path / "venvs" / "blumkin" / "bin" / "blumkin"
    app.parent.mkdir(parents=True)
    app.write_text("#!/bin/sh\n")
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: "/usr/bin/pipx")
    monkeypatch.setattr(install_method.subprocess, "run", lambda *_a, **_k: _pipx_list([]))

    assert detect_install(command_path=app, environ={}).method == METHOD_UNMANAGED


def test_detect_install_never_raises_on_junk_pipx_json(tmp_path, monkeypatch) -> None:
    app = tmp_path / "blumkin"
    app.write_text("#!/bin/sh\n")
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: "/usr/bin/pipx")
    monkeypatch.setattr(
        install_method.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=["pipx"], returncode=0, stdout='{"venvs": [1, 2, 3]}'
        ),
    )

    assert detect_install(command_path=app, environ={}).method == METHOD_UNMANAGED


# --- source checkout / unmanaged ----------------------------------------


def test_unmanaged_when_nothing_claims_it(tmp_path, monkeypatch) -> None:
    app = tmp_path / "blumkin"
    app.write_text("#!/bin/sh\n")
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: None)

    result = detect_install(command_path=app, environ={})
    assert result.method == METHOD_UNMANAGED
    assert result.manager is None


def test_source_checkout_when_running_from_a_git_tree(tmp_path, monkeypatch) -> None:
    app = tmp_path / ".venv" / "bin" / "blumkin"
    app.parent.mkdir(parents=True)
    app.write_text("#!/bin/sh\n")
    monkeypatch.setattr(install_method.shutil, "which", lambda _n: None)
    monkeypatch.setattr(install_method, "is_source_checkout", lambda **_k: True)

    result = detect_install(command_path=app, environ={})
    assert result.method == METHOD_SOURCE_CHECKOUT
    assert result.checkout is not None


# --- inspect_checkout (one real git repo) ------------------------------


def test_inspect_checkout_reads_a_real_repo(tmp_path) -> None:
    def _git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "Test")
    (tmp_path / "f.txt").write_text("hi\n")
    _git("add", "f.txt")
    _git("commit", "-qm", "first")

    checkout = inspect_checkout(tmp_path)
    assert checkout.branch == "main"
    assert checkout.dirty is False
    assert checkout.head is not None and len(checkout.head) == 12
    assert checkout.behind_origin is None  # no remote configured

    (tmp_path / "f.txt").write_text("changed\n")
    assert inspect_checkout(tmp_path).dirty is True


# --- metadata_stale ---------------------------------------------------------


def _pyproject(tmp_path: Path, version: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "blumkin"\nversion = "{version}"\n'
    )
    return tmp_path


def test_metadata_stale_flags_a_pulled_but_not_reinstalled_tree(tmp_path) -> None:
    stale = metadata_stale(_pyproject(tmp_path, "999.0.0"))
    assert stale is not None
    installed, declared = stale
    assert declared == "999.0.0"
    assert installed  # the real installed blumkin version


def test_metadata_stale_none_when_versions_match(tmp_path) -> None:
    from blumkin.version import package_version

    assert metadata_stale(_pyproject(tmp_path, package_version())) is None


def test_metadata_stale_flags_a_mismatch_in_either_direction(tmp_path) -> None:
    """An older tag checked out without a reinstall is just as incoherent (#239)."""
    stale = metadata_stale(_pyproject(tmp_path, "0.0.1"))
    assert stale is not None and stale[1] == "0.0.1"


def test_metadata_stale_none_without_a_pyproject(tmp_path) -> None:
    assert metadata_stale(tmp_path) is None


# --- suggested_commands ---------------------------------------------------


@pytest.mark.parametrize(
    ("method", "second"),
    [
        (METHOD_EDITABLE_UV, "uv tool install -e /co --force"),
        (METHOD_EDITABLE_PIPX, "pipx install -e /co --force"),
        (METHOD_SOURCE_CHECKOUT, None),
    ],
)
def test_suggested_commands_by_method(method, second) -> None:
    install = Install(
        checkout=Checkout(
            behind_origin=1, branch="main", dirty=False, head="a" * 12, path=Path("/co")
        ),
        managed_path=Path("/bin/blumkin"),
        method=method,
    )
    commands = suggested_commands(install)
    assert commands[0] == "git -C /co pull --ff-only"
    assert (commands[1:] or [None])[0] == second


def test_suggested_commands_empty_for_a_package_install() -> None:
    install = Install(checkout=None, managed_path=Path("/bin/blumkin"), method=METHOD_PIPX)
    assert suggested_commands(install) == []


def test_suggested_commands_shell_quote_paths_with_spaces() -> None:
    """`/Users/Jane Doe/src/blumkin` is normal on macOS - the printed lines must paste."""
    install = Install(
        checkout=Checkout(
            behind_origin=0,
            branch="main",
            dirty=False,
            head="a" * 12,
            path=Path("/Users/Jane Doe/src/blumkin"),
        ),
        managed_path=Path("/bin/blumkin"),
        method=METHOD_EDITABLE_UV,
    )
    assert suggested_commands(install) == [
        "git -C '/Users/Jane Doe/src/blumkin' pull --ff-only",
        "uv tool install -e '/Users/Jane Doe/src/blumkin' --force",
    ]


# --- doctor surfaces the method and the coherence check ----------------


def test_doctor_reports_method_and_warns_on_stale_metadata(tmp_path, monkeypatch) -> None:
    from click.testing import CliRunner

    from blumkin import cli
    from blumkin.cli import main

    editable = Install(
        checkout=Checkout(
            behind_origin=1, branch="main", dirty=False, head="a" * 12, path=Path("/co")
        ),
        managed_path=Path("/home/u/.local/bin/blumkin"),
        method=METHOD_EDITABLE_UV,
    )
    monkeypatch.setattr(cli, "detect_install", lambda **_k: editable)
    monkeypatch.setattr(cli, "metadata_stale", lambda _p: ("0.5.0", "0.6.0"))
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'client_id = "fake-google-desktop-client.apps.googleusercontent.com"\n'
        'provider = "google"\n'
        'default_tz = "UTC"\n'
    )

    payload = json.loads(CliRunner().invoke(main, ["doctor", "--json"]).output)
    assert payload["install"]["method"] == "editable-uv"
    assert payload["install"]["metadata_stale"] is True
    assert any("stale vs the checkout (0.6.0)" in w for w in payload["warnings"])
    assert any("uv tool install -e /co --force" in w for w in payload["warnings"])
