"""Unit tests for Graph client HTTP timeout wiring."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from blumkin.graph import create_graph_client


def test_create_graph_client_applies_configured_timeout(monkeypatch) -> None:
    monkeypatch.setattr(
        "blumkin.graph.load_config",
        lambda: SimpleNamespace(
            client_id="x",
            graph_timeout_seconds=45.0,
            files_scopes=False,
            wo1162425_scopes=False,
        ),
    )
    monkeypatch.setattr("blumkin.graph.effective_scopes", lambda _cfg: ["User.Read"])
    fake_cred = MagicMock()
    monkeypatch.setattr("blumkin.graph.create_credential", lambda _cfg: fake_cred)

    http = MagicMock()
    with (
        patch(
            "blumkin.graph.GraphClientFactory.create_with_default_middleware",
            return_value=http,
        ),
        patch("blumkin.graph.AzureIdentityAuthenticationProvider") as auth_ctor,
        patch("blumkin.graph.GraphRequestAdapter") as adapter_ctor,
        patch("blumkin.graph.GraphServiceClient") as client_ctor,
    ):
        create_graph_client()

    assert http.timeout.connect == 30.0
    assert http.timeout.read == 45.0
    auth_ctor.assert_called_once()
    adapter_ctor.assert_called_once()
    client_ctor.assert_called_once()
