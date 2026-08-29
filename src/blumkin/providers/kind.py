"""Provider kind enum and config parsing."""

from __future__ import annotations

from enum import StrEnum


class ProviderConfigError(ValueError):
    """Invalid or unimplemented ``provider`` value in config.toml."""


class ProviderKind(StrEnum):
    """Account / backend family for workspace skills."""

    MICROSOFT = "microsoft"


def parse_provider_kind(raw: str) -> ProviderKind:
    """Parse a config.toml ``provider`` string.

    Only ``microsoft`` is accepted today. ``google`` is reserved until an adapter
    lands (#67 / #84).
    """
    key = raw.strip().lower()
    if key in {"", "microsoft"}:
        return ProviderKind.MICROSOFT
    if key == "google":
        raise ProviderConfigError(
            "provider 'google' is not implemented yet (see GitHub #67 / #84); "
            'use provider = "microsoft"'
        )
    raise ProviderConfigError(f"unknown provider {raw!r}; supported: microsoft")
