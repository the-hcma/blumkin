"""Hermetic tests for `drive upload` on Google Drive and OneDrive (issue #555)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner
from googleapiclient.errors import HttpError
from msgraph.generated.models.drive_item import DriveItem
from msgraph.generated.models.folder import Folder
from msgraph.generated.models.o_data_errors.main_error import MainError
from msgraph.generated.models.o_data_errors.o_data_error import ODataError

from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig, PreferencesConfig
from blumkin.exit_codes import EXIT_SUCCESS
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.providers.microsoft import MicrosoftWorkspaceProvider
from blumkin.skills.dispatch import run_skill
from blumkin.skills.drive import (
    DriveUploadError,
    resolve_upload_source,
)
from blumkin.skills.errors import ConsentRequiredError, ScopeAddonDisabledError

_GOOGLE_MOD = "blumkin.providers.google.drive"
_MS_MOD = "blumkin.providers.microsoft_drive"
_FOLDER_MIME = "application/vnd.google-apps.folder"


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


def _ms_cfg(config_dir: Path, *, docs_scopes: bool = True) -> BlumkinConfig:
    return BlumkinConfig(
        client_id="abc",
        config_dir=config_dir,
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


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "work" / "photo.png"
    path.parent.mkdir()
    path.write_bytes(b"\x89PNG-bytes")
    return path


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    directory.mkdir()
    return directory


# ------------------------------------------------------------------ the local file


def test_a_regular_file_is_described_by_name_size_and_type(source: Path, config_dir: Path) -> None:
    found = resolve_upload_source(str(source), name=None, mime_type=None, config_dir=config_dir)
    assert found.path == source.resolve()
    assert found.name == "photo.png"
    assert found.size == len(b"\x89PNG-bytes")
    assert found.mime_type == "image/png"


def test_the_name_and_type_can_be_given(source: Path, config_dir: Path) -> None:
    found = resolve_upload_source(
        str(source), name="avatar.bin", mime_type="application/x-custom", config_dir=config_dir
    )
    assert (found.name, found.mime_type) == ("avatar.bin", "application/x-custom")


def test_an_unknown_extension_is_octet_stream(tmp_path: Path, config_dir: Path) -> None:
    path = tmp_path / "data.zzzunknown"
    path.write_bytes(b"x")
    found = resolve_upload_source(str(path), name=None, mime_type=None, config_dir=config_dir)
    assert found.mime_type == "application/octet-stream"


@pytest.mark.parametrize(
    "name", ["", "  ", "a/b.png", "a\\b.png", ".", "..", "x\x00y", "x\ny.png", "n" * 256]
)
def test_a_name_that_is_not_a_plain_file_name_is_refused(
    source: Path, config_dir: Path, name: str
) -> None:
    with pytest.raises(DriveUploadError):
        resolve_upload_source(str(source), name=name, mime_type=None, config_dir=config_dir)


@pytest.mark.parametrize("mime", ["png", "image", "image/", "/png", "text/plain; x=1", "a b/c"])
def test_a_mime_type_must_look_like_type_slash_subtype(
    source: Path, config_dir: Path, mime: str
) -> None:
    with pytest.raises(DriveUploadError):
        resolve_upload_source(str(source), name=None, mime_type=mime, config_dir=config_dir)


def test_a_missing_file_a_folder_and_a_special_file_are_refused(
    tmp_path: Path, config_dir: Path
) -> None:
    for target in (tmp_path / "missing.png", tmp_path):
        with pytest.raises(DriveUploadError):
            resolve_upload_source(str(target), name=None, mime_type=None, config_dir=config_dir)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(DriveUploadError):
        resolve_upload_source(str(fifo), name=None, mime_type=None, config_dir=config_dir)


def test_a_symlink_to_a_regular_file_uploads_that_file_under_the_links_name(
    source: Path, tmp_path: Path, config_dir: Path
) -> None:
    link = tmp_path / "link.png"
    link.symlink_to(source)
    found = resolve_upload_source(str(link), name=None, mime_type=None, config_dir=config_dir)
    assert found.path == source.resolve()
    assert found.name == "link.png"


def test_nothing_inside_blumkins_own_config_directory_can_be_uploaded(
    config_dir: Path,
) -> None:
    secret = config_dir / ".msal_token_cache.json"
    secret.write_text("tokens")
    nested = config_dir / "profiles" / "work"
    nested.mkdir(parents=True)
    (nested / "auth_record.json").write_text("record")
    for target in (secret, nested / "auth_record.json"):
        with pytest.raises(DriveUploadError, match="config"):
            resolve_upload_source(str(target), name=None, mime_type=None, config_dir=config_dir)


def test_a_symlink_into_the_config_directory_is_refused_too(
    tmp_path: Path, config_dir: Path
) -> None:
    secret = config_dir / "google_token.json"
    secret.write_text("tokens")
    link = tmp_path / "innocent.json"
    link.symlink_to(secret)
    with pytest.raises(DriveUploadError, match="config"):
        resolve_upload_source(str(link), name=None, mime_type=None, config_dir=config_dir)


def test_a_file_over_the_size_cap_is_refused(
    source: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("blumkin.skills.drive.UPLOAD_MAX_BYTES", 4)
    with pytest.raises(DriveUploadError, match="at most"):
        resolve_upload_source(str(source), name=None, mime_type=None, config_dir=config_dir)


# ------------------------------------------------------------------ Google Drive


def _google_patched(service: MagicMock) -> Any:
    return patch.multiple(
        _GOOGLE_MOD,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
        execute=MagicMock(side_effect=lambda request, **_kw: request.execute()),
    )


def _created(**extra: Any) -> dict[str, Any]:
    return {
        "id": "newid",
        "name": "photo.png",
        "mimeType": "image/png",
        "size": "10",
        "modifiedTime": "2026-10-28T12:00:00Z",
        "webViewLink": "https://drive.example/newid",
        "parents": ["root"],
        **extra,
    }


def _google_upload(service: MagicMock, config_dir: Path, **kwargs: Any) -> dict[str, Any]:
    with _google_patched(service):
        return asyncio.run(GoogleWorkspaceProvider(_google_cfg(config_dir)).drive_upload(**kwargs))


def test_google_small_upload_creates_one_file_in_the_root(source: Path, config_dir: Path) -> None:
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": []}
    files.create.return_value.execute.return_value = _created()

    payload = _google_upload(service, config_dir, file=str(source))

    kwargs = files.create.call_args.kwargs
    assert kwargs["body"] == {"name": "photo.png", "parents": ["root"]}
    assert kwargs["supportsAllDrives"] is True
    media = kwargs["media_body"]
    assert media.mimetype() == "image/png"
    assert media.resumable() is False
    assert payload["item"]["id"] == "newid"
    assert payload["item"]["web_url"] == "https://drive.example/newid"
    assert payload["item"]["provider"] == "google"
    assert payload["bytes"] == len(b"\x89PNG-bytes")


def test_google_upload_goes_into_a_folder_found_by_path(source: Path, config_dir: Path) -> None:
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.side_effect = [
        {"files": [{"id": "idA", "name": "Reports", "mimeType": _FOLDER_MIME}]},
        {"files": []},
    ]
    files.create.return_value.execute.return_value = _created(parents=["idA"])

    payload = _google_upload(service, config_dir, file=str(source), folder="Reports")

    assert files.create.call_args.kwargs["body"]["parents"] == ["idA"]
    assert payload["item"]["parent_id"] == "idA"


def test_google_upload_into_a_folder_that_does_not_exist_creates_nothing(
    source: Path, config_dir: Path
) -> None:
    from blumkin.skills.drive import DriveFolderNotFoundError

    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": []}
    with pytest.raises(DriveFolderNotFoundError):
        _google_upload(service, config_dir, file=str(source), folder="Missing")
    files.create.assert_not_called()


def test_google_upload_into_a_folder_id_checks_it_is_a_folder(
    source: Path, config_dir: Path
) -> None:
    service = MagicMock()
    files = service.files.return_value
    files.get.return_value.execute.return_value = {"id": "f1", "mimeType": "image/png"}
    with pytest.raises(ValueError, match="not a folder"):
        _google_upload(service, config_dir, file=str(source), folder_id="f1")
    files.create.assert_not_called()


def test_google_upload_never_adds_a_second_file_with_the_same_name(
    source: Path, config_dir: Path
) -> None:
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": [{"id": "old", "name": "photo.png"}]}
    with pytest.raises(DriveUploadError, match="already"):
        _google_upload(service, config_dir, file=str(source))
    files.create.assert_not_called()


def test_google_large_upload_is_resumable_and_sent_in_chunks(
    tmp_path: Path, config_dir: Path
) -> None:
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (6 * 1024 * 1024))
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": []}
    request = files.create.return_value
    request.next_chunk.side_effect = [
        (SimpleNamespace(progress=lambda: 0.5), None),
        (None, _created(name="big.bin", size=str(6 * 1024 * 1024))),
    ]

    payload = _google_upload(service, config_dir, file=str(big))

    media = files.create.call_args.kwargs["media_body"]
    assert media.resumable() is True
    assert request.next_chunk.call_count == 2
    request.execute.assert_not_called()
    assert payload["bytes"] == 6 * 1024 * 1024


def test_google_api_errors_surface_unchanged_for_the_caller_to_classify(
    source: Path, config_dir: Path
) -> None:
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": []}
    files.create.return_value.execute.side_effect = HttpError(
        SimpleNamespace(status=403, reason="forbidden"), b"{}"
    )
    with pytest.raises(HttpError):
        _google_upload(service, config_dir, file=str(source))


# ------------------------------------------------------------------ OneDrive


def _item(**fields: Any) -> DriveItem:
    item = DriveItem()
    item.id = fields.get("id", "msid")
    item.name = fields.get("name", "photo.png")
    item.size = fields.get("size", 10)
    item.web_url = fields.get("web_url", "https://onedrive.example/msid")
    return item


def _ms_client(send: Any) -> SimpleNamespace:
    return SimpleNamespace(request_adapter=SimpleNamespace(send_async=send))


def _ms_upload(client: Any, config_dir: Path, **kwargs: Any) -> dict[str, Any]:
    with patch(f"{_MS_MOD}.create_graph_client", return_value=client):
        return asyncio.run(MicrosoftWorkspaceProvider(_ms_cfg(config_dir)).drive_upload(**kwargs))


def test_onedrive_small_upload_is_one_put_that_fails_on_a_name_clash(
    source: Path, config_dir: Path
) -> None:
    send = AsyncMock(return_value=_item())

    payload = _ms_upload(_ms_client(send), config_dir, file=str(source))

    info = send.call_args.args[0]
    assert info.http_method.value == "PUT"
    assert "conflictBehavior=fail" in info.url_template
    assert "root:" in info.url_template
    assert info.path_parameters["path"] == "photo.png"
    assert payload["item"]["id"] == "msid"
    assert payload["item"]["provider"] == "microsoft"
    assert payload["bytes"] == len(b"\x89PNG-bytes")


def test_onedrive_upload_into_a_folder_found_by_path_puts_under_its_id(
    source: Path, config_dir: Path
) -> None:
    folder = _item(id="fid", name="Reports")
    folder.folder = Folder()
    send = AsyncMock(return_value=_item())
    with patch(f"{_MS_MOD}._resolve_folder", AsyncMock(return_value=folder)):
        _ms_upload(_ms_client(send), config_dir, file=str(source), folder="Reports")
    info = send.call_args.args[0]
    assert "items/{parent}:" in info.url_template
    assert info.path_parameters["parent"] == "fid"


def test_onedrive_upload_into_a_missing_folder_sends_nothing(
    source: Path, config_dir: Path
) -> None:
    from blumkin.skills.drive import DriveFolderNotFoundError

    send = AsyncMock()
    with (
        patch(f"{_MS_MOD}._resolve_folder", AsyncMock(return_value=None)),
        pytest.raises(DriveFolderNotFoundError),
    ):
        _ms_upload(_ms_client(send), config_dir, file=str(source), folder="Missing")
    send.assert_not_called()


def test_onedrive_name_clash_is_a_clear_error_and_nothing_is_replaced(
    source: Path, config_dir: Path
) -> None:
    error = ODataError()
    error.response_status_code = 409
    error.error = MainError()
    error.error.code = "nameAlreadyExists"
    send = AsyncMock(side_effect=error)

    with pytest.raises(DriveUploadError, match="already"):
        _ms_upload(_ms_client(send), config_dir, file=str(source))


def test_onedrive_large_upload_uses_a_session_in_aligned_chunks_without_a_token(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = tmp_path / "big.bin"
    payload_bytes = os.urandom(5 * 1024 * 1024)
    big.write_bytes(payload_bytes)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        end = int(request.headers["Content-Range"].split("-")[1].split("/")[0])
        total = int(request.headers["Content-Range"].split("/")[1])
        if end + 1 < total:
            return httpx.Response(202, json={"nextExpectedRanges": [f"{end + 1}-"]})
        return httpx.Response(201, json={"id": "bigid", "name": "big.bin", "size": total})

    monkeypatch.setattr(
        f"{_MS_MOD}._session_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    send = AsyncMock(return_value=SimpleNamespace(upload_url="https://upload.example/session/1"))

    payload = _ms_upload(_ms_client(send), config_dir, file=str(big))

    session_request = send.call_args.args[0]
    assert session_request.http_method.value == "POST"
    assert b"conflictBehavior" in session_request.content and b"fail" in session_request.content
    assert len(seen) >= 2
    received = b"".join(request.content for request in seen)
    assert received == payload_bytes
    for request in seen[:-1]:
        assert len(request.content) % (320 * 1024) == 0
    assert all("Authorization" not in request.headers for request in seen)
    assert all(str(request.url) == "https://upload.example/session/1" for request in seen)
    assert payload["item"]["id"] == "bigid"
    assert payload["bytes"] == len(payload_bytes)


def test_onedrive_large_upload_cancels_the_session_when_a_chunk_fails(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (5 * 1024 * 1024))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(500, json={"error": {"message": "boom"}})

    monkeypatch.setattr(
        f"{_MS_MOD}._session_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    send = AsyncMock(return_value=SimpleNamespace(upload_url="https://upload.example/session/1"))

    with pytest.raises(DriveUploadError, match="upload"):
        _ms_upload(_ms_client(send), config_dir, file=str(big))

    assert calls[-1] == "DELETE"


# ------------------------------------------------------------------ gates and CLI


def test_upload_needs_yes(source: Path, config_dir: Path) -> None:
    with pytest.raises(ConsentRequiredError):
        asyncio.run(
            run_skill("drive.upload", {"file": str(source)}, config=_google_cfg(config_dir))
        )


def test_upload_on_microsoft_needs_the_files_toggle(source: Path, config_dir: Path) -> None:
    with pytest.raises(ScopeAddonDisabledError):
        asyncio.run(
            run_skill(
                "drive.upload",
                {"file": str(source), "yes": True},
                config=_ms_cfg(config_dir, docs_scopes=False),
            )
        )


def test_the_skill_passes_its_arguments_to_the_provider(source: Path, config_dir: Path) -> None:
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": []}
    files.create.return_value.execute.return_value = _created()
    with _google_patched(service):
        result = asyncio.run(
            run_skill(
                "drive.upload",
                {"file": str(source), "name": "x.png", "mime_type": "image/png", "yes": True},
                config=_google_cfg(config_dir),
            )
        )
    assert result["item"]["id"] == "newid"
    assert files.create.call_args.kwargs["body"]["name"] == "x.png"


def test_the_cli_uploads_and_prints_the_link(
    source: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config_dir))
    (config_dir / "config.toml").write_text(
        '[profiles.default]\nclient_id = "abc-123"\nprovider = "microsoft"\n', encoding="utf-8"
    )
    result_payload = {
        "item": {
            "id": "newid",
            "name": "photo.png",
            "kind": "file",
            "size": 10,
            "web_url": "https://drive.example/newid",
            "provider": "google",
        },
        "bytes": 10,
    }
    with patch("blumkin.cli.run_skill", AsyncMock(return_value=result_payload)) as run:
        shown = CliRunner().invoke(
            main, ["drive", "upload", "--file", str(source), "--folder", "Reports", "--yes"]
        )
        as_json = CliRunner().invoke(
            main, ["--json", "drive", "upload", "--file", str(source), "--yes"]
        )
    assert shown.exit_code == EXIT_SUCCESS, shown.output
    assert "https://drive.example/newid" in shown.output
    assert json.loads(as_json.output)["item"]["id"] == "newid"
    first = run.await_args_list[0]
    assert first.args[0] == "drive.upload"
    assert first.args[1]["file"] == str(source)
    assert first.args[1]["folder"] == "Reports"
    assert first.args[1]["yes"] is True
