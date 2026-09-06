"""Shared Microsoft Graph client."""

from __future__ import annotations

import httpx
from azure.identity import InteractiveBrowserCredential
from kiota_abstractions.base_request_configuration import RequestConfiguration
from kiota_abstractions.headers_collection import HeadersCollection
from kiota_authentication_azure.azure_identity_authentication_provider import (
    AzureIdentityAuthenticationProvider,
)
from msgraph.generated.models.o_data_errors.o_data_error import ODataError
from msgraph.graph_request_adapter import GraphRequestAdapter, options
from msgraph.graph_service_client import GraphServiceClient
from msgraph_core import GraphClientFactory

from blumkin.auth import create_credential, effective_scopes
from blumkin.config import BlumkinConfig, load_config

# Graph's id-shaped complaints: "that id does not name anything", not "your query
# was malformed". A bare 400 is not enough - Graph also 400s a --top over its cap.
_ID_LOOKUP_ERROR_CODES = frozenset(
    {
        "errorfoldernotfound",
        "errorinvalididmalformed",
        "erroritemnotfound",
        "resourcenotfound",
    }
)


def create_graph_client(
    config: BlumkinConfig | None = None,
    credential: InteractiveBrowserCredential | None = None,
) -> GraphServiceClient:
    cfg = config or load_config()
    cred = credential or create_credential(cfg)
    scopes = effective_scopes(cfg)
    timeout_s = float(cfg.graph_timeout_seconds)
    connect_s = min(30.0, timeout_s)
    http_client = GraphClientFactory.create_with_default_middleware(options=options)
    http_client.timeout = httpx.Timeout(timeout_s, connect=connect_s)
    # Use kiota's provider (empty allowed_hosts = all hosts). msgraph_core's
    # default NationalClouds still include https:// URLs and fail AllowedHostsValidator.
    auth_provider = AzureIdentityAuthenticationProvider(cred, scopes=scopes)
    adapter = GraphRequestAdapter(auth_provider, client=http_client)
    return GraphServiceClient(request_adapter=adapter)


def is_id_lookup_failure(exc: ODataError) -> bool:
    """True when Graph rejected an id in the request path rather than the query.

    Lets callers turn a bad ``--id`` / ``--event-id`` into a clean ``not_found``
    instead of a generic Graph error, without mistaking a query-level 400 for a
    missing resource.
    """
    status = getattr(exc, "response_status_code", None)
    code = str(getattr(getattr(exc, "error", None), "code", "") or "").casefold()
    return code in _ID_LOOKUP_ERROR_CODES or status == 404


def request_config(
    query_parameters=None, *, headers: dict[str, str] | None = None
) -> RequestConfiguration:
    # RequestConfiguration declares `headers` as a bare dataclass default, so every
    # instance shares one HeadersCollection. Adding to it would leak headers into
    # unrelated requests for the life of the process; give each config its own.
    config = RequestConfiguration(
        headers=HeadersCollection(),
        query_parameters=query_parameters,
    )
    for name, value in (headers or {}).items():
        config.headers.add(name, value)
    return config
