"""The throwaway-draft probe that detects Outlook's own auto-signature."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.providers.kind import ProviderKind
from blumkin.providers.microsoft_mail_probe import (
    _PROBE_SENTINEL,
    _body_carries_extra_content,
    _sweep_probe_drafts,
    probe_outlook_signature,
)

_MOD = "blumkin.providers.microsoft_mail_probe"


def _cfg() -> BlumkinConfig:
    return BlumkinConfig(
        client_id="x",
        config_dir=Path("unused"),
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=None,
        graph_timeout_seconds=60.0,
        legacy_flat=True,
        mail_signature=MailSignatureConfig(),
        profile="default",
        provider=ProviderKind.MICROSOFT,
        tags=(),
        tenant_id="t",
        wo1162425_scopes=False,
    )


def _client(returned_body: str | None, *, stale: list[str] | None = None) -> MagicMock:
    client = MagicMock()
    client.me.messages.post = AsyncMock(return_value=SimpleNamespace(id="probe-1"))
    client.me.messages.get = AsyncMock(
        return_value=SimpleNamespace(value=[SimpleNamespace(id=i) for i in (stale or [])])
    )
    body = SimpleNamespace(content=returned_body) if returned_body is not None else None
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(return_value=SimpleNamespace(body=body))
    item.delete = AsyncMock(return_value=None)
    return client


def _run(client: MagicMock, monkeypatch: pytest.MonkeyPatch) -> bool | None:
    monkeypatch.setattr(f"{_MOD}.create_graph_client", lambda _cfg: client)
    return asyncio.run(probe_outlook_signature(config=_cfg()))


def test_body_carries_extra_content_ignores_the_sentinel_and_boilerplate() -> None:
    assert not _body_carries_extra_content(
        f"<html><head><style>p {{color:red}}</style></head><body><p>{_PROBE_SENTINEL}</p>"
        "<!-- comment --></body></html>"
    )


def test_body_carries_extra_content_flags_an_injected_signature() -> None:
    assert _body_carries_extra_content(
        f"<p>{_PROBE_SENTINEL}</p><div id=Signature><p>Ada Example<br>Technical Fellow</p></div>"
    )


def test_body_carries_extra_content_flags_an_image_only_signature() -> None:
    assert _body_carries_extra_content(
        f'<p>{_PROBE_SENTINEL}</p><div><img src="cid:logo" alt=""></div>'
    )


def test_probe_detects_an_injected_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(f"<p>{_PROBE_SENTINEL}</p><div>Ada Example - BRK Tech</div>")
    assert _run(client, monkeypatch) is True
    client.me.messages.by_message_id.return_value.delete.assert_awaited_once()


def test_probe_reports_no_signature_when_body_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(f"<html><body><p>{_PROBE_SENTINEL}</p></body></html>")
    assert _run(client, monkeypatch) is False


def test_probe_deletes_the_draft_even_if_the_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client("unused")
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(side_effect=RuntimeError("boom"))
    assert _run(client, monkeypatch) is None
    item.delete.assert_awaited_once()


def test_probe_returns_none_when_create_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.me.messages.get = AsyncMock(return_value=SimpleNamespace(value=[]))
    client.me.messages.post = AsyncMock(side_effect=RuntimeError("no Mail scope"))
    assert _run(client, monkeypatch) is None


def test_probe_returns_none_when_graph_returns_no_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.me.messages.get = AsyncMock(return_value=SimpleNamespace(value=[]))
    client.me.messages.post = AsyncMock(return_value=None)
    assert _run(client, monkeypatch) is None


def test_probe_sweeps_leftover_probe_drafts_from_a_previous_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(f"<p>{_PROBE_SENTINEL}</p>", stale=["stale-a", "stale-b"])
    assert _run(client, monkeypatch) is False
    deleted = [c.args[0] for c in client.me.messages.by_message_id.call_args_list]
    assert "stale-a" in deleted and "stale-b" in deleted


def test_sweep_is_best_effort_and_deletes_each_match() -> None:
    client = MagicMock()
    client.me.messages.get = AsyncMock(
        return_value=SimpleNamespace(value=[SimpleNamespace(id="a"), SimpleNamespace(id=None)])
    )
    client.me.messages.by_message_id.return_value.delete = AsyncMock(return_value=None)
    asyncio.run(_sweep_probe_drafts(client))
    client.me.messages.by_message_id.assert_called_once_with("a")
