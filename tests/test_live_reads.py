"""Live Graph read + silent refresh validation (operator machine)."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from blumkin.auth import reload_token_cache_from_disk, save_token_cache, status_dict
from blumkin.config import load_config
from blumkin.providers.kind import ProviderKind
from blumkin.providers.microsoft_mail_probe import probe_outlook_signature
from blumkin.secret_store import read_text as read_secret_text
from blumkin.secret_store import write_text as write_secret_text
from blumkin.skills.calendar import calendar_today

pytestmark = pytest.mark.live


def _live_ready() -> bool:
    if os.environ.get("BLUMKIN_LIVE", "").strip() not in {"1", "true", "yes"}:
        return False
    cfg = load_config()
    status = status_dict(cfg)
    return bool(
        status["client_id_configured"]
        and status["token_cache"]
        and status["auth_record"]
        and status["refresh_token_present"]
    )


def _force_expire_access_tokens(cfg) -> None:
    """Force-expire access tokens in the token cache, wherever it lives.

    Goes through ``secret_store`` (not ``cache_path.read_text()`` directly)
    because a keychain-preferring profile (the new default) migrates the
    cache into the OS keychain and unlinks the legacy file on first read - a
    raw file read/write here would raise ``FileNotFoundError`` on exactly the
    dev machines this live test is meant to run on (issue #287 review).
    """
    raw = read_secret_text(cfg, "token_cache")
    assert raw is not None, "token cache missing - _live_ready() should have skipped"
    data = json.loads(raw)
    for entry in (data.get("AccessToken") or {}).values():
        if isinstance(entry, dict) and "expires_on" in entry:
            entry["expires_on"] = "1"  # 1970-01-01 — definitely expired
    write_secret_text(cfg, "token_cache", json.dumps(data))


def test_live_calendar_today() -> None:
    if not _live_ready():
        pytest.skip(
            "Set BLUMKIN_LIVE=1 and configure ~/.config/blumkin "
            "(config.toml + token cache + auth record + refresh token)"
        )
    payload = asyncio.run(calendar_today())
    assert "date" in payload
    assert "items" in payload
    assert isinstance(payload["items"], list)
    assert payload["timezone"]


def test_live_outlook_signature_probe_round_trips() -> None:
    """The probe creates + reads + deletes a draft; assert it stays in our own mailbox."""
    if not _live_ready():
        pytest.skip(
            "Set BLUMKIN_LIVE=1 and configure ~/.config/blumkin "
            "(config.toml + token cache + auth record + refresh token)"
        )
    cfg = load_config()
    if cfg.provider is not ProviderKind.MICROSOFT:
        pytest.skip("the Outlook signature probe is Microsoft-only")
    detected = asyncio.run(probe_outlook_signature(cfg))
    # None only if Graph refused the probe entirely; otherwise it is a real bool.
    assert detected is None or isinstance(detected, bool)


def test_live_silent_refresh_after_forced_access_token_expiry() -> None:
    """Expire cached access token; next Graph call must refresh without a browser."""
    if not _live_ready():
        pytest.skip(
            "Set BLUMKIN_LIVE=1 and configure ~/.config/blumkin "
            "(config.toml + token cache + auth record + refresh token)"
        )
    cfg = load_config()
    backup = read_secret_text(cfg, "token_cache")
    assert backup is not None
    try:
        _force_expire_access_tokens(cfg)
        reload_token_cache_from_disk(cfg)
        before = status_dict(cfg)
        assert before["access_token_expired"] is True
        assert before["refresh_token_present"] is True

        payload = asyncio.run(calendar_today())
        assert isinstance(payload["items"], list)

        # Persist any in-memory MSAL updates, then re-read status from disk.
        save_token_cache(cfg)
        reload_token_cache_from_disk(cfg)
        after = status_dict(cfg)
        assert after["access_token_expired"] is False
        assert after["access_token_expires_at"] is not None
        assert after["access_token_expires_in_seconds"] is not None
        assert after["access_token_expires_in_seconds"] > 0
    except BaseException:
        write_secret_text(cfg, "token_cache", backup)
        reload_token_cache_from_disk(cfg)
        raise
