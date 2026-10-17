"""Regression guard for the `.me` placeholder path (issues #354, #387, #428).

With msgraph-core older than 1.5.2 and microsoft-kiota-http 1.13 or newer, the
Graph transport skipped its middleware, so a `.me` call went out as
`/users/me-token-to-replace` and Graph answered 404. This drives the real client
built by `create_graph_client` against a fake transport, with no network.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from kiota_abstractions.authentication.anonymous_authentication_provider import (
    AnonymousAuthenticationProvider,
)
from msgraph_core import GraphClientFactory

from blumkin.graph import create_graph_client

_PLACEHOLDER = "me-token-to-replace"


def test_me_call_is_rewritten_from_the_placeholder_path(monkeypatch: pytest.MonkeyPatch) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200, json={"id": "offline-test"}, headers={"content-type": "application/json"}
        )

    real_factory = GraphClientFactory.create_with_default_middleware

    def factory_with_fake_transport(*, options=None, **_unused):
        return real_factory(
            options=options, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )

    monkeypatch.setattr(
        "blumkin.graph.load_config",
        lambda: SimpleNamespace(
            client_id="x", graph_timeout_seconds=45.0, files_scopes=False, wo1162425_scopes=False
        ),
    )
    monkeypatch.setattr("blumkin.graph.effective_scopes", lambda _cfg: ["User.Read"])
    monkeypatch.setattr("blumkin.graph.create_credential", lambda _cfg: MagicMock())
    monkeypatch.setattr(
        "blumkin.graph.AzureIdentityAuthenticationProvider",
        lambda _credential, scopes: AnonymousAuthenticationProvider(),
    )
    monkeypatch.setattr(
        "blumkin.graph.GraphClientFactory.create_with_default_middleware",
        staticmethod(factory_with_fake_transport),
    )

    client = create_graph_client()
    asyncio.run(client.me.get())

    assert paths == ["/v1.0/me"], f"Expected the placeholder to become /v1.0/me, got {paths}"
    assert _PLACEHOLDER not in paths[0]
