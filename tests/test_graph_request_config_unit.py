"""Request configuration shared by every Graph call."""

from __future__ import annotations

from blumkin.graph import request_config


def test_request_config_carries_query_parameters() -> None:
    query = object()

    assert request_config(query).query_parameters is query


def test_request_config_adds_the_requested_headers() -> None:
    config = request_config(None, headers={"Prefer": 'outlook.body-content-type="text"'})

    assert config.headers.get("prefer") == {'outlook.body-content-type="text"'}


def test_request_config_does_not_leak_headers_between_calls() -> None:
    """RequestConfiguration shares one HeadersCollection across instances by default.

    Without an explicit collection per config, a Prefer header set for one request
    would ride along on every later request in the process.
    """
    first = request_config(None, headers={"Prefer": 'outlook.body-content-type="text"'})
    second = request_config(None, headers={"Prefer": 'outlook.body-content-type="html"'})
    third = request_config(None)

    assert first.headers.get("prefer") == {'outlook.body-content-type="text"'}
    assert second.headers.get("prefer") == {'outlook.body-content-type="html"'}
    assert not third.headers.keys()
