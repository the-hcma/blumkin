"""Workspace providers (Microsoft and Google)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from blumkin.providers.kind import ProviderConfigError, ProviderKind, parse_provider_kind

if TYPE_CHECKING:
    from blumkin.config import BlumkinConfig
    from blumkin.providers.google_provider import GoogleWorkspaceProvider
    from blumkin.providers.microsoft import MicrosoftWorkspaceProvider
    from blumkin.providers.protocol import WorkspaceProvider


def get_provider(config: BlumkinConfig | None = None) -> WorkspaceProvider:
    """Return the workspace provider for ``config.provider``."""
    from blumkin.config import load_config
    from blumkin.providers.google_provider import GoogleWorkspaceProvider
    from blumkin.providers.microsoft import MicrosoftWorkspaceProvider

    cfg = config or load_config()
    if cfg.provider is ProviderKind.GOOGLE:
        return GoogleWorkspaceProvider(cfg)
    if cfg.provider is ProviderKind.MICROSOFT:
        return MicrosoftWorkspaceProvider(cfg)
    raise ProviderConfigError(
        f"provider {cfg.provider.value!r} is not implemented yet (see GitHub #67 / #84)"
    )


__all__ = [
    "GoogleWorkspaceProvider",
    "MicrosoftWorkspaceProvider",
    "ProviderConfigError",
    "ProviderKind",
    "WorkspaceProvider",
    "get_provider",
    "parse_provider_kind",
]


def __getattr__(name: str):
    if name == "GoogleWorkspaceProvider":
        from blumkin.providers.google_provider import GoogleWorkspaceProvider

        return GoogleWorkspaceProvider
    if name == "MicrosoftWorkspaceProvider":
        from blumkin.providers.microsoft import MicrosoftWorkspaceProvider

        return MicrosoftWorkspaceProvider
    if name == "WorkspaceProvider":
        from blumkin.providers.protocol import WorkspaceProvider

        return WorkspaceProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name}")
