"""Unit tests for Google HTTP timeout + retry helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.providers import google_http
from blumkin.providers.kind import ProviderKind


def test_authorized_http_uses_config_timeout(monkeypatch) -> None:
    captured: dict[str, float | None] = {}

    def fake_http(*, timeout=None):
        captured["timeout"] = timeout
        return MagicMock(name="httplib2.Http")

    monkeypatch.setattr(google_http.httplib2, "Http", fake_http)
    monkeypatch.setattr(
        google_http.google_auth_httplib2,
        "AuthorizedHttp",
        lambda creds, http=None: http,
    )
    cfg = _cfg(Path("/tmp"), timeout=45.0)
    http = google_http.authorized_http(MagicMock(), cfg)
    assert http is not None
    assert captured["timeout"] == 45.0


def test_execute_passes_num_retries() -> None:
    request = MagicMock()
    request.execute.return_value = {"ok": True}
    assert google_http.execute(request) == {"ok": True}
    request.execute.assert_called_once_with(num_retries=google_http.DEFAULT_HTTP_RETRIES)


def test_refresh_request_defaults_timeout_from_config(monkeypatch) -> None:
    cfg = _cfg(Path("/tmp"), timeout=45.0)
    timed = google_http.refresh_request(cfg)
    seen: dict[str, float | None] = {}

    def fake_call(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        seen["timeout"] = timeout
        return MagicMock(status=200, data=b"{}", headers={})

    monkeypatch.setattr(
        "google.auth.transport.requests.Request.__call__",
        fake_call,
    )
    timed("https://oauth2.googleapis.com/token", method="POST", body=b"x", headers={})
    assert seen["timeout"] == 45.0


def _cfg(config_dir: Path, *, timeout: float = 60.0) -> BlumkinConfig:
    return BlumkinConfig(
        client_id="fake",
        config_dir=config_dir,
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=None,
        graph_timeout_seconds=timeout,
        legacy_flat=True,
        mail_signature=MailSignatureConfig(),
        profile="default",
        provider=ProviderKind.GOOGLE,
        tags=(),
        tenant_id="",
        wo1162425_scopes=False,
    )
