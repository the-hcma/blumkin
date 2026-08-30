"""Shared pytest fixtures for hermetic blumkin tests."""

from __future__ import annotations

import pytest


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
        'client_id = "test-client"\ntenant_id = "contoso.com"\ndefault_tz = "UTC"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(root))
