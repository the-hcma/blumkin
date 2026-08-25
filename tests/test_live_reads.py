"""Live Graph read + silent refresh validation (operator machine)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from blumkin.auth import reload_token_cache_from_disk, save_token_cache, status_dict
from blumkin.config import load_config
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


def _force_expire_access_tokens(cache_path: Path) -> None:
    data = json.loads(cache_path.read_text())
    for entry in (data.get("AccessToken") or {}).values():
        if isinstance(entry, dict) and "expires_on" in entry:
            entry["expires_on"] = "1"  # 1970-01-01 — definitely expired
    cache_path.write_text(json.dumps(data))
    cache_path.chmod(0o600)


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


def test_live_silent_refresh_after_forced_access_token_expiry() -> None:
    """Expire cached access token; next Graph call must refresh without a browser."""
    if not _live_ready():
        pytest.skip(
            "Set BLUMKIN_LIVE=1 and configure ~/.config/blumkin "
            "(config.toml + token cache + auth record + refresh token)"
        )
    cfg = load_config()
    cache_path = cfg.token_cache_path
    backup = cache_path.read_text()
    try:
        _force_expire_access_tokens(cache_path)
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
        cache_path.write_text(backup)
        cache_path.chmod(0o600)
        raise
