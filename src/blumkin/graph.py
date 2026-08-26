"""Shared Microsoft Graph client."""

from __future__ import annotations

from azure.identity import InteractiveBrowserCredential
from kiota_abstractions.base_request_configuration import RequestConfiguration
from msgraph.graph_service_client import GraphServiceClient

from blumkin.auth import create_credential, effective_scopes
from blumkin.config import BlumkinConfig, load_config


def create_graph_client(
    config: BlumkinConfig | None = None,
    credential: InteractiveBrowserCredential | None = None,
) -> GraphServiceClient:
    cfg = config or load_config()
    cred = credential or create_credential(cfg)
    return GraphServiceClient(credentials=cred, scopes=effective_scopes(cfg))


def request_config(query_parameters) -> RequestConfiguration:
    return RequestConfiguration(query_parameters=query_parameters)
