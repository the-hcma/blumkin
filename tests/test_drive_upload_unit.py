"""Hermetic tests for `drive upload` on Google Drive and OneDrive (issue #555)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
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
    DriveItemNotFoundError,
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


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
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
    monkeypatch.setattr(f"{_MS_MOD}._pause", lambda seconds: None)
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


# ------------------------------------------------------------------ what must never leave


def test_a_path_is_judged_by_which_directory_it_is_in_not_by_how_it_is_spelled(
    config_dir: Path, tmp_path: Path
) -> None:
    from blumkin.skills.drive import path_is_inside

    (config_dir / "profiles").mkdir()
    (config_dir / "profiles" / "t.json").write_text("x")
    alias = tmp_path / "alias"
    alias.symlink_to(config_dir)
    unresolved = alias / "profiles" / "t.json"
    assert not unresolved.is_relative_to(config_dir)
    assert path_is_inside(unresolved, config_dir)
    assert not path_is_inside(tmp_path / "work", config_dir)


def test_a_hard_link_to_a_secret_is_refused_even_outside_the_config_directory(
    tmp_path: Path, config_dir: Path
) -> None:
    secret = config_dir / "google_token.json"
    secret.write_text("tokens")
    linked = tmp_path / "innocent.json"
    os.link(secret, linked)
    with pytest.raises(DriveUploadError, match="link"):
        resolve_upload_source(str(linked), name=None, mime_type=None, config_dir=config_dir)


@pytest.mark.parametrize(
    "relative",
    [".ssh/id_rsa", ".aws/credentials", ".gnupg/key", ".config/gcloud/creds.json", ".netrc"],
)
def test_well_known_secret_locations_are_refused(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    home = tmp_path / "home"
    target = home / relative
    target.parent.mkdir(parents=True)
    target.write_text("secret")
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(DriveUploadError, match="secret"):
        resolve_upload_source(str(target), name=None, mime_type=None, config_dir=config_dir)


def test_an_env_file_is_refused_wherever_it_is(tmp_path: Path, config_dir: Path) -> None:
    env = tmp_path / "project" / ".env"
    env.parent.mkdir()
    env.write_text("KEY=1")
    with pytest.raises(DriveUploadError, match="secret"):
        resolve_upload_source(str(env), name=None, mime_type=None, config_dir=config_dir)


def test_the_google_oauth_client_file_is_refused_wherever_it_lives(
    tmp_path: Path, config_dir: Path
) -> None:
    client = tmp_path / "downloads" / "client_secret.json"
    client.parent.mkdir()
    client.write_text("{}")
    with pytest.raises(DriveUploadError, match="secret"):
        resolve_upload_source(
            str(client), name=None, mime_type=None, config_dir=config_dir, denied=(client,)
        )


def test_the_checked_file_is_the_one_that_is_read_even_if_it_is_swapped_afterwards(
    source: Path, tmp_path: Path, config_dir: Path
) -> None:
    found = resolve_upload_source(str(source), name=None, mime_type=None, config_dir=config_dir)
    try:
        secret = config_dir / "google_token.json"
        secret.write_text("tokens")
        os.replace(secret, source)  # the name now leads to a different file
        assert found.handle.read() == b"\x89PNG-bytes"
    finally:
        found.close()


def test_a_file_that_changes_while_it_is_sent_is_detected(source: Path, config_dir: Path) -> None:
    found = resolve_upload_source(str(source), name=None, mime_type=None, config_dir=config_dir)
    try:
        found.ensure_unchanged()
        with source.open("ab") as handle:
            handle.write(b"more")
        with pytest.raises(DriveUploadError, match="changed"):
            found.ensure_unchanged()
    finally:
        found.close()


def test_the_google_upload_reads_from_the_checked_handle(source: Path, config_dir: Path) -> None:
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": []}
    request = MagicMock()
    request.execute.return_value = _created()
    sent: list[bytes] = []

    def create(**kwargs: Any) -> MagicMock:
        sent.append(kwargs["media_body"].getbytes(0, 100))
        return request

    files.create.side_effect = create
    _google_upload(service, config_dir, file=str(source))
    assert sent == [b"\x89PNG-bytes"]


def test_google_upload_that_the_service_rejects_is_translated_like_the_other_verbs(
    source: Path, config_dir: Path
) -> None:
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": []}
    files.create.return_value.execute.side_effect = HttpError(
        SimpleNamespace(status=404, reason="not found"), b"{}"
    )
    with pytest.raises(DriveItemNotFoundError):
        _google_upload(service, config_dir, file=str(source))


def test_google_upload_with_no_file_in_the_answer_is_an_error_not_a_success(
    source: Path, config_dir: Path
) -> None:
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": []}
    files.create.return_value.execute.return_value = None
    with pytest.raises(RuntimeError, match="no file"):
        _google_upload(service, config_dir, file=str(source))


@pytest.mark.parametrize(
    "name", ["a:b.txt", "a*b.txt", 'a"b.txt', "a<b.txt", "a|b.txt", "a?.txt", "ends."]
)
def test_onedrive_names_it_cannot_hold_are_refused_before_anything_is_sent(
    source: Path, config_dir: Path, name: str
) -> None:
    send = AsyncMock()
    with pytest.raises(DriveUploadError, match="OneDrive"):
        _ms_upload(_ms_client(send), config_dir, file=str(source), name=name)
    send.assert_not_called()


def test_the_session_client_honours_the_proxy_and_certificate_environment() -> None:
    from blumkin.providers.microsoft_drive import _session_client

    with _session_client() as client:
        assert client.trust_env is True


def _big(tmp_path: Path) -> Path:
    big = tmp_path / "big.bin"
    big.write_bytes(os.urandom(5 * 1024 * 1024))
    return big


def _session_send() -> AsyncMock:
    return AsyncMock(return_value=SimpleNamespace(upload_url="https://upload.example/session/1"))


def test_a_chunk_that_gets_a_server_error_is_retried_and_the_upload_still_completes(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = _big(tmp_path)
    attempts = {"n": 0}
    pauses: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        total = int(request.headers["Content-Range"].split("/")[1])
        end = int(request.headers["Content-Range"].split("-")[1].split("/")[0])
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, headers={"Retry-After": "2"})
        if end + 1 < total:
            return httpx.Response(202)
        return httpx.Response(201, json={"id": "bigid", "name": "big.bin", "size": total})

    monkeypatch.setattr(
        f"{_MS_MOD}._session_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(f"{_MS_MOD}._pause", pauses.append)

    payload = _ms_upload(_ms_client(_session_send()), config_dir, file=str(big))

    assert payload["item"]["id"] == "bigid"
    assert pauses == [2.0]


def test_a_timeout_on_a_chunk_is_retried_too(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = _big(tmp_path)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        total = int(request.headers["Content-Range"].split("/")[1])
        end = int(request.headers["Content-Range"].split("-")[1].split("/")[0])
        if end + 1 < total:
            return httpx.Response(202)
        return httpx.Response(201, json={"id": "bigid", "name": "big.bin", "size": total})

    monkeypatch.setattr(
        f"{_MS_MOD}._session_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(f"{_MS_MOD}._pause", lambda seconds: None)

    assert (
        _ms_upload(_ms_client(_session_send()), config_dir, file=str(big))["item"]["id"] == "bigid"
    )


def test_a_chunk_that_keeps_failing_gives_up_after_a_few_tries_and_cancels(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = _big(tmp_path)
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(204) if request.method == "DELETE" else httpx.Response(503)

    monkeypatch.setattr(
        f"{_MS_MOD}._session_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(f"{_MS_MOD}._pause", lambda seconds: None)

    with pytest.raises(DriveUploadError, match="503"):
        _ms_upload(_ms_client(_session_send()), config_dir, file=str(big))

    assert methods.count("PUT") == 4  # one try and three retries
    assert methods[-1] == "DELETE"


@pytest.mark.parametrize(
    "url",
    ["https://127.0.0.1/s", "https://localhost/s", "https://[::1]/s", "http://upload.example/s"],
)
def test_an_upload_session_url_must_be_a_public_https_address(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    big = _big(tmp_path)
    send = AsyncMock(return_value=SimpleNamespace(upload_url=url))
    with pytest.raises(DriveUploadError, match="session"):
        _ms_upload(_ms_client(send), config_dir, file=str(big))


def test_a_final_answer_that_is_not_json_is_an_upload_error_and_cancels_the_session(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = _big(tmp_path)
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        end = (
            int(request.headers["Content-Range"].split("-")[1].split("/")[0])
            if request.method == "PUT"
            else 0
        )
        total = (
            int(request.headers["Content-Range"].split("/")[1]) if request.method == "PUT" else 0
        )
        if request.method == "PUT" and end + 1 < total:
            return httpx.Response(202)
        return (
            httpx.Response(204) if request.method == "DELETE" else httpx.Response(201, text="oops")
        )

    monkeypatch.setattr(
        f"{_MS_MOD}._session_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(DriveUploadError, match="answer"):
        _ms_upload(_ms_client(_session_send()), config_dir, file=str(big))


def test_a_file_that_changes_during_a_large_upload_cancels_the_session(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = _big(tmp_path)
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "PUT" and len(methods) == 1:
            with big.open("ab") as handle:
                handle.write(b"grown")
            return httpx.Response(202)
        return httpx.Response(204) if request.method == "DELETE" else httpx.Response(202)

    monkeypatch.setattr(
        f"{_MS_MOD}._session_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(DriveUploadError, match="changed"):
        _ms_upload(_ms_client(_session_send()), config_dir, file=str(big))
    assert methods[-1] == "DELETE"


@pytest.mark.parametrize("folder", ["/", "//", " / "])
def test_a_folder_with_no_name_in_it_means_the_drive_root_on_both_services(
    source: Path, config_dir: Path, folder: str
) -> None:
    send = AsyncMock(return_value=_item())
    resolve = AsyncMock()
    with patch(f"{_MS_MOD}._resolve_folder", resolve):
        _ms_upload(_ms_client(send), config_dir, file=str(source), folder=folder)
    resolve.assert_not_called()
    assert "root:" in send.call_args.args[0].url_template

    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": []}
    files.create.return_value.execute.return_value = _created()
    _google_upload(service, config_dir, file=str(source), folder=folder)
    assert files.create.call_args.kwargs["body"]["parents"] == ["root"]


def test_a_missing_destination_has_the_exit_code_the_docs_name(
    source: Path, config_dir: Path
) -> None:
    from blumkin.skills.drive import DriveFolderNotFoundError
    from blumkin.skills.errors import classify_exception

    # A missing folder path is not_found (exit 5) on both services.
    assert classify_exception(DriveFolderNotFoundError("no folder at 'X'")).exit_code == 5
    # A missing Microsoft --folder-id is not_found (5); a missing Google one is a usage error (2).
    send = AsyncMock(side_effect=DriveItemNotFoundError("no drive item with id 'x'"))
    with patch(f"{_MS_MOD}._send_item", send), pytest.raises(DriveItemNotFoundError) as ms:
        _ms_upload(_ms_client(AsyncMock()), config_dir, file=str(source), folder_id="x")
    assert classify_exception(ms.value).exit_code == 5
    service = MagicMock()
    service.files.return_value.get.return_value.execute.side_effect = HttpError(
        SimpleNamespace(status=404, reason="nf"), b"{}"
    )
    with pytest.raises(ValueError, match="does not name a drive item") as google:
        _google_upload(service, config_dir, file=str(source), folder_id="x")
    assert classify_exception(google.value).exit_code == 2


def test_a_chunk_whose_answer_was_lost_resumes_from_where_the_session_says_it_is(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = _big(tmp_path)
    chunk = 320 * 1024 * 12
    log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            log.append("status")
            return httpx.Response(200, json={"nextExpectedRanges": [f"{chunk}-"]})
        rng = request.headers["Content-Range"]
        start = int(rng.split(" ")[1].split("-")[0])
        end = int(rng.split("-")[1].split("/")[0])
        total = int(rng.split("/")[1])
        log.append(f"put:{start}")
        if log.count(f"put:{start}") == 1 and start == 0:
            raise httpx.ReadTimeout("answer lost", request=request)  # the server kept the bytes
        if start == 0:
            return httpx.Response(416)
        if end + 1 < total:
            return httpx.Response(202)
        return httpx.Response(201, json={"id": "bigid", "name": "big.bin", "size": total})

    monkeypatch.setattr(
        f"{_MS_MOD}._session_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(f"{_MS_MOD}._pause", lambda seconds: None)

    payload = _ms_upload(_ms_client(_session_send()), config_dir, file=str(big))

    assert payload["item"]["id"] == "bigid"
    assert "status" in log
    assert log.count("put:0") == 2 and f"put:{chunk}" in log


def test_a_final_chunk_whose_answer_was_lost_is_not_reported_as_a_name_clash(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = _big(tmp_path)
    last_seen = {"n": 0}
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method in {"DELETE", "GET"}:
            return httpx.Response(404 if request.method == "GET" else 204)
        rng = request.headers["Content-Range"]
        end = int(rng.split("-")[1].split("/")[0])
        total = int(rng.split("/")[1])
        if end + 1 < total:
            return httpx.Response(202)
        last_seen["n"] += 1
        if last_seen["n"] == 1:
            raise httpx.ReadTimeout("answer lost", request=request)
        return httpx.Response(409)

    monkeypatch.setattr(
        f"{_MS_MOD}._session_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(f"{_MS_MOD}._pause", lambda seconds: None)

    with pytest.raises(DriveUploadError, match="may have completed") as caught:
        _ms_upload(_ms_client(_session_send()), config_dir, file=str(big))

    assert "already exists" not in str(caught.value)


def test_a_retry_after_that_is_not_a_finite_number_falls_back_to_the_backoff(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = _big(tmp_path)
    pauses: list[float] = []
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "nan"})
        rng = request.headers["Content-Range"]
        end = int(rng.split("-")[1].split("/")[0])
        total = int(rng.split("/")[1])
        if end + 1 < total:
            return httpx.Response(202)
        return httpx.Response(201, json={"id": "bigid", "name": "big.bin", "size": total})

    monkeypatch.setattr(
        f"{_MS_MOD}._session_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(f"{_MS_MOD}._pause", pauses.append)

    assert _ms_upload(_ms_client(_session_send()), config_dir, file=str(big))["item"]["id"]
    assert pauses == [1.0]


def test_a_file_that_changed_after_it_was_uploaded_names_the_copy_that_was_left(
    source: Path, config_dir: Path
) -> None:
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": []}

    def create(**kwargs: Any) -> MagicMock:
        with source.open("ab") as handle:
            handle.write(b"changed")
        request = MagicMock()
        request.execute.return_value = _created()
        return request

    files.create.side_effect = create
    with pytest.raises(DriveUploadError, match="changed") as caught:
        _google_upload(service, config_dir, file=str(source))
    assert "newid" in str(caught.value)


def test_a_path_that_leads_somewhere_else_by_the_time_it_is_opened_is_refused(
    source: Path, tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other.png"
    other.write_bytes(b"other")
    real = Path.resolve
    calls = {"n": 0}

    def swapping(self: Path, *args: Any, **kwargs: Any) -> Path:
        if self == source:
            calls["n"] += 1
            return real(other) if calls["n"] > 1 else real(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", swapping)
    with pytest.raises(DriveUploadError, match="changed"):
        resolve_upload_source(str(source), name=None, mime_type=None, config_dir=config_dir)


def test_an_unknown_home_directory_skips_the_credential_folders_instead_of_crashing(
    source: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", staticmethod(no_home))
    found = resolve_upload_source(str(source), name=None, mime_type=None, config_dir=config_dir)
    found.close()
    assert found.name == "photo.png"


@pytest.mark.parametrize(
    "name",
    [
        "CON",
        "con.txt",
        "NUL.png",
        "COM1",
        "lpt9.txt",
        "desktop.ini",
        ".lock",
        "~$draft.docx",
        "a_vti_b.txt",
    ],
)
def test_onedrive_reserved_names_are_refused_before_anything_is_sent(
    source: Path, config_dir: Path, name: str
) -> None:
    send = AsyncMock()
    with pytest.raises(DriveUploadError, match="OneDrive"):
        _ms_upload(_ms_client(send), config_dir, file=str(source), name=name)
    send.assert_not_called()


def test_upload_errors_are_usage_errors_whatever_the_file_name_says() -> None:
    from blumkin.skills.errors import classify_exception

    for text in (
        "a file named 'Missing_Q3.docx' already exists in that folder",
        "a file named 'client_id.txt' already exists in that folder",
    ):
        info = classify_exception(DriveUploadError(text))
        assert (info.slug, info.exit_code) == ("usage_error", 2), text


def test_the_other_drive_value_errors_are_classified_by_type_too() -> None:
    from blumkin.skills.drive import (
        DriveDownloadError,
        DriveExportError,
        DriveFolderAmbiguousError,
        DriveReadUnsupportedError,
        DriveSelectorError,
    )
    from blumkin.skills.errors import classify_exception

    for error in (
        DriveDownloadError("'Missing client_id' is a folder"),
        DriveExportError("Missing format"),
        DriveFolderAmbiguousError("Missing client_id twice"),
        DriveReadUnsupportedError("Missing"),
        DriveSelectorError("Missing client_id"),
    ):
        info = classify_exception(error)
        assert (info.slug, info.exit_code) == ("usage_error", 2), error


def test_a_large_google_upload_does_not_run_on_the_event_loop_thread(
    tmp_path: Path, config_dir: Path
) -> None:
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (6 * 1024 * 1024))
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": []}
    seen: list[int] = []

    def next_chunk(**kwargs: Any) -> tuple[None, dict[str, Any]]:
        seen.append(threading.get_ident())
        return None, _created(name="big.bin")

    files.create.return_value.next_chunk.side_effect = next_chunk
    _google_upload(service, config_dir, file=str(big))
    assert seen and threading.get_ident() not in seen


def test_a_large_onedrive_upload_does_not_run_on_the_event_loop_thread(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = _big(tmp_path)
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(threading.get_ident())
        rng = request.headers["Content-Range"]
        end = int(rng.split("-")[1].split("/")[0])
        total = int(rng.split("/")[1])
        if end + 1 < total:
            return httpx.Response(202)
        return httpx.Response(201, json={"id": "bigid", "name": "big.bin", "size": total})

    monkeypatch.setattr(
        f"{_MS_MOD}._session_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    _ms_upload(_ms_client(_session_send()), config_dir, file=str(big))
    assert seen and threading.get_ident() not in seen
