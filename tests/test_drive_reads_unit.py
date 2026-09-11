"""Hermetic tests for the `drive` read side (list / get) on both providers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from googleapiclient.errors import HttpError
from msgraph.generated.models.o_data_errors.o_data_error import ODataError

from blumkin.config import BlumkinConfig, MailSignatureConfig, PreferencesConfig
from blumkin.providers.google_auth import DRIVE_SCOPES
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.providers.microsoft import MicrosoftWorkspaceProvider
from blumkin.skills.dispatch import run_skill
from blumkin.skills.drive import (
    DriveFolderAmbiguousError,
    DriveFolderNotFoundError,
    DriveItemNotFoundError,
    DriveSelectorError,
    format_drive_get_human,
    format_drive_list_human,
    normalize_order,
    split_path,
    validate_folder_selector,
)
from blumkin.skills.errors import ScopeAddonDisabledError

_GOOGLE_MOD = "blumkin.providers.google.drive"
_MS_MOD = "blumkin.providers.microsoft_drive"


# --------------------------------------------------------------------------- shared layer


def test_validate_folder_selector_rejects_both() -> None:
    with pytest.raises(DriveSelectorError):
        validate_folder_selector("A/B", "id-1")
    validate_folder_selector(None, None)  # no raise
    validate_folder_selector("A/B", None)


def test_normalize_order() -> None:
    assert normalize_order(None) == "modified"
    assert normalize_order("NAME") == "name"
    with pytest.raises(DriveSelectorError):
        normalize_order("size")


def test_split_path_drops_empty_segments() -> None:
    assert split_path("/A//B/ ") == ["A", "B"]


def test_human_formatters() -> None:
    listed = format_drive_list_human({"items": [{"kind": "doc", "name": "Vocab", "id": "d1"}]})
    assert any("Vocab" in line for line in listed)
    assert format_drive_list_human({"items": []}) == ["(no items)"]
    got = format_drive_get_human(
        {"item": {"name": "V", "kind": "doc", "id": "d1", "provider": "google"}}
    )
    assert any("id=d1" in line for line in got)


# --------------------------------------------------------------------------- config helpers


def _google_cfg(config_dir: Path) -> BlumkinConfig:
    oauth = config_dir / "client.json"
    oauth.write_text("{}")
    return BlumkinConfig(
        client_id="id.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=oauth,
        graph_timeout_seconds=60.0,
        mail_signature=MailSignatureConfig(),
        preferences=PreferencesConfig(),
        profile="default",
        provider=ProviderKind.GOOGLE,
        tags=(),
        tenant_id="",
        wo1162425_scopes=False,
    )


def _ms_cfg(*, docs_scopes: bool = True) -> BlumkinConfig:
    return BlumkinConfig(
        client_id="abc",
        config_dir=Path("unused"),
        default_tz="UTC",
        docs_scopes=docs_scopes,
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


# --------------------------------------------------------------------------- Google backend


def _google_service() -> MagicMock:
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [
            {
                "id": "d1",
                "name": "Portuguese vocab",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-09-01T10:00:00Z",
                "webViewLink": "https://docs.google.com/document/d/d1/edit",
                "parents": ["folder-1"],
            },
            {
                "id": "f2",
                "name": "attachments",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": ["folder-1"],
            },
        ]
    }
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "d1",
        "name": "Portuguese vocab",
        "mimeType": "application/vnd.google-apps.document",
        "size": "2048",
        "modifiedTime": "2026-09-01T10:00:00Z",
        "webViewLink": "https://docs.google.com/document/d/d1/edit",
        "parents": ["folder-1"],
        "owners": [{"displayName": "H", "emailAddress": "h@example.com"}],
        "exportLinks": {"application/pdf": "u", "text/plain": "u"},
    }
    return service


def _google_patched(service: MagicMock):
    return patch.multiple(
        _GOOGLE_MOD,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
        execute=MagicMock(side_effect=lambda request, **_kw: request.execute()),
    )


def test_google_drive_list_stable_shape(tmp_path: Path) -> None:
    with _google_patched(_google_service()):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_list(folder_id="folder-1")
        )
    assert payload["items"][0] == {
        "id": "d1",
        "name": "Portuguese vocab",
        "mime_type": "application/vnd.google-apps.document",
        "kind": "doc",
        "size": None,
        "modified": "2026-09-01T10:00:00Z",
        "web_url": "https://docs.google.com/document/d/d1/edit",
        "parent_id": "folder-1",
        "provider": "google",
    }
    assert payload["items"][1]["kind"] == "folder"


def test_google_drive_list_scopes_and_query(tmp_path: Path) -> None:
    service = _google_service()
    get_creds = MagicMock(return_value=MagicMock())
    with patch.multiple(
        _GOOGLE_MOD,
        get_credentials=get_creds,
        build_api_service=MagicMock(return_value=service),
        execute=MagicMock(side_effect=lambda request, **_kw: request.execute()),
    ):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_list(
                folder_id="folder-1", query="vocab"
            )
        )
    assert get_creds.call_args.kwargs["required_scopes"] is DRIVE_SCOPES
    kw = service.files.return_value.list.call_args.kwargs
    q = kw["q"]
    assert "'folder-1' in parents" in q and "name contains 'vocab'" in q and "trashed = false" in q
    # Folder-scoped list reaches into Shared Drives.
    assert kw["supportsAllDrives"] is True and kw["includeItemsFromAllDrives"] is True


def test_google_drive_list_root_stays_my_drive(tmp_path: Path) -> None:
    service = _google_service()
    with _google_patched(service):
        asyncio.run(GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_list())
    kw = service.files.return_value.list.call_args.kwargs
    assert "in parents" not in kw["q"]
    # An unscoped root list must NOT fold in every Shared Drive.
    assert kw["supportsAllDrives"] is True
    assert "includeItemsFromAllDrives" not in kw


def test_google_drive_get_carries_supports_all_drives(tmp_path: Path) -> None:
    service = _google_service()
    with _google_patched(service):
        asyncio.run(GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_get(item_id="d1"))
    assert service.files.return_value.get.call_args.kwargs["supportsAllDrives"] is True


def test_google_drive_list_folder_path_walk(tmp_path: Path) -> None:
    service = MagicMock()
    service.files.return_value.list.return_value.execute.side_effect = [
        {"files": [{"id": "lvl1", "name": "Language Classes"}]},
        {"files": [{"id": "lvl2", "name": "Portuguese Classes"}]},
        {"files": []},
    ]
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_list(
                folder="Language Classes/Portuguese Classes"
            )
        )
    assert payload["items"] == []
    final_q = service.files.return_value.list.call_args.kwargs["q"]
    assert "'lvl2' in parents" in final_q


def test_google_drive_list_ambiguous_folder_segment(tmp_path: Path) -> None:
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "a", "name": "Reports"}, {"id": "b", "name": "Reports"}]
    }
    with _google_patched(service), pytest.raises(DriveFolderAmbiguousError):
        asyncio.run(GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_list(folder="Reports"))


def test_google_drive_list_missing_folder_segment(tmp_path: Path) -> None:
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {"files": []}
    with _google_patched(service), pytest.raises(DriveFolderNotFoundError):
        asyncio.run(GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_list(folder="Nope"))


def test_google_drive_list_selector_conflict(tmp_path: Path) -> None:
    with _google_patched(_google_service()), pytest.raises(DriveSelectorError):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_list(folder="A", folder_id="i")
        )


def test_google_drive_list_paginates_to_top(tmp_path: Path) -> None:
    service = MagicMock()
    page = {"files": [{"id": f"x{i}", "name": str(i), "mimeType": "text/plain"} for i in range(3)]}
    service.files.return_value.list.return_value.execute.side_effect = [
        {**page, "nextPageToken": "tok"},
        page,
    ]
    with _google_patched(service):
        payload = asyncio.run(GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_list(top=5))
    assert len(payload["items"]) == 5


def test_google_drive_get_maps_metadata(tmp_path: Path) -> None:
    with _google_patched(_google_service()):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_get(item_id="d1")
        )
    item = payload["item"]
    assert item["size"] == 2048
    assert item["kind"] == "doc"
    assert item["owners"] == [{"name": "H", "email": "h@example.com"}]
    assert item["export_formats"] == ["pdf", "txt"]


def test_google_drive_get_404(tmp_path: Path) -> None:
    service = MagicMock()
    resp = SimpleNamespace(status=404, reason="Not Found")
    service.files.return_value.get.return_value.execute.side_effect = HttpError(resp, b"nope")
    with _google_patched(service), pytest.raises(DriveItemNotFoundError):
        asyncio.run(GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_get(item_id="missing"))


# --------------------------------------------------------------------------- Microsoft backend


def _ms_item(
    *, item_id: str, name: str, folder: bool = False, parent_id: str = "p1", parent_path: str = ""
) -> SimpleNamespace:
    ext = name.rsplit(".", 1)
    mime = "application/octet-stream" if len(ext) == 2 and not folder else None
    return SimpleNamespace(
        id=item_id,
        name=name,
        size=10,
        file=None if folder else SimpleNamespace(mime_type=mime),
        folder=SimpleNamespace(child_count=0) if folder else None,
        last_modified_date_time=datetime(2026, 9, 1, tzinfo=UTC),
        web_url=f"https://onedrive.example/{name}",
        parent_reference=SimpleNamespace(id=parent_id, path=parent_path),
        created_by=None,
        last_modified_by=None,
    )


def _ms_client(pages: list[Any] | None = None, item: Any = None, by_path: Any = None) -> MagicMock:
    """`item` answers a GET /items/{id}; `by_path` answers a GET /root:/{+path};
    everything else pops `pages` (a collection response)."""
    client = MagicMock()
    calls: list[Any] = []
    queue = list(pages or [])

    async def send_async(request_info: Any, _factory: Any, _err: Any) -> Any:
        calls.append(request_info)
        tmpl = request_info.url_template
        if "root:/{+path}" in tmpl and "children" not in tmpl:
            return by_path
        if "items/{id}" in tmpl and "children" not in tmpl:
            return item
        return queue.pop(0) if queue else SimpleNamespace(value=[], odata_next_link=None)

    client.request_adapter.send_async = AsyncMock(side_effect=send_async)
    client.calls = calls
    return client


def _ms_run(
    client: MagicMock, monkeypatch: pytest.MonkeyPatch, cfg: BlumkinConfig, method: str, **kw: Any
):
    monkeypatch.setattr(f"{_MS_MOD}.create_graph_client", lambda _c: client)
    return asyncio.run(getattr(MicrosoftWorkspaceProvider(cfg), method)(**kw))


def test_ms_drive_list_children_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    page = SimpleNamespace(
        value=[
            _ms_item(item_id="d1", name="Notes.docx"),
            _ms_item(item_id="f2", name="sub", folder=True),
        ],
        odata_next_link=None,
    )
    client = _ms_client(pages=[page])
    payload = _ms_run(client, monkeypatch, _ms_cfg(), "drive_list", folder_id="p1")
    kinds = {i["name"]: i["kind"] for i in payload["items"]}
    assert kinds == {"Notes.docx": "doc", "sub": "folder"}
    assert payload["items"][0]["provider"] == "microsoft"
    assert payload["items"][0]["modified"] == "2026-09-01T00:00:00+00:00"
    assert "items/{id}/children" in client.calls[0].url_template


def test_ms_drive_list_root_caps_top_server_side(monkeypatch: pytest.MonkeyPatch) -> None:
    page = SimpleNamespace(
        value=[_ms_item(item_id=f"x{i}", name=f"f{i}.txt") for i in range(2)],
        odata_next_link="more",
    )
    client = _ms_client(pages=[page])
    payload = _ms_run(client, monkeypatch, _ms_cfg(), "drive_list", top=2)
    assert len(payload["items"]) == 2
    url = client.calls[0].url_template
    assert url.startswith("https://graph.microsoft.com/v1.0/me/drive/root/children?")
    assert "%24top=2" in url and "%24orderby=lastModifiedDateTime" in url
    assert len(client.calls) == 1  # stopped paging once top was reached


def test_ms_drive_list_search_escapes_apostrophe(monkeypatch: pytest.MonkeyPatch) -> None:
    page = SimpleNamespace(value=[_ms_item(item_id="d1", name="vocab.docx")], odata_next_link=None)
    client = _ms_client(pages=[page])
    _ms_run(client, monkeypatch, _ms_cfg(), "drive_list", query="what's new")
    assert "search(q='{+q}')" in client.calls[0].url_template
    assert client.calls[0].path_parameters["q"] == "what''s new"


def test_ms_drive_list_query_in_folder_is_exact_not_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _ms_item(item_id="reports-id", name="Reports", folder=True)
    hits = SimpleNamespace(
        value=[
            _ms_item(item_id="a", name="a.txt", parent_id="reports-id"),
            _ms_item(item_id="b", name="b.txt", parent_id="archive-reports-id"),  # /Archive/Reports
        ],
        odata_next_link=None,
    )
    client = _ms_client(pages=[hits], by_path=resolved)
    payload = _ms_run(client, monkeypatch, _ms_cfg(), "drive_list", folder="Reports", query="x")
    assert [i["id"] for i in payload["items"]] == ["a"]


def test_ms_drive_list_folder_path_is_percent_encoded(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ms_client(pages=[], by_path=_ms_item(item_id="q1", name="Q#1", folder=True))
    _ms_run(client, monkeypatch, _ms_cfg(), "drive_list", folder="Q#1")
    resolve_call = next(c for c in client.calls if "root:/{+path}" in c.url_template)
    assert resolve_call.path_parameters["path"] == "Q%231"


def test_ms_drive_list_folder_that_is_a_file_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    a_file = _ms_item(item_id="f", name="Reports")  # folder=False
    client = _ms_client(by_path=a_file)
    with pytest.raises(DriveFolderNotFoundError):
        _ms_run(client, monkeypatch, _ms_cfg(), "drive_list", folder="Reports")


def test_ms_drive_list_follows_next_link(monkeypatch: pytest.MonkeyPatch) -> None:
    page1 = SimpleNamespace(
        value=[_ms_item(item_id="a", name="a.txt")], odata_next_link="https://graph/next"
    )
    page2 = SimpleNamespace(value=[_ms_item(item_id="b", name="b.txt")], odata_next_link=None)
    client = _ms_client(pages=[page1, page2])
    payload = _ms_run(client, monkeypatch, _ms_cfg(), "drive_list", top=0)
    assert {i["id"] for i in payload["items"]} == {"a", "b"}


def test_ms_drive_list_missing_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()

    async def boom(*_a: Any, **_k: Any) -> Any:
        raise ODataError(response_status_code=404)

    client.request_adapter.send_async = AsyncMock(side_effect=boom)
    monkeypatch.setattr(f"{_MS_MOD}.create_graph_client", lambda _c: client)
    with pytest.raises(DriveFolderNotFoundError):
        asyncio.run(MicrosoftWorkspaceProvider(_ms_cfg()).drive_list(folder="Nope"))


def test_ms_drive_get_maps_and_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ms_client(item=_ms_item(item_id="d1", name="Report.xlsx"))
    payload = _ms_run(client, monkeypatch, _ms_cfg(), "drive_get", item_id="d1")
    assert payload["item"]["kind"] == "sheet"
    assert payload["item"]["export_formats"] == ["pdf"]

    client2 = _ms_client(item=None)
    with pytest.raises(DriveItemNotFoundError):
        _ms_run(client2, monkeypatch, _ms_cfg(), "drive_get", item_id="gone")


# --------------------------------------------------------------------------- dispatch gate


def test_drive_skills_gated_on_docs_scopes_for_microsoft() -> None:
    with pytest.raises(ScopeAddonDisabledError):
        asyncio.run(run_skill("drive.list", {}, config=_ms_cfg(docs_scopes=False)))


def test_drive_catalog_scopes_match_the_docs_scopes_gate() -> None:
    """Every DRIVE_SKILLS entry is gated on docs_scopes (Files.ReadWrite), so its
    published `scopes` must say Files.ReadWrite - not the weaker Files.Read."""
    from blumkin.skills import DRIVE_SKILLS, describe_skill

    for sid in DRIVE_SKILLS:
        spec = describe_skill(sid)
        assert spec is not None
        assert "Files.ReadWrite" in spec.scopes, sid
        assert "Files.Read" not in spec.scopes, sid


def test_drive_get_arg_maps_to_item_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Prov:
        async def drive_get(self, *, item_id: str) -> dict[str, Any]:
            captured["item_id"] = item_id
            return {"item": {}}

    asyncio.run(run_skill("drive.get", {"id": "abc"}, config=_ms_cfg(), provider=_Prov()))
    assert captured == {"item_id": "abc"}
