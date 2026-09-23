"""Unit tests for Google auth error typing and scope auto-escalation (issue #133)."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError, TransportError

from blumkin.auth import AuthRequiredError, AuthTransientError, MissingScopeError
from blumkin.config import BlumkinConfig, MailSignatureConfig, PreferencesConfig
from blumkin.providers import google_auth
from blumkin.providers.google_auth import (
    CALENDAR_FREEBUSY_SCOPES,
    CALENDAR_READ_SCOPES,
    CALENDAR_SCOPES,
    CHAT_READ_SCOPES,
    CHAT_SCOPES,
    DOCS_SCOPES,
    DRIVE_SCOPES,
    GOOGLE_REQUIRED_SCOPES,
    GOOGLE_SCOPES,
    MAIL_MODIFY_SCOPES,
    MAIL_READ_SCOPES,
    MAIL_SETTINGS_SCOPES,
    MAIL_WRITE_SCOPES,
    PEOPLE_SCOPES,
)
from blumkin.providers.kind import ProviderConfigError, ProviderKind


def test_classify_refresh_error_invalid_grant_is_auth_required() -> None:
    exc = google_auth._classify_refresh_error(RefreshError("invalid_grant: Token has expired"))
    assert isinstance(exc, AuthRequiredError)


def test_classify_refresh_error_retryable_is_transient() -> None:
    """A 5xx token-endpoint outage (google-auth marks it retryable) is not a bad grant."""
    exc = google_auth._classify_refresh_error(
        RefreshError("server_error: internal error", retryable=True)
    )
    assert isinstance(exc, AuthTransientError)


def test_classify_refresh_error_transport_is_transient() -> None:
    exc = google_auth._classify_refresh_error(TransportError("Connection reset by peer"))
    assert isinstance(exc, AuthTransientError)


def test_classify_refresh_error_unknown_falls_back_to_auth_required() -> None:
    exc = google_auth._classify_refresh_error(RuntimeError("boom"))
    assert isinstance(exc, AuthRequiredError)


def test_client_config_defaults_when_toml_and_file_omit_endpoints(tmp_path: Path) -> None:
    """No toml override, no endpoint in the Desktop JSON: blumkin's hardcoded defaults win."""
    cfg = _cfg(tmp_path)
    installed = google_auth._client_config(cfg)["installed"]
    assert installed["auth_uri"] == "https://accounts.google.com/o/oauth2/auth"
    assert installed["token_uri"] == "https://oauth2.googleapis.com/token"
    assert installed["redirect_uris"] == ["http://localhost"]


def test_client_config_prefers_desktop_json_endpoint_over_default(tmp_path: Path) -> None:
    """No toml override, but the Desktop JSON sets an endpoint: the file wins over the default."""
    oauth_file = tmp_path / "desktop-client.json"
    oauth_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "fake-google-desktop-client.apps.googleusercontent.com",
                    "client_secret": "fake-google-client-secret",
                    "auth_uri": "https://file.example/auth",
                    "token_uri": "https://file.example/token",
                    "redirect_uris": ["http://127.0.0.1"],
                }
            }
        )
    )
    cfg = _cfg(tmp_path, oauth_file=oauth_file)
    installed = google_auth._client_config(cfg)["installed"]
    assert installed["auth_uri"] == "https://file.example/auth"
    assert installed["token_uri"] == "https://file.example/token"
    assert installed["redirect_uris"] == ["http://127.0.0.1"]


def test_client_config_prefers_toml_override_over_desktop_json(tmp_path: Path) -> None:
    """A config.toml override (issue #368) beats both the Desktop JSON and the default."""
    oauth_file = tmp_path / "desktop-client.json"
    oauth_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "fake-google-desktop-client.apps.googleusercontent.com",
                    "client_secret": "fake-google-client-secret",
                    "auth_uri": "https://file.example/auth",
                    "token_uri": "https://file.example/token",
                    "redirect_uris": ["http://127.0.0.1"],
                }
            }
        )
    )
    cfg = dataclasses.replace(
        _cfg(tmp_path, oauth_file=oauth_file),
        google_auth_uri="https://toml.example/auth",
        google_token_uri="https://toml.example/token",
        google_redirect_uris=("http://toml.example/callback",),
    )
    installed = google_auth._client_config(cfg)["installed"]
    assert installed["auth_uri"] == "https://toml.example/auth"
    assert installed["token_uri"] == "https://toml.example/token"
    assert installed["redirect_uris"] == ["http://toml.example/callback"]


def test_client_config_client_id_prefers_toml_over_desktop_json(tmp_path: Path) -> None:
    """``cfg.client_id`` (already toml-or-file resolved by ``load_config``) wins over the file."""
    oauth_file = tmp_path / "desktop-client.json"
    oauth_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "file-client-id.apps.googleusercontent.com",
                    "client_secret": "fake-google-client-secret",
                }
            }
        )
    )
    cfg = dataclasses.replace(
        _cfg(tmp_path, oauth_file=oauth_file),
        client_id="toml-client-id.apps.googleusercontent.com",
    )
    installed = google_auth._client_config(cfg)["installed"]
    assert installed["client_id"] == "toml-client-id.apps.googleusercontent.com"


def test_client_config_blank_client_id_raises_even_when_file_has_one(tmp_path: Path) -> None:
    """``_client_config`` trusts only ``cfg.client_id`` (already toml-or-file resolved by
    ``load_config``); constructing a ``BlumkinConfig`` directly with a blank ``client_id``
    must fail even when the Desktop JSON's own ``client_id`` is present, and the error must
    not misattribute the cause to the file."""
    oauth_file = tmp_path / "desktop-client.json"
    oauth_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "file-client-id.apps.googleusercontent.com",
                    "client_secret": "fake-google-client-secret",
                }
            }
        )
    )
    cfg = dataclasses.replace(_cfg(tmp_path, oauth_file=oauth_file), client_id="")
    with pytest.raises(ProviderConfigError, match="client_id is required"):
        google_auth._client_config(cfg)


def test_get_credentials_noninteractive_default_ignores_directory_readonly(
    tmp_path: Path,
) -> None:
    """auth refresh must not exit 4 for the one scope no command requires.

    Regression from issue #133 review, round 3: a grant covering every command
    except directory.readonly (admin-restricted in some Workspaces) previously
    ran `blumkin auth refresh` fine and must keep doing so.
    """
    cfg = _cfg(tmp_path)
    _write_valid_token(cfg, scopes=sorted(GOOGLE_REQUIRED_SCOPES))
    result = google_auth.get_credentials(cfg, allow_interactive=False)
    assert result.valid


def test_get_credentials_noninteractive_narrows_gate_to_required_subset(tmp_path: Path) -> None:
    """A subset grant must keep working for the command that only needs that subset.

    Regression from issue #133 review: `people resolve` degrades gracefully on a
    contacts-only grant and `mail list` only needs gmail.readonly - the
    non-interactive fail-fast gate must not block either just because the build's
    full GOOGLE_SCOPES union is not entirely granted.
    """
    cfg = _cfg(tmp_path)
    _write_valid_token(cfg, scopes=sorted(PEOPLE_SCOPES))

    result = google_auth.get_credentials(
        cfg, allow_interactive=False, required_scopes=PEOPLE_SCOPES
    )
    assert result.valid

    with pytest.raises(MissingScopeError):
        google_auth.get_credentials(cfg, allow_interactive=False, required_scopes=CHAT_SCOPES)


def test_get_credentials_noninteractive_sufficient_scope_returns_creds(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _write_valid_token(cfg, scopes=sorted(GOOGLE_SCOPES))

    result = google_auth.get_credentials(cfg, allow_interactive=False)

    assert result.valid


def test_get_credentials_noninteractive_valid_but_insufficient_scope_fails_fast(
    tmp_path: Path,
) -> None:
    """Bug from issue #133: a valid-but-scope-short token must not be handed back."""
    cfg = _cfg(tmp_path)
    granted = {"https://www.googleapis.com/auth/gmail.readonly"}
    _write_valid_token(cfg, scopes=sorted(granted))

    with (
        patch.object(google_auth, "_consent_once") as consent_once,
        pytest.raises(MissingScopeError) as excinfo,
    ):
        google_auth.get_credentials(cfg, allow_interactive=False)

    consent_once.assert_not_called()
    assert excinfo.value.current == granted
    assert excinfo.value.missing == GOOGLE_REQUIRED_SCOPES - granted


def test_google_scopes_is_the_union_of_every_required_subset() -> None:
    """Every per-skill-area subset must stay covered by the build-wide scope set.

    Guards against the two drifting apart silently (they are separate literals,
    not derived from each other, for lexicographic ordering - see the comment
    above CALENDAR_SCOPES in google_auth.py).
    """
    directory_readonly = frozenset({"https://www.googleapis.com/auth/directory.readonly"})
    assert GOOGLE_SCOPES == (
        CALENDAR_SCOPES
        | CHAT_SCOPES
        | DOCS_SCOPES
        | DRIVE_SCOPES
        | MAIL_READ_SCOPES
        | MAIL_WRITE_SCOPES
        | MAIL_MODIFY_SCOPES
        | MAIL_SETTINGS_SCOPES
        | PEOPLE_SCOPES
        | directory_readonly
    )
    assert GOOGLE_REQUIRED_SCOPES == GOOGLE_SCOPES - directory_readonly
    assert CALENDAR_FREEBUSY_SCOPES.issubset(CALENDAR_SCOPES)
    assert CALENDAR_READ_SCOPES.issubset(CALENDAR_SCOPES)
    assert CHAT_READ_SCOPES.issubset(CHAT_SCOPES)
    assert DOCS_SCOPES.issubset(GOOGLE_REQUIRED_SCOPES)
    assert DRIVE_SCOPES.issubset(GOOGLE_REQUIRED_SCOPES)


def test_interactive_consent_forces_prompt_immediately_when_grant_already_insufficient(
    tmp_path: Path,
) -> None:
    """A stored-but-short grant must ask for full consent on the very first attempt."""
    cfg = _cfg(tmp_path)
    _write_token(cfg, scopes=["https://www.googleapis.com/auth/gmail.readonly"])
    final_creds = MagicMock()

    with patch.object(google_auth, "_consent_once", return_value=final_creds) as consent_once:
        google_auth._run_interactive_consent(cfg)

    consent_once.assert_called_once_with(cfg, force_consent=True)


def test_interactive_consent_retries_once_on_partial_grant_then_succeeds(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    partial = frozenset({"https://www.googleapis.com/auth/gmail.readonly"})
    warning = _scope_warning('Scope has changed from "" to "gmail.readonly".', sorted(partial))
    final_creds = MagicMock()

    with patch.object(
        google_auth, "_consent_once", side_effect=[warning, final_creds]
    ) as consent_once:
        result = google_auth._run_interactive_consent(cfg)

    assert result is final_creds
    assert consent_once.call_count == 2
    # Second attempt must force prompt=consent so the operator sees every box.
    assert consent_once.call_args_list[1].kwargs == {"force_consent": True}


def test_interactive_consent_stops_after_second_partial_grant(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    first = _scope_warning(
        'Scope has changed from "" to "gmail.readonly".',
        ["https://www.googleapis.com/auth/gmail.readonly"],
    )
    second_scopes = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/chat.messages",
    ]
    second = _scope_warning(
        'Scope has changed from "gmail.readonly" to "gmail.readonly chat.messages".',
        second_scopes,
    )

    with (
        patch.object(google_auth, "_consent_once", side_effect=[first, second]) as consent_once,
        pytest.raises(MissingScopeError) as excinfo,
    ):
        google_auth._run_interactive_consent(cfg)

    assert consent_once.call_count == 2
    assert excinfo.value.current == frozenset(second_scopes)
    assert "https://www.googleapis.com/auth/gmail.compose" in excinfo.value.missing
    assert excinfo.value.missing == GOOGLE_SCOPES - excinfo.value.current


def test_get_credentials_invalidates_the_agent_cache_after_a_fresh_interactive_consent(
    tmp_path: Path,
) -> None:
    """A fresh interactive consent may be a different account (PR #346 review) - the
    previous agent-cached token must not keep being served for the rest of its TTL."""
    from blumkin import secret_store

    cfg = _cfg(tmp_path)
    fake_creds = MagicMock()

    with (
        patch.object(google_auth, "_run_interactive_consent", return_value=fake_creds),
        patch.object(secret_store, "invalidate_agent_cache") as invalidate,
        patch.object(google_auth, "_save_credentials") as save_credentials,
    ):
        google_auth.get_credentials(cfg, allow_interactive=True)

    invalidate.assert_called_once_with(cfg)
    save_credentials.assert_called_once_with(cfg, fake_creds)


def test_status_dict_missing_scopes_empty_before_first_login(tmp_path: Path) -> None:
    payload = google_auth.status_dict(_cfg(tmp_path))
    assert payload["granted_scopes"] == []
    assert payload["missing_scopes"] == []


def test_status_dict_reads_google_token_from_keyring_once(tmp_path: Path, monkeypatch) -> None:
    """A single `status_dict()` call (`doctor` / `auth status`) must not re-probe
    the same keychain item more than once - each extra round trip is a separate
    OS Keychain authorization prompt in practice, and this used to touch
    `google_token` up to four times (once each for the access-token-expiry read,
    the granted-scopes read, the presence check, and the backend check)."""
    from blumkin import secret_store

    token = json.dumps(
        {
            "token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "fake-google-desktop-client.apps.googleusercontent.com",
            "client_secret": "fake-google-client-secret",
            "scopes": sorted(GOOGLE_REQUIRED_SCOPES),
        }
    )
    calls: list[str] = []

    class _FakeKeyring:
        def delete_password(self, service: str, account: str) -> None:
            pass

        def get_password(self, service: str, account: str) -> str | None:
            calls.append(account)
            kind = json.loads(account)[2]
            return token if kind == "google_token" else None

        def set_password(self, service: str, account: str, value: str) -> None:
            pass

    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _FakeKeyring())
    cfg = dataclasses.replace(_cfg(tmp_path), token_storage="keyring")
    google_auth.status_dict(cfg)
    google_token_touches = [c for c in calls if json.loads(c)[2] == "google_token"]
    assert len(google_token_touches) == 1


def test_status_dict_reports_granted_and_missing_scopes(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _write_valid_token(cfg, scopes=["https://www.googleapis.com/auth/gmail.readonly"])
    payload = google_auth.status_dict(cfg)
    assert payload["granted_scopes"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert payload["missing_scopes"] == sorted(
        GOOGLE_REQUIRED_SCOPES - {"https://www.googleapis.com/auth/gmail.readonly"}
    )


def test_status_dict_reports_token_storage_backend(tmp_path: Path, monkeypatch) -> None:
    """`doctor` prints this key verbatim - a drop or rename must fail loudly (issue #287 review)."""
    cfg = _cfg(tmp_path)
    assert google_auth.status_dict(cfg)["token_storage_backend"] == "file"

    from blumkin import secret_store

    class _FakeKeyring:
        def delete_password(self, service: str, username: str) -> None:
            pass

        def get_password(self, service: str, username: str) -> str | None:
            return None

        def set_password(self, service: str, username: str, password: str) -> None:
            pass

    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _FakeKeyring())
    keyring_dir = tmp_path / "keyring-profile"
    keyring_dir.mkdir()
    keyring_cfg = dataclasses.replace(_cfg(keyring_dir), token_storage="keyring")
    assert google_auth.status_dict(keyring_cfg)["token_storage_backend"] == "keyring"


def _cfg(config_dir: Path, *, oauth_file: Path | None = None) -> BlumkinConfig:
    path = oauth_file
    if path is None:
        path = config_dir / "desktop-client.json"
        if not path.is_file():
            path.write_text(
                '{"installed": {'
                '"client_id": "fake-google-desktop-client.apps.googleusercontent.com", '
                '"client_secret": "fake-google-client-secret"}}'
            )
    cfg = BlumkinConfig(
        client_id="fake-google-desktop-client.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=path,
        graph_timeout_seconds=60.0,
        mail_signature=MailSignatureConfig(),
        preferences=PreferencesConfig(),
        profile="default",
        provider=ProviderKind.GOOGLE,
        tags=(),
        tenant_id="",
        wo1162425_scopes=False,
    )
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _scope_warning(message: str, new_scope: list[str]) -> Warning:
    """Build the ``Warning`` oauthlib raises on a partial grant, with its scope attrs."""
    warning = Warning(message)
    setattr(warning, "new_scope", new_scope)  # noqa: B010 - mirrors oauthlib's own dynamic attr
    return warning


def _write_token(cfg: BlumkinConfig, *, scopes: list[str]) -> None:
    cfg.google_token_path.write_text(
        json.dumps(
            {
                "client_id": "fake-google-desktop-client.apps.googleusercontent.com",
                "client_secret": "fake-google-client-secret",
                "refresh_token": "fake-refresh",
                "scopes": scopes,
                "token": "fake-access",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )


def _write_valid_token(cfg: BlumkinConfig, *, scopes: list[str]) -> None:
    """Write a token that ``Credentials.from_authorized_user_info`` loads as valid."""
    expiry = datetime.now(UTC) + timedelta(hours=1)
    cfg.google_token_path.write_text(
        json.dumps(
            {
                "client_id": "fake-google-desktop-client.apps.googleusercontent.com",
                "client_secret": "fake-google-client-secret",
                "expiry": expiry.isoformat().replace("+00:00", "Z"),
                "refresh_token": "fake-refresh",
                "scopes": scopes,
                "token": "fake-access",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )
