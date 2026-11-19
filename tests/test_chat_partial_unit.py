"""Why a chat match is partial and `chat last --allow-partial` (#787)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from blumkin.chat_match import partial_message, skip_reasons, skipped_summary
from blumkin.cli import main
from blumkin.skills.chat import (
    _resolve_chat_target,
    chat_find,
    chat_last,
    format_find_human,
    format_last_human,
)


def _error(status: int | None) -> Exception:
    exc = RuntimeError(f"graph {status}")
    if status is not None:
        exc.response_status_code = status  # type: ignore[attr-defined]
    return exc


def _patch_graph(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )


def _find_client(errors: dict[str, Exception]) -> MagicMock:
    chats = [SimpleNamespace(id=cid, topic=cid, chat_type="group") for cid in (*errors, "chat-ok")]
    client = MagicMock()
    client.me.chats.get = AsyncMock(return_value=SimpleNamespace(value=chats, odata_next_link=None))

    def by_chat_id(chat_id: str) -> MagicMock:
        stub = MagicMock()
        if chat_id in errors:
            stub.members.get = AsyncMock(side_effect=errors[chat_id])
        else:
            members = [SimpleNamespace(display_name="Rachel Green")]
            stub.members.get = AsyncMock(
                return_value=SimpleNamespace(value=members, odata_next_link=None)
            )
        return stub

    client.me.chats.by_chat_id.side_effect = by_chat_id
    return client


def test_find_says_why_chats_were_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _find_client(
        {"a": _error(403), "b": _error(403), "c": _error(429), "d": _error(500), "e": _error(None)}
    )
    _patch_graph(monkeypatch, client)

    found = asyncio.run(chat_find(with_name="rachel"))

    assert found["skipped"] == 5 and found["partial"] is True
    assert found["skip_reasons"] == {"forbidden": 2, "other": 2, "throttled": 1}
    assert [item["id"] for item in found["items"]] == ["chat-ok"]


def test_find_without_skips_reports_zero_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_graph(monkeypatch, _find_client({}))

    found = asyncio.run(chat_find(with_name="rachel"))

    assert found["partial"] is False
    assert found["skip_reasons"] == {"forbidden": 0, "other": 0, "throttled": 0}


def test_skip_reasons_sorts_statuses_into_causes() -> None:
    assert skip_reasons([403, 429, 503, 504, 401, None]) == {
        "forbidden": 1,
        "other": 2,
        "throttled": 3,
    }


def test_the_refusal_names_the_causes_and_a_read_gets_the_way_out() -> None:
    found = {"skipped": 6, "skip_reasons": {"forbidden": 6, "other": 0, "throttled": 0}}

    read = partial_message("Rachel", found, allow_hint=True)
    write = partial_message("Rachel", found)

    assert "skipped 6 chat(s): 6 forbidden (403, retrying will not help)" in read
    assert "--allow-partial" in read
    assert "--allow-partial" not in write and "--chat-id" in write


def test_the_refusal_without_known_causes_keeps_its_old_wording() -> None:
    assert "is partial (skipped 2 chat(s)); retry later" in partial_message("x", {"skipped": 2})


def _partial_find(items: list[dict[str, str]], reasons: dict[str, int] | None = None) -> AsyncMock:
    found = {
        "items": items,
        "partial": True,
        "skip_reasons": reasons or {"forbidden": 6, "other": 0, "throttled": 0},
        "skipped": 6,
    }
    return AsyncMock(return_value=found)


def _messages_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    message = SimpleNamespace(
        id="m1",
        message_type="message",
        created_date_time="2026-09-01T12:00:00+00:00",
        body=SimpleNamespace(content="<p>hi</p>"),
        from_=SimpleNamespace(user=SimpleNamespace(display_name="Rachel", id="u1")),
    )
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.get = AsyncMock(
        return_value=SimpleNamespace(value=[message], odata_next_link=None)
    )
    _patch_graph(monkeypatch, client)
    return client


def test_last_refuses_a_partial_match_by_default_and_names_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("blumkin.skills.chat.chat_find", _partial_find([{"id": "chat-1"}]))
    _messages_client(monkeypatch)

    with pytest.raises(ValueError, match=r"6 forbidden.*--allow-partial"):
        asyncio.run(chat_last(with_name="rachel", n=1))


def test_last_accepts_a_unique_match_with_allow_partial_and_says_it_was_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("blumkin.skills.chat.chat_find", _partial_find([{"id": "chat-1"}]))
    client = _messages_client(monkeypatch)

    result = asyncio.run(chat_last(with_name="rachel", n=1, allow_partial=True))

    assert result["chat"]["id"] == "chat-1"
    assert result["partial"] is True and result["skipped"] == 6
    assert result["skip_reasons"]["forbidden"] == 6
    client.me.chats.by_chat_id.assert_called_with("chat-1")
    assert [item["body_text"] for item in result["items"]] == ["hi"]


def test_allow_partial_still_refuses_an_ambiguous_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "blumkin.skills.chat.chat_find", _partial_find([{"id": "chat-1"}, {"id": "chat-2"}])
    )
    _messages_client(monkeypatch)

    with pytest.raises(ValueError, match="ambiguous chat match"):
        asyncio.run(chat_last(with_name="rachel", n=1, allow_partial=True))


def test_allow_partial_with_no_match_is_still_not_found_and_stays_marked_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("blumkin.skills.chat.chat_find", _partial_find([]))
    _messages_client(monkeypatch)

    result = asyncio.run(chat_last(with_name="rachel", n=1, allow_partial=True))

    assert result["chat"] is None and result["partial"] is True
    assert result["skip_reasons"]["forbidden"] == 6


def test_a_write_target_never_accepts_a_partial_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """`chat send`, `edit` and `delete` resolve through here and have no way out."""
    monkeypatch.setattr("blumkin.skills.chat.chat_find", _partial_find([{"id": "chat-1"}]))

    with pytest.raises(ValueError, match="is partial") as raised:
        asyncio.run(_resolve_chat_target(chat_id=None, with_name="rachel", config=MagicMock()))

    assert "--allow-partial" not in str(raised.value)


def test_human_output_names_the_causes() -> None:
    payload = {
        "items": [],
        "query": "rachel",
        "skip_reasons": {"forbidden": 6, "other": 0, "throttled": 0},
        "skipped": 6,
    }

    assert any("6 forbidden (403" in line for line in format_find_human(payload))
    assert any("6 forbidden (403" in line for line in format_last_human({**payload, "chat": None}))
    chat = {"id": "c", "topic": "T"}
    assert any(
        "6 forbidden (403" in line
        for line in format_last_human({**payload, "chat": chat, "items": []})
    )
    assert skipped_summary({"skipped": 1}) == ""


def test_the_cli_passes_allow_partial_to_the_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    """The isolated default config selects the Microsoft provider, as in the envelope tests."""
    seen: dict[str, object] = {}

    async def fake_last(**kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"chat": {"id": "c", "topic": "T"}, "items": [], "query": "rachel", "skipped": 0}

    monkeypatch.setattr("blumkin.providers.microsoft.chat_last", fake_last)
    result = CliRunner().invoke(
        main, ["chat", "last", "--with", "rachel", "--allow-partial", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert seen.get("allow_partial") is True
