"""Hermetic tests for Google Chat find / last."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.providers.google_auth import GOOGLE_SCOPES
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind

_GOOGLE_CHAT = "blumkin.providers.google.chat"


def _space(name: str, *, display: str | None = None, dm: bool = True) -> dict:
    return {
        "name": name,
        "displayName": display,
        "spaceType": "DIRECT_MESSAGE" if dm else "SPACE",
    }


def _message(name: str, text: str, sender: str = "Vivek") -> dict:
    return {
        "name": name,
        "text": text,
        "createTime": "2026-09-01T12:00:00Z",
        "sender": {"displayName": sender, "name": "users/1"},
    }


def test_chat_find_matches_on_member_display_name(tmp_path: Path) -> None:
    service = _service(
        spaces=[_space("spaces/AAA"), _space("spaces/BBB")],
        members={"spaces/AAA": ["Vivek Kumar"], "spaces/BBB": ["Ada Lovelace"]},
    )
    with _patched(service):
        payload = asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).chat_find(with_name="vivek"))
    assert [item["id"] for item in payload["items"]] == ["spaces/AAA"]
    assert payload["items"][0]["members"] == ["Vivek Kumar"]
    assert payload["items"][0]["chat_type"] == "oneOnOne"
    assert payload["partial"] is False
    assert payload["skipped"] == 0


def test_chat_find_counts_a_refused_membership_as_skipped(tmp_path: Path) -> None:
    """A space we cannot read members for makes the result partial, not a silent miss."""
    service = _service(
        spaces=[_space("spaces/AAA"), _space("spaces/BBB")],
        members={"spaces/BBB": ["Vivek Kumar"]},
        member_errors={"spaces/AAA": HttpError(httplib2.Response({"status": 403}), b"{}", uri="x")},
    )
    with _patched(service):
        payload = asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).chat_find(with_name="vivek"))
    assert [item["id"] for item in payload["items"]] == ["spaces/BBB"]
    assert payload["partial"] is True
    assert payload["skipped"] == 1


def test_chat_find_rejects_an_empty_name(tmp_path: Path) -> None:
    with _patched(_service()), pytest.raises(ValueError, match="non-empty display name"):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).chat_find(with_name="  "))


def test_chat_last_by_chat_id_skips_the_space_scan(tmp_path: Path) -> None:
    service = _service(messages=[_message("spaces/AAA/messages/1", "ping")])
    service.spaces.return_value.list.side_effect = AssertionError("should not list spaces")
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_last(chat_id="spaces/AAA", n=1)
        )
    assert payload["chat"]["id"] == "spaces/AAA"
    assert payload["items"][0]["body_text"] == "ping"
    assert payload["items"][0]["id"] == "spaces/AAA/messages/1"
    assert payload["filters"] == {"complete": None, "contains": None, "scanned": None}


def test_chat_last_contains_filters_and_reports_the_scan(tmp_path: Path) -> None:
    service = _service(
        messages=[
            _message("spaces/AAA/messages/1", "lunch?"),
            _message("spaces/AAA/messages/2", "Admin access sorted"),
        ]
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_last(
                chat_id="spaces/AAA", contains="ADMIN", n=5
            )
        )
    assert [item["id"] for item in payload["items"]] == ["spaces/AAA/messages/2"]
    assert payload["filters"]["contains"] == "ADMIN"
    assert payload["filters"]["complete"] is True
    assert payload["filters"]["scanned"] == 2


def test_chat_last_fails_closed_on_ambiguity_and_partial_scans(tmp_path: Path) -> None:
    ambiguous = _service(
        spaces=[_space("spaces/AAA"), _space("spaces/BBB")],
        members={"spaces/AAA": ["Vivek Kumar"], "spaces/BBB": ["Vivek Rao"]},
    )
    with _patched(ambiguous), pytest.raises(ValueError, match="ambiguous chat match"):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).chat_last(with_name="vivek", n=1))

    partial = _service(
        spaces=[_space("spaces/AAA"), _space("spaces/BBB")],
        members={"spaces/BBB": ["Vivek Kumar"]},
        member_errors={"spaces/AAA": HttpError(httplib2.Response({"status": 403}), b"{}", uri="x")},
    )
    with _patched(partial), pytest.raises(ValueError, match="is partial"):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).chat_last(with_name="vivek", n=1))


def test_chat_last_no_match_stays_a_stdout_result(tmp_path: Path) -> None:
    """Same contract as the Graph path: chat null, not an exception."""
    service = _service(spaces=[_space("spaces/AAA")], members={"spaces/AAA": ["Ada"]})
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_last(with_name="nobody", n=1)
        )
    assert payload["chat"] is None
    assert payload["items"] == []


def test_chat_last_requires_exactly_one_selector(tmp_path: Path) -> None:
    provider = GoogleWorkspaceProvider(_cfg(tmp_path))
    with _patched(_service()):
        with pytest.raises(ValueError, match="exactly one of --with or --chat-id"):
            asyncio.run(provider.chat_last(n=1))
        with pytest.raises(ValueError, match="exactly one of --with or --chat-id"):
            asyncio.run(provider.chat_last(with_name="v", chat_id="spaces/AAA", n=1))


def test_google_scopes_include_chat_reads() -> None:
    for scope in ("chat.spaces.readonly", "chat.messages.readonly", "chat.memberships.readonly"):
        assert f"https://www.googleapis.com/auth/{scope}" in GOOGLE_SCOPES


def _cfg(config_dir: Path) -> BlumkinConfig:
    oauth = config_dir / "desktop-client.json"
    if not oauth.is_file():
        oauth.write_text(
            '{"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "s"}}'
        )
    return BlumkinConfig(
        client_id="id.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=oauth,
        graph_timeout_seconds=60.0,
        legacy_flat=True,
        mail_signature=MailSignatureConfig(),
        profile="default",
        provider=ProviderKind.GOOGLE,
        tags=(),
        tenant_id="",
        wo1162425_scopes=False,
    )


def _patched(service: MagicMock):
    return patch.multiple(
        _GOOGLE_CHAT,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    )


def _service(
    *,
    spaces: list[dict] | None = None,
    members: dict[str, list[str]] | None = None,
    member_errors: dict[str, Exception] | None = None,
    messages: list[dict] | None = None,
) -> MagicMock:
    service = MagicMock()
    spaces_api = service.spaces.return_value
    spaces_api.list.return_value.execute.return_value = {"spaces": spaces or []}

    def _members_list(*, parent: str, pageSize: int, pageToken: str | None):  # noqa: N803
        request = MagicMock()
        error = (member_errors or {}).get(parent)
        if error is not None:
            request.execute.side_effect = error
        else:
            request.execute.return_value = {
                "memberships": [
                    {"member": {"displayName": name}} for name in (members or {}).get(parent, [])
                ]
            }
        return request

    spaces_api.members.return_value.list.side_effect = _members_list
    spaces_api.messages.return_value.list.return_value.execute.return_value = {
        "messages": messages or []
    }
    return service


def test_chat_find_raises_when_every_space_is_refused(tmp_path: Path) -> None:
    """All-refused is an auth/scope problem, not "no chat found" with exit 0."""
    error = HttpError(httplib2.Response({"status": 403}), b"{}", uri="x")
    service = _service(
        spaces=[_space("spaces/AAA"), _space("spaces/BBB")],
        member_errors={"spaces/AAA": error, "spaces/BBB": error},
    )
    with _patched(service), pytest.raises(HttpError):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).chat_find(with_name="vivek"))


def test_chat_last_by_name_reads_messages_from_the_resolved_space(tmp_path: Path) -> None:
    """Pins the resolve-then-read wiring: the message list must target the match's id."""
    service = _service(
        spaces=[_space("spaces/AAA")],
        members={"spaces/AAA": ["Vivek Kumar"]},
        messages=[_message("spaces/AAA/messages/1", "ping")],
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_last(with_name="vivek", n=1)
        )
    assert payload["chat"]["id"] == "spaces/AAA"
    assert payload["items"][0]["body_text"] == "ping"
    kwargs = service.spaces.return_value.messages.return_value.list.call_args.kwargs
    assert kwargs["parent"] == "spaces/AAA"
    assert kwargs["orderBy"] == "createTime DESC"


def test_chat_last_contains_pages_and_reports_an_incomplete_scan(tmp_path: Path) -> None:
    """Exercises the paging walk and the early-stop reporting the Graph path pins."""
    service = _service()
    pages = [
        {"messages": [_message("spaces/AAA/messages/1", "lunch")], "nextPageToken": "p2"},
        {"messages": [_message("spaces/AAA/messages/2", "admin access")]},
    ]
    service.spaces.return_value.messages.return_value.list.return_value.execute.side_effect = pages
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_last(
                chat_id="spaces/AAA", contains="admin", n=1
            )
        )
    assert [item["id"] for item in payload["items"]] == ["spaces/AAA/messages/2"]
    assert payload["filters"]["scanned"] == 2
    # Stopped as soon as n matches were in hand, so absence past here is unknown.
    assert payload["filters"]["complete"] is False


def test_chat_find_maps_group_chat_and_named_spaces_to_group(tmp_path: Path) -> None:
    """GROUP_CHAT is Google's group DM; both it and SPACE are "group" to Graph."""
    for space_type in ("GROUP_CHAT", "SPACE"):
        service = _service(
            spaces=[{"name": "spaces/AAA", "displayName": "Team", "spaceType": space_type}],
            members={"spaces/AAA": ["Vivek Kumar"]},
        )
        with _patched(service):
            payload = asyncio.run(
                GoogleWorkspaceProvider(_cfg(tmp_path)).chat_find(with_name="vivek")
            )
        assert payload["items"][0]["chat_type"] == "group", space_type


def test_chat_last_skips_system_notices_and_tombstones(tmp_path: Path) -> None:
    """A membership event must not surface as a message, nor count toward the scan."""
    service = _service(
        messages=[
            {"name": "spaces/AAA/messages/sys", "createTime": "2026-09-01T11:00:00Z"},
            {
                "name": "spaces/AAA/messages/gone",
                "text": "admin access",
                "createTime": "2026-09-01T11:30:00Z",
                "deletionMetadata": {"deletionType": "CREATOR"},
            },
            _message("spaces/AAA/messages/real", "admin access granted"),
        ]
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_last(
                chat_id="spaces/AAA", contains="admin", n=5
            )
        )
    assert [item["id"] for item in payload["items"]] == ["spaces/AAA/messages/real"]
    # Only the real message was scanned, so the count means the same as on Graph.
    assert payload["filters"]["scanned"] == 1


def test_chat_last_contains_matches_a_formatted_text_only_message(tmp_path: Path) -> None:
    """A card message keeps its content in formattedText; --contains must still see it."""
    service = _service(
        messages=[
            {
                "name": "spaces/AAA/messages/card",
                "formattedText": "*Admin access* granted",
                "createTime": "2026-09-01T12:00:00Z",
                "sender": {"displayName": "Vivek", "name": "users/1"},
            }
        ]
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).chat_last(
                chat_id="spaces/AAA", contains="admin access", n=5
            )
        )
    assert [item["id"] for item in payload["items"]] == ["spaces/AAA/messages/card"]
    assert "Admin access" in payload["items"][0]["body_text"]
