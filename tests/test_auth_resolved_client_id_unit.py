"""Unit tests for blumkin.auth's Microsoft ``client_id`` keychain resolution (issue #368).

``_resolved_client_id`` prefers a vaulted ``ms_client_id`` app secret over
``cfg.client_id``, mirroring Google's ``client_secret`` precedence
(``_vaulted_or_file_client_secret`` in ``providers/google_auth.py``). It is
deliberately used only in ``create_credential`` (the actual token-acquisition
path), not in ``status_dict`` / ``_granted_scopes_from_cache`` - see the
docstring on ``_granted_scopes_from_cache`` for why (keeping ``status_dict``'s
existing "one keychain touch" guarantee for this slice).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from blumkin import auth
from blumkin.config import BlumkinConfig, MailSignatureConfig, PreferencesConfig
from blumkin.providers.kind import ProviderKind


def _cfg(*, client_id: str) -> BlumkinConfig:
    return BlumkinConfig(
        client_id=client_id,
        config_dir=Path("unused"),
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=None,
        graph_timeout_seconds=60.0,
        mail_signature=MailSignatureConfig(),
        preferences=PreferencesConfig(),
        profile="default",
        provider=ProviderKind.MICROSOFT,
        tags=(),
        tenant_id="brk.tech",
        wo1162425_scopes=False,
    )


def test_resolved_client_id_falls_back_to_toml_when_unvaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(client_id="toml-client-id")
    monkeypatch.setattr(auth, "read_app_secret", lambda cfg, kind: None)  # noqa: ARG005
    assert auth._resolved_client_id(cfg) == "toml-client-id"


def test_resolved_client_id_prefers_vaulted_value(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(client_id="toml-client-id")
    monkeypatch.setattr(
        auth,
        "read_app_secret",
        lambda cfg, kind: "vaulted-client-id",  # noqa: ARG005
    )
    assert auth._resolved_client_id(cfg) == "vaulted-client-id"


def test_create_credential_raises_when_neither_toml_nor_vault_has_a_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(client_id="")
    monkeypatch.setattr(auth, "read_app_secret", lambda cfg, kind: None)  # noqa: ARG005
    with pytest.raises(auth.ProviderConfigError, match="Missing client_id"):
        auth.create_credential(cfg)


def test_create_credential_uses_vaulted_client_id_when_toml_is_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile with no ``client_id`` in ``config.toml`` at all still authenticates once
    ``ms_client_id`` is vaulted (issue #368's "config.toml + keychain, no mandatory extra
    field" goal)."""
    cfg = _cfg(client_id="")
    monkeypatch.setattr(
        auth,
        "read_app_secret",
        lambda cfg, kind: "vaulted-client-id",  # noqa: ARG005
    )
    fake_cred = MagicMock(name="InteractiveBrowserCredential")
    with patch("blumkin.auth.InteractiveBrowserCredential", return_value=fake_cred) as ctor:
        with pytest.raises(auth.AuthRequiredError):
            auth.create_credential(cfg, allow_interactive=False)
    assert ctor.call_args.kwargs["client_id"] == "vaulted-client-id"
