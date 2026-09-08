"""Hermetic tests for `drive mkdir` / `drive move` / `drive rename` (#212)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.providers.microsoft import MicrosoftWorkspaceProvider
from blumkin.skills.dispatch import run_skill
from blumkin.skills.drive import (
    DriveFolderNotFoundError,
    DriveSelectorError,
    validate_move_selector,
)
from blumkin.skills.errors import ConsentRequiredError

_GOOGLE_MOD = "blumkin.providers.google.drive"
_MS_MOD = "blumkin.providers.microsoft_drive"


def test_validate_move_selector_is_xor() -> None:
    validate_move_selector("A/B", None)
    validate_move_selector(None, "id")
    with pytest.raises(DriveSelectorError):
        validate_move_selector(None, None)
    with pytest.raises(DriveSelectorError):
        validate_move_selector("A/B", "id")


# --------------------------------------------------------------------------- config


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
        legacy_flat=True,
        mail_signature=MailSignatureConfig(),
        profile="default",
        provider=ProviderKind.MICROSOFT,
        tags=(),
        tenant_id="brk.tech",
        wo1162425_scopes=False,
    )


# --------------------------------------------------------------------------- Google


def _google_patched(service: MagicMock):
    return patch.multiple(
        _GOOGLE_MOD,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
        execute=MagicMock(side_effect=lambda request, **_kw: request.execute()),
    )


def test_google_mkdir_creates_missing_segments(tmp_path: Path) -> None:
    service = MagicMock()
    files = service.files.return_value
    # Probe walk: "A" found, "A/B" missing -> DriveFolderNotFoundError -> create walk.
    files.list.return_value.execute.side_effect = [
        {"files": [{"id": "idA", "name": "A"}]},
        {"files": []},
        {"files": [{"id": "idA", "name": "A"}]},
        {"files": []},
    ]
    files.create.return_value.execute.return_value = {
        "id": "idB",
        "name": "B",
        "webViewLink": "https://drive/B",
    }
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_mkdir(path="A/B")
        )
    assert payload["created"] is True
    assert payload["folder"]["id"] == "idB" and payload["folder"]["path"] == "A/B"


def test_google_mkdir_noop_when_exists(tmp_path: Path) -> None:
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "idA", "name": "A", "webViewLink": "u"}]
    }
    with _google_patched(service):
        payload = asyncio.run(GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_mkdir(path="A"))
    assert payload["created"] is False and payload["folder"]["id"] == "idA"
    service.files.return_value.create.assert_not_called()


def test_google_move_reparents(tmp_path: Path) -> None:
    service = MagicMock()
    files = service.files.return_value
    files.get.return_value.execute.return_value = {"id": "f1", "name": "doc", "parents": ["old"]}
    files.update.return_value.execute.return_value = {
        "id": "f1",
        "name": "doc",
        "mimeType": "text/plain",
        "parents": ["dest"],
    }
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_move(
                item_id="f1", dest_folder_id="dest"
            )
        )
    kw = files.update.call_args.kwargs
    assert kw["addParents"] == "dest" and kw["removeParents"] == "old"
    assert payload["moved_to"] == "dest" and payload["item"]["parent_id"] == "dest"


def test_google_move_missing_dest_path_without_make_parents(tmp_path: Path) -> None:
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {"files": []}
    with _google_patched(service), pytest.raises(DriveFolderNotFoundError):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_move(
                item_id="f1", dest_path="Nope"
            )
        )


def test_google_rename(tmp_path: Path) -> None:
    service = MagicMock()
    service.files.return_value.update.return_value.execute.return_value = {
        "id": "f1",
        "name": "new name",
        "mimeType": "text/plain",
    }
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_rename(
                item_id="f1", name="new name"
            )
        )
    assert service.files.return_value.update.call_args.kwargs["body"] == {"name": "new name"}
    assert payload["item"]["name"] == "new name"


# --------------------------------------------------------------------------- Microsoft


def _ms_client(*, path_items: dict[str, Any] | None = None, patched: Any = None) -> MagicMock:
    client = MagicMock()
    path_items = path_items or {}
    sent: list[Any] = []

    async def send_async(ri: Any, _f: Any, _e: Any) -> Any:
        sent.append(ri)
        if ri.http_method.name == "PATCH":
            return patched
        if ri.http_method.name == "POST":
            return SimpleNamespace(id="new-folder", name="leaf", web_url="u")
        # GET by path
        return path_items.get(ri.path_parameters.get("path"))

    client.request_adapter.send_async = AsyncMock(side_effect=send_async)
    client.sent = sent
    return client


def _ms_run(client: MagicMock, monkeypatch: pytest.MonkeyPatch, method: str, **kw: Any):
    monkeypatch.setattr(f"{_MS_MOD}.create_graph_client", lambda _c: client)
    return asyncio.run(getattr(MicrosoftWorkspaceProvider(_ms_cfg()), method)(**kw))


def test_ms_mkdir_creates_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ms_client(path_items={"A": SimpleNamespace(id="idA", name="A", web_url="u")})
    payload = _ms_run(client, monkeypatch, "drive_mkdir", path="A/B")
    assert payload["created"] is True
    assert any(ri.http_method.name == "POST" for ri in client.sent)


def test_ms_mkdir_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    exists = SimpleNamespace(id="idAB", name="B", web_url="u")
    client = _ms_client(path_items={"A/B": exists})
    payload = _ms_run(client, monkeypatch, "drive_mkdir", path="A/B")
    assert payload["created"] is False and payload["folder"]["id"] == "idAB"


def test_ms_move_patches_parent_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    patched = SimpleNamespace(
        id="f1",
        name="doc",
        file=SimpleNamespace(mime_type="text/plain"),
        folder=None,
        size=1,
        last_modified_date_time=None,
        web_url="u",
        parent_reference=SimpleNamespace(id="dest", path=""),
        created_by=None,
        last_modified_by=None,
    )
    client = _ms_client(patched=patched)
    payload = _ms_run(client, monkeypatch, "drive_move", item_id="f1", dest_folder_id="dest")
    patch_ri = next(ri for ri in client.sent if ri.http_method.name == "PATCH")
    assert patch_ri.path_parameters["id"] == "f1"
    assert payload["moved_to"] == "dest"


def test_ms_move_missing_dest_without_make_parents(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ms_client(path_items={})
    with pytest.raises(DriveFolderNotFoundError):
        _ms_run(client, monkeypatch, "drive_move", item_id="f1", dest_path="Nope")


def test_ms_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    patched = SimpleNamespace(
        id="f1",
        name="renamed",
        file=SimpleNamespace(mime_type="text/plain"),
        folder=None,
        size=1,
        last_modified_date_time=None,
        web_url="u",
        parent_reference=None,
        created_by=None,
        last_modified_by=None,
    )
    payload = _ms_run(
        _ms_client(patched=patched), monkeypatch, "drive_rename", item_id="f1", name="renamed"
    )
    assert payload["item"]["name"] == "renamed"


# --------------------------------------------------------------------------- dispatch / consent


def test_write_verbs_require_yes() -> None:
    with pytest.raises(ConsentRequiredError):
        asyncio.run(run_skill("drive.rename", {"id": "x", "name": "y"}, config=_ms_cfg()))


def test_move_to_xor_to_id_is_usage_error() -> None:
    captured: dict[str, Any] = {}

    class _Prov:
        async def drive_move(self, **kw: Any) -> dict[str, Any]:
            captured.update(kw)
            from blumkin.skills.drive import validate_move_selector as _v

            _v(kw.get("dest_path"), kw.get("dest_folder_id"))
            return {}

    with pytest.raises(DriveSelectorError):
        asyncio.run(
            run_skill(
                "drive.move",
                {"id": "x", "to": "A", "to_id": "B", "yes": True},
                config=_ms_cfg(),
                provider=_Prov(),
            )
        )


def test_move_arg_remap(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Prov:
        async def drive_move(
            self,
            *,
            item_id: str,
            dest_folder_id: str | None = None,
            dest_path: str | None = None,
            make_parents: bool = False,
        ) -> dict[str, Any]:
            captured.update(
                item_id=item_id,
                dest_folder_id=dest_folder_id,
                dest_path=dest_path,
                make_parents=make_parents,
            )
            return {}

    asyncio.run(
        run_skill(
            "drive.move",
            {"id": "x", "to": "A/B", "make_parents": True, "yes": True},
            config=_ms_cfg(),
            provider=_Prov(),
        )
    )
    assert captured == {
        "item_id": "x",
        "dest_folder_id": None,
        "dest_path": "A/B",
        "make_parents": True,
    }
