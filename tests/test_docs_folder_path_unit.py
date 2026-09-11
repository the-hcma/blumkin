"""`docs create --folder` targets a pre-existing (possibly nested) folder path (#212)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kiota_abstractions.method import Method

from blumkin.config import BlumkinConfig, MailSignatureConfig, PreferencesConfig
from blumkin.providers.google_auth import (
    DOCS_FOLDER_SCOPES,
    DOCS_SCOPES,
    GOOGLE_REQUIRED_SCOPES,
)
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.providers.microsoft import MicrosoftWorkspaceProvider
from blumkin.skills.drive import DriveFolderAmbiguousError

_DRIVE = "https://www.googleapis.com/auth/drive"
_DOCS_MOD = "blumkin.providers.google.docs"
_DRIVE_MOD = "blumkin.providers.google.drive"


def test_folder_scope_split() -> None:
    # A root-level docs create must not require the broad `drive` scope.
    assert _DRIVE not in DOCS_SCOPES
    assert _DRIVE in DOCS_FOLDER_SCOPES
    assert DOCS_SCOPES < DOCS_FOLDER_SCOPES
    assert DOCS_FOLDER_SCOPES <= GOOGLE_REQUIRED_SCOPES


def _run_google_docs_create(service: MagicMock, cfg: BlumkinConfig, **kwargs: Any) -> Any:
    get_creds = MagicMock(return_value=MagicMock())
    with (
        patch.multiple(
            _DOCS_MOD,
            get_credentials=get_creds,
            build_api_service=MagicMock(return_value=service),
            execute=MagicMock(side_effect=lambda request, **_kw: request.execute()),
        ),
        patch.multiple(
            _DRIVE_MOD,
            execute=MagicMock(side_effect=lambda request, **_kw: request.execute()),
        ),
    ):
        payload = asyncio.run(GoogleWorkspaceProvider(cfg).docs_create(**kwargs))
    return payload, get_creds


def _docs_service() -> MagicMock:
    service = MagicMock()
    docs = service.documents.return_value
    docs.create.return_value.execute.return_value = {"documentId": "doc-1"}
    docs.batchUpdate.return_value.execute.return_value = {}
    service.files.return_value.update.return_value.execute.return_value = {"id": "doc-1"}
    return service


def test_root_docs_create_uses_the_narrow_scope(tmp_path: Path) -> None:
    _, get_creds = _run_google_docs_create(
        _docs_service(), _google_cfg(tmp_path), title="T", body="hi"
    )
    assert get_creds.call_args.kwargs["required_scopes"] is DOCS_SCOPES


def test_folder_docs_create_requires_the_broad_scope(tmp_path: Path) -> None:
    service = _docs_service()
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "f1", "name": "Reports"}]
    }
    _, get_creds = _run_google_docs_create(
        service, _google_cfg(tmp_path), title="T", body="hi", folder="Reports"
    )
    assert get_creds.call_args.kwargs["required_scopes"] is DOCS_FOLDER_SCOPES


def test_folder_docs_create_creates_missing_segments(tmp_path: Path) -> None:
    service = _docs_service()
    files = service.files.return_value
    # "Language Classes" exists; "Portuguese Classes" missing -> created.
    files.list.return_value.execute.side_effect = [
        {"files": [{"id": "lc", "name": "Language Classes"}]},
        {"files": []},
    ]
    files.create.return_value.execute.return_value = {
        "id": "pc-new",
        "name": "Portuguese Classes",
        "webViewLink": "u",
    }
    (payload, _) = _run_google_docs_create(
        service,
        _google_cfg(tmp_path),
        title="Week 4",
        body="hi",
        folder="Language Classes/Portuguese Classes",
    )
    create_body = files.create.call_args.kwargs["body"]
    assert create_body["parents"] == ["lc"] and create_body["mimeType"].endswith(".folder")
    assert files.update.call_args.kwargs["addParents"] == "pc-new"
    assert payload["document"]["folder"] == "Language Classes/Portuguese Classes"


def test_folder_docs_create_ambiguous_segment_is_side_effect_free(tmp_path: Path) -> None:
    service = _docs_service()
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "a", "name": "Reports"}, {"id": "b", "name": "Reports"}]
    }
    with pytest.raises(DriveFolderAmbiguousError):
        _run_google_docs_create(
            service, _google_cfg(tmp_path), title="T", body="hi", folder="Reports"
        )
    # The folder is resolved BEFORE the doc is minted, so an ambiguous --folder
    # leaves nothing behind for an MCP retry to multiply.
    service.documents.return_value.create.assert_not_called()
    service.files.return_value.create.assert_not_called()
    service.files.return_value.update.assert_not_called()


def _google_cfg(config_dir: Path) -> BlumkinConfig:
    (config_dir / "client.json").write_text("{}")
    return BlumkinConfig(
        client_id="id.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=config_dir / "client.json",
        graph_timeout_seconds=60.0,
        mail_signature=MailSignatureConfig(),
        preferences=PreferencesConfig(),
        profile="default",
        provider=ProviderKind.GOOGLE,
        tags=(),
        tenant_id="",
        wo1162425_scopes=False,
    )


def test_google_docs_create_files_into_a_nested_existing_path(tmp_path: Path) -> None:
    service = MagicMock()
    docs = service.documents.return_value
    docs.create.return_value.execute.return_value = {"documentId": "doc-1"}
    docs.batchUpdate.return_value.execute.return_value = {}
    files = service.files.return_value
    # "Language Classes" then "Portuguese Classes" both already exist.
    files.list.return_value.execute.side_effect = [
        {"files": [{"id": "lc", "name": "Language Classes"}]},
        {"files": [{"id": "pc", "name": "Portuguese Classes"}]},
    ]
    files.update.return_value.execute.return_value = {"id": "doc-1", "parents": ["pc"]}
    with (
        patch.multiple(
            _DOCS_MOD,
            get_credentials=MagicMock(return_value=MagicMock()),
            build_api_service=MagicMock(return_value=service),
            execute=MagicMock(side_effect=lambda request, **_kw: request.execute()),
        ),
        patch.multiple(
            _DRIVE_MOD,
            execute=MagicMock(side_effect=lambda request, **_kw: request.execute()),
        ),
    ):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).docs_create(
                title="Week 4", body="hi", folder="Language Classes/Portuguese Classes"
            )
        )
    files.create.assert_not_called()  # both segments already existed
    assert service.files.return_value.update.call_args.kwargs["addParents"] == "pc"
    assert payload["document"]["folder"] == "Language Classes/Portuguese Classes"


def _ms_cfg() -> BlumkinConfig:
    return BlumkinConfig(
        client_id="abc",
        config_dir=Path("unused"),
        default_tz="UTC",
        docs_scopes=True,
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


def test_ms_docs_create_reuses_a_nested_existing_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    async def send_async(ri: Any, _f: Any, _e: Any) -> Any:
        calls.append(ri)
        if ":/content" in ri.url_template:
            return SimpleNamespace(id="01ABC", name="Week 4.docx", web_url="u")
        if ri.http_method == Method.GET:  # folder existence probe - both exist
            return SimpleNamespace(id="folder")
        return SimpleNamespace(id="new")  # a POST would mean a segment was missing

    client = MagicMock()
    client.request_adapter.send_async = AsyncMock(side_effect=send_async)
    monkeypatch.setattr("blumkin.providers.microsoft_docs.create_graph_client", lambda _c: client)
    asyncio.run(
        MicrosoftWorkspaceProvider(_ms_cfg()).docs_create(
            title="Week 4", body="hi", folder="Language Classes/Portuguese Classes"
        )
    )
    assert not [c for c in calls if c.http_method == Method.POST]  # nothing created
    gets = [c.path_parameters["path"] for c in calls if c.http_method == Method.GET]
    assert gets == ["Language Classes", "Language Classes/Portuguese Classes"]
