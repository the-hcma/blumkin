"""`docs create --folder` targets a pre-existing (possibly nested) folder path (#212)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kiota_abstractions.method import Method

from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.providers.google_auth import DOCS_SCOPES
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.providers.microsoft import MicrosoftWorkspaceProvider

_DOCS_MOD = "blumkin.providers.google.docs"
_DRIVE_MOD = "blumkin.providers.google.drive"


def test_drive_scope_is_in_the_docs_subset() -> None:
    assert "https://www.googleapis.com/auth/drive" in DOCS_SCOPES


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
        legacy_flat=True,
        mail_signature=MailSignatureConfig(),
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
        legacy_flat=True,
        mail_signature=MailSignatureConfig(),
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
