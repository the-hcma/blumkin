"""Shared pytest fixtures for hermetic blumkin tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _force_file_secret_backend(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never let the test suite touch the real OS keychain (issue #287).

    ``token_storage`` defaults to ``"auto"``, which prefers a real keyring
    backend when one is usable - on a developer's macOS laptop that is the
    login Keychain. Without this, tests that assert on the on-disk secret
    file would instead read/write the operator's actual Keychain items.
    Tests that specifically exercise the keyring path install their own fake
    backend via ``blumkin.secret_store`` and don't need this guard.
    """
    if request.node.get_closest_marker("live") is not None:
        return
    from blumkin import cli, secret_store

    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    # `doctor`'s macOS-keychain-missing warning (issue #308) is about a *real*
    # absence (stale install, unreachable backend) - not this fixture's own
    # stubbing of `_keyring_module` above, which would otherwise make every
    # hermetic test on a macOS runner look like it has no keychain. Tests that
    # specifically exercise that warning patch `cli.macos_keychain_missing`
    # back themselves.
    monkeypatch.setattr(cli, "macos_keychain_missing", lambda cfg: False)


@pytest.fixture(autouse=True)
def _isolate_default_blumkin_config(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provide a minimal config.toml so missing-file count=0 does not break wiring tests.

    Tests that set ``BLUMKIN_CONFIG_DIR`` themselves (including empty dirs) override
    this. Live tests keep the operator's real config directory.
    """
    if request.node.get_closest_marker("live") is not None:
        return
    root = tmp_path_factory.mktemp("blumkin-default-config")
    (root / "config.toml").write_text(
        '[profiles.default]\nclient_id = "test-client"\n'
        'tenant_id = "contoso.com"\ndefault_tz = "UTC"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(root))


@pytest.fixture(autouse=True)
def _stub_install_detection(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep `blumkin doctor` / `upgrade` off real `pipx` / `git` subprocesses.

    `detect_install` shells out to `pipx list --json` and `git`; every command
    that reports build state calls it. Tests that exercise detection itself use
    `blumkin.install_method` directly (untouched here) or override
    `blumkin.cli.detect_install` with their own fixture.
    """
    if request.node.get_closest_marker("live") is not None:
        return
    from blumkin import cli, install_method

    monkeypatch.setattr(
        cli,
        "detect_install",
        lambda **_kwargs: install_method.Install(
            checkout=None,
            managed_path=Path("/usr/bin/blumkin"),
            method=install_method.METHOD_UNMANAGED,
        ),
    )
