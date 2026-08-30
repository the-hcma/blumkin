"""Shared Google HTTP: timeouts + bounded retries for discovery clients."""

from __future__ import annotations

from typing import Any

import google_auth_httplib2
import httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import HttpRequest

from blumkin.config import BlumkinConfig

DEFAULT_HTTP_RETRIES = 3


def authorized_http(
    creds: Credentials, config: BlumkinConfig
) -> google_auth_httplib2.AuthorizedHttp:
    """httplib2 transport with connect/read timeout from config."""
    timeout_s = float(config.graph_timeout_seconds)
    return google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout_s))


def build_api_service(
    api: str,
    version: str,
    *,
    creds: Credentials,
    config: BlumkinConfig,
) -> Any:
    """Build a discovery client on the timed AuthorizedHttp."""
    return build(
        api,
        version,
        http=authorized_http(creds, config),
        cache_discovery=False,
    )


def execute(request: HttpRequest, *, num_retries: int = DEFAULT_HTTP_RETRIES) -> Any:
    """Run a discovery request with bounded retries for transient failures."""
    return request.execute(num_retries=num_retries)


def refresh_request(config: BlumkinConfig) -> Request:
    """google-auth transport Request with a default per-call timeout."""
    timeout_s = float(config.graph_timeout_seconds)

    class _TimedRequest(Request):
        def __call__(  # type: ignore[override]
            self,
            url,
            method="GET",
            body=None,
            headers=None,
            timeout=None,
            **kwargs,
        ):
            return super().__call__(
                url,
                method=method,
                body=body,
                headers=headers,
                timeout=timeout_s if timeout is None else timeout,
                **kwargs,
            )

    return _TimedRequest()
