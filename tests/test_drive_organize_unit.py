"""Hermetic tests for `drive mkdir` / `drive move` / `drive rename` (#212)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.exit_codes import EXIT_USAGE
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.providers.microsoft import MicrosoftWorkspaceProvider
from blumkin.skills.dispatch import run_skill
from blumkin.skills.drive import (
    DriveFolderNotFoundError,
    DriveItemNotFoundError,
    DriveSelectorError,
    validate_move_selector,
)
from blumkin.skills.errors import ConsentRequiredError, classify_exception

_GOOGLE_MOD = "blumkin.providers.google.drive"
_MS_MOD = "blumkin.providers.microsoft_drive"


def test_validate_move_selector_is_xor() -> None:
    validate_move_selector("A/B", None)
    validate_move_selector(None, "id")
    with pytest.raises(DriveSelectorError):
        validate_move_selector(None, None)
    with pytest.raises(DriveSelectorError):
        validate_move_selector("A/B", "id")


def test_validate_move_selector_treats_blank_as_absent() -> None:
    # An empty / whitespace value must not slip past the gate and silently
    # reparent into the drive root.
    for path, fid in (("", None), ("  ", None), (None, ""), ("", ""), (" ", " ")):
        with pytest.raises(DriveSelectorError):
            validate_move_selector(path, fid)


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


_FOLDER_MIME = "application/vnd.google-apps.folder"


def _google_get_by_id(rows: dict[str, dict[str, Any]]) -> Any:
    """`files().get(fileId=...)` -> a request whose .execute() returns rows[fileId]."""

    def _get(*, fileId: str, **_kw: Any) -> Any:  # noqa: N803 - Google API kwarg name
        req = MagicMock()
        req.execute.return_value = rows[fileId]
        return req

    return _get


def test_google_move_reparents(tmp_path: Path) -> None:
    service = MagicMock()
    files = service.files.return_value
    files.get.side_effect = _google_get_by_id(
        {
            "f1": {"id": "f1", "name": "doc", "parents": ["old"]},
            "dest": {"id": "dest", "mimeType": _FOLDER_MIME},
        }
    )
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
    assert kw["supportsAllDrives"] is True
    assert payload["moved_to"] == "dest" and payload["item"]["parent_id"] == "dest"


def test_google_move_to_id_must_be_a_folder(tmp_path: Path) -> None:
    service = MagicMock()
    service.files.return_value.get.side_effect = _google_get_by_id(
        {
            "f1": {"id": "f1", "parents": ["old"]},
            "notfolder": {"id": "notfolder", "mimeType": "text/plain"},
        }
    )
    with _google_patched(service), pytest.raises(ValueError) as exc:
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_move(
                item_id="f1", dest_folder_id="notfolder"
            )
        )
    assert not isinstance(exc.value, LookupError)
    service.files.return_value.update.assert_not_called()


def test_google_move_refuses_a_multi_parent_item(tmp_path: Path) -> None:
    service = MagicMock()
    service.files.return_value.get.side_effect = _google_get_by_id(
        {"f1": {"id": "f1", "name": "legacy", "parents": ["A", "B"]}}
    )
    with _google_patched(service), pytest.raises(ValueError) as exc:
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_move(
                item_id="f1", dest_folder_id="C"
            )
        )
    assert not isinstance(exc.value, LookupError)
    service.files.return_value.update.assert_not_called()  # visibility elsewhere unchanged


def test_google_validate_dest_folder_id_propagates_non_404(tmp_path: Path) -> None:
    service = MagicMock()
    resp = SimpleNamespace(status=503, reason="Service Unavailable")

    def _get(*, fileId: str, **_kw: Any) -> Any:  # noqa: N803
        if fileId == "f1":
            req = MagicMock()
            req.execute.return_value = {"id": "f1", "parents": ["old"]}
            return req
        raise HttpError(resp, b"busy")

    service.files.return_value.get.side_effect = _get
    with _google_patched(service), pytest.raises(Exception) as exc:  # noqa: PT011
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_move(
                item_id="f1", dest_folder_id="valid-but-backend-down"
            )
        )
    # A 5xx must NOT be reported as a usage error about the id.
    assert not isinstance(exc.value, ValueError)


def test_google_move_validates_source_before_creating_parents(tmp_path: Path) -> None:
    service = MagicMock()
    resp = SimpleNamespace(status=404, reason="Not Found")
    service.files.return_value.get.side_effect = HttpError(resp, b"nope")
    with _google_patched(service), pytest.raises(DriveItemNotFoundError):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_move(
                item_id="ghost", dest_path="New/Chain", make_parents=True
            )
        )
    service.files.return_value.create.assert_not_called()  # no stray folders


def test_google_move_missing_dest_path_is_usage_error(tmp_path: Path) -> None:
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {"files": []}
    with _google_patched(service), pytest.raises(ValueError) as exc:
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_move(
                item_id="f1", dest_path="Nope"
            )
        )
    # A LookupError would classify as not_found (exit 5); this must be usage_error (2).
    assert not isinstance(exc.value, LookupError)


def test_google_move_to_drive_root_is_refused(tmp_path: Path) -> None:
    with _google_patched(MagicMock()), pytest.raises(ValueError):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_move(item_id="f1", dest_path="/")
        )


def test_move_missing_dest_classifies_as_usage_error(tmp_path: Path) -> None:
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {"files": []}
    with _google_patched(service), pytest.raises(ValueError) as exc:
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_move(
                item_id="f1", dest_path="Nope"
            )
        )
    assert classify_exception(exc.value).exit_code == EXIT_USAGE


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


def _odata_error(status: int) -> Exception:
    from msgraph.generated.models.o_data_errors.o_data_error import ODataError

    return ODataError(response_status_code=status)


def _ms_item(item_id: str, name: str, *, folder: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id=item_id,
        name=name,
        web_url="u",
        size=1,
        file=None if folder else SimpleNamespace(mime_type="text/plain"),
        folder=SimpleNamespace(child_count=0) if folder else None,
        last_modified_date_time=None,
        parent_reference=SimpleNamespace(id="p", path=""),
        created_by=None,
        last_modified_by=None,
    )


def _ms_folder(item_id: str, name: str) -> SimpleNamespace:
    return _ms_item(item_id, name, folder=True)


def _ms_file(item_id: str, name: str) -> SimpleNamespace:
    return _ms_item(item_id, name, folder=False)


def _ms_client(
    *, by_id: dict[str, Any] | None = None, path_items: dict[str, Any] | None = None
) -> MagicMock:
    """Routes by URL template: `/items/{id}` -> by_id, `/root:/{+path}` -> path_items,
    POST -> a canned new folder. `client.patched` records every `_patch_item` call."""
    client = MagicMock()
    by_id = by_id or {}
    path_items = path_items or {}
    sent: list[Any] = []

    async def send_async(ri: Any, _f: Any, _e: Any) -> Any:
        sent.append(ri)
        tmpl = ri.url_template
        if ri.http_method.name == "POST":
            return SimpleNamespace(id="new-folder", name="leaf", web_url="u", folder=object())
        if "root:/{+path}" in tmpl:
            return path_items.get(ri.path_parameters.get("path"))
        return by_id.get(ri.path_parameters.get("id"))

    client.request_adapter.send_async = AsyncMock(side_effect=send_async)
    client.sent = sent
    client.patched = []
    return client


def _ms_run(client: MagicMock, monkeypatch: pytest.MonkeyPatch, method: str, **kw: Any):
    async def spy_patch(_client: Any, item_id: str, patch: Any) -> Any:
        client.patched.append((item_id, patch))
        return _ms_item(item_id, patch.name or "doc", folder=False)

    monkeypatch.setattr(f"{_MS_MOD}.create_graph_client", lambda _c: client)
    monkeypatch.setattr(f"{_MS_MOD}._patch_item", spy_patch)
    return asyncio.run(getattr(MicrosoftWorkspaceProvider(_ms_cfg()), method)(**kw))


def test_ms_mkdir_creates_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ms_client(path_items={"A": _ms_folder("idA", "A")})
    payload = _ms_run(client, monkeypatch, "drive_mkdir", path="A/B")
    assert payload["created"] is True
    assert any(ri.http_method.name == "POST" for ri in client.sent)


def test_ms_mkdir_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ms_client(path_items={"A/B": _ms_folder("idAB", "B")})
    payload = _ms_run(client, monkeypatch, "drive_mkdir", path="A/B")
    assert payload["created"] is False and payload["folder"]["id"] == "idAB"


def test_ms_mkdir_refuses_a_file_in_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ms_client(path_items={"A/B": _ms_file("f", "B")})
    with pytest.raises(DriveFolderNotFoundError):
        _ms_run(client, monkeypatch, "drive_mkdir", path="A/B")


def test_ms_move_to_a_file_path_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ms_client(
        by_id={"x": _ms_file("x", "src")}, path_items={"Reports": _ms_file("f", "R")}
    )
    with pytest.raises(ValueError) as exc:
        _ms_run(client, monkeypatch, "drive_move", item_id="x", dest_path="Reports")
    assert not isinstance(exc.value, LookupError)


def test_ms_move_patches_parent_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ms_client(by_id={"f1": _ms_file("f1", "doc"), "dest": _ms_folder("dest", "Dest")})
    payload = _ms_run(client, monkeypatch, "drive_move", item_id="f1", dest_folder_id="dest")
    (item_id, patch) = client.patched[0]
    assert item_id == "f1"
    assert patch.parent_reference is not None and patch.parent_reference.id == "dest"
    assert payload["moved_to"] == "dest"


def test_ms_move_to_id_must_be_an_existing_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    # A file id or a missing id passed as --to-id is a usage error, not a raw PATCH 400.
    client = _ms_client(by_id={"f1": _ms_file("f1", "doc"), "afile": _ms_file("afile", "x.txt")})
    with pytest.raises(ValueError) as exc:
        _ms_run(client, monkeypatch, "drive_move", item_id="f1", dest_folder_id="afile")
    assert not isinstance(exc.value, LookupError) and not client.patched

    client2 = _ms_client(by_id={"f1": _ms_file("f1", "doc")})
    with pytest.raises(ValueError):
        _ms_run(client2, monkeypatch, "drive_move", item_id="f1", dest_folder_id="ghost")


def test_ms_mkdir_survives_a_lost_race_409(monkeypatch: pytest.MonkeyPatch) -> None:
    # First POST loses the race; the re-read then finds the concurrently-made folder.
    client = MagicMock()
    client.sent = []
    client.patched = []
    state = {"posted": False}

    async def send_async(ri: Any, _f: Any, _e: Any) -> Any:
        client.sent.append(ri)
        if ri.http_method.name == "POST":
            state["posted"] = True
            raise _odata_error(409)
        # GET by path: "A" always exists; "A/B" appears only after the POST attempt.
        path = ri.path_parameters.get("path")
        if path == "A":
            return _ms_folder("idA", "A")
        if path == "A/B" and state["posted"]:
            return _ms_folder("idAB", "B")
        return None

    client.request_adapter.send_async = AsyncMock(side_effect=send_async)
    payload = _ms_run(client, monkeypatch, "drive_mkdir", path="A/B")
    assert payload["created"] is True and payload["folder"]["id"] == "idAB"


def test_ms_move_validates_source_before_creating_parents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bad --id + --make-parents must not create the destination chain.
    client = _ms_client(by_id={}, path_items={})
    with pytest.raises(DriveItemNotFoundError):
        _ms_run(
            client,
            monkeypatch,
            "drive_move",
            item_id="ghost",
            dest_path="New/Chain",
            make_parents=True,
        )
    assert not [ri for ri in client.sent if ri.http_method.name == "POST"]


def test_ms_move_missing_dest_is_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ms_client(by_id={"f1": _ms_file("f1", "src")}, path_items={})
    with pytest.raises(ValueError) as exc:
        _ms_run(client, monkeypatch, "drive_move", item_id="f1", dest_path="Nope")
    assert not isinstance(exc.value, LookupError)  # usage_error (2), not not_found (5)


def test_ms_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ms_client()
    payload = _ms_run(client, monkeypatch, "drive_rename", item_id="f1", name="renamed")
    (item_id, patch) = client.patched[0]
    assert item_id == "f1" and patch.name == "renamed" and patch.parent_reference is None
    assert payload["item"]["name"] == "renamed"


# --------------------------------------------------------------------------- dispatch / consent


@pytest.mark.parametrize(
    ("skill", "args"),
    [
        ("drive.mkdir", {"path": "A"}),
        ("drive.move", {"id": "x", "to": "A/B"}),
        ("drive.rename", {"id": "x", "name": "y"}),
    ],
)
def test_write_verbs_require_yes(skill: str, args: dict[str, Any]) -> None:
    # The gate is spec-driven (per-SkillSpec `--yes required`), so assert all three.
    with pytest.raises(ConsentRequiredError):
        asyncio.run(run_skill(skill, args, config=_ms_cfg()))


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
