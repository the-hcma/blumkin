"""Live Google API read validation (operator machine, Google profile).

Mirrors ``test_live_reads.py`` for ``provider = "google"``. CI never runs this
(it is deselected with ``-m 'not live_google'``); the operator runs it locally
with a configured Google profile:

    BLUMKIN_LIVE_GOOGLE=1 uv run pytest -m live_google

Reads only - never a verb that notifies anyone (see
``.cursor/rules/no-third-party-side-effects.mdc``).
"""

from __future__ import annotations

import asyncio
import os

import pytest

from blumkin.config import load_config
from blumkin.providers import get_provider
from blumkin.providers.google_auth import status_dict
from blumkin.providers.kind import ProviderKind

# ``live`` too, so CI's ``-m 'not live'`` deselects these without touching the
# shared ``.github/ci`` gate; the operator narrows to ``-m live_google``.
pytestmark = [pytest.mark.live, pytest.mark.live_google]


def _live_ready() -> bool:
    if os.environ.get("BLUMKIN_LIVE_GOOGLE", "").strip() not in {"1", "true", "yes"}:
        return False
    cfg = load_config()
    if cfg.provider is not ProviderKind.GOOGLE:
        return False
    status = status_dict(cfg)
    return bool(
        status["client_id_configured"] and status["token_cache"] and status["refresh_token_present"]
    )


_SKIP = (
    "Set BLUMKIN_LIVE_GOOGLE=1 and select a logged-in Google profile "
    "(provider=google, token file + refresh token under ~/.config/blumkin)"
)


def test_live_google_calendar_today() -> None:
    if not _live_ready():
        pytest.skip(_SKIP)
    payload = asyncio.run(get_provider().calendar_today())
    assert "date" in payload
    assert isinstance(payload["items"], list)
    assert payload["timezone"]


def test_live_google_mail_inbox() -> None:
    if not _live_ready():
        pytest.skip(_SKIP)
    payload = asyncio.run(get_provider().mail_inbox(top=5))
    assert isinstance(payload["items"], list)
    assert len(payload["items"]) <= 5


def test_live_google_status_reports_a_future_expiry() -> None:
    if not _live_ready():
        pytest.skip(_SKIP)
    # A read forces a silent refresh when the access token has lapsed.
    asyncio.run(get_provider().calendar_today())
    status = status_dict(load_config())
    assert status["access_token_expired"] is False
    assert status["access_token_expires_in_seconds"]
    assert status["access_token_expires_in_seconds"] > 0
