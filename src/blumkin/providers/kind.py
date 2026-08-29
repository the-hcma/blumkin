"""Provider kind enum and config/env parsing."""

from __future__ import annotations

from enum import StrEnum


class ProviderKind(StrEnum):
    """Account / backend family for workspace skills."""

    MICROSOFT = "microsoft"


def parse_provider_kind(raw: str) -> ProviderKind:
    """Parse a config/env provider string.

    ``google`` is reserved and rejected until a Google adapter lands (#67 / #84).
    """
    key = raw.strip().lower()
    if key in {"", "microsoft", "ms", "m365", "graph"}:
        return ProviderKind.MICROSOFT
    if key == "google":
        raise ValueError(
            "provider 'google' is not implemented yet (see GitHub #67 / #84); "
            'use provider = "microsoft"'
        )
    raise ValueError(f"unknown provider {raw!r}; supported: microsoft")
