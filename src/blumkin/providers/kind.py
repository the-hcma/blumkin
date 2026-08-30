"""Provider kind enum and config parsing."""

from __future__ import annotations

from enum import StrEnum


class ProviderConfigError(ValueError):
    """Invalid or unimplemented ``provider`` value in config.toml."""


class ProviderKind(StrEnum):
    """Account / backend family for workspace skills."""

    GOOGLE = "google"
    MICROSOFT = "microsoft"


def parse_provider_kind(raw: str) -> ProviderKind:
    """Parse a config.toml ``provider`` string.

    Accepts ``microsoft`` (default for empty) and ``google``.
    """
    key = raw.strip().lower()
    if key in {"", "microsoft"}:
        return ProviderKind.MICROSOFT
    if key == "google":
        return ProviderKind.GOOGLE
    raise ProviderConfigError(f"unknown provider {raw!r}; supported: google, microsoft")
