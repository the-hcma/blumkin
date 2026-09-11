"""Hermetic tests for `drive download` / `drive export` / `drive read` (#208 phase 2)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blumkin.config import BlumkinConfig, MailSignatureConfig, PreferencesConfig
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.providers.microsoft import MicrosoftWorkspaceProvider
from blumkin.skills.drive import (
    DriveDownloadError,
    DriveExportError,
    DriveReadUnsupportedError,
    export_mime,
    flatten_google_doc,
    resolve_export_dest,
)

_GOOGLE_MOD = "blumkin.providers.google.drive"
_MS_MOD = "blumkin.providers.microsoft_drive"


# --------------------------------------------------------------------------- shared helpers


def test_export_mime_maps_and_rejects() -> None:
    assert export_mime("out.PDF") == ("pdf", "application/pdf")
    assert export_mime("a/b/c.csv")[1] == "text/csv"
    with pytest.raises(DriveExportError):
        export_mime("notes.rtf")


def test_resolve_export_dest_refuses_existing(tmp_path: Path) -> None:
    target = tmp_path / "x.pdf"
    target.write_bytes(b"old")
    with pytest.raises(DriveExportError):
        resolve_export_dest(str(target))
    with pytest.raises(DriveExportError):
        resolve_export_dest(str(tmp_path))  # a directory


def test_flatten_google_doc_markdown_subset() -> None:
    document = {
        "title": "Vocab",
        "lists": {
            "L1": {"listProperties": {"nestingLevels": [{"glyphType": "GLYPH_TYPE_UNSPECIFIED"}]}},
            "L2": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}},
        },
        "body": {
            "content": [
                {
                    "paragraph": {
                        "paragraphStyle": {"namedStyleType": "HEADING_1"},
                        "elements": [{"textRun": {"content": "Week 3\n", "textStyle": {}}}],
                    }
                },
                {
                    "paragraph": {
                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                        "elements": [
                            {"textRun": {"content": "A ", "textStyle": {}}},
                            {"textRun": {"content": "bold", "textStyle": {"bold": True}}},
                            {"textRun": {"content": " word and a ", "textStyle": {}}},
                            {
                                "textRun": {
                                    "content": "link",
                                    "textStyle": {"link": {"url": "https://x.test"}},
                                }
                            },
                            {"textRun": {"content": "\n", "textStyle": {}}},
                        ],
                    }
                },
                {
                    "paragraph": {
                        "bullet": {"listId": "L1", "nestingLevel": 0},
                        "elements": [{"textRun": {"content": "first\n", "textStyle": {}}}],
                    }
                },
                {
                    "paragraph": {
                        "bullet": {"listId": "L2", "nestingLevel": 0},
                        "elements": [{"textRun": {"content": "step one\n", "textStyle": {}}}],
                    }
                },
            ]
        },
    }
    md = flatten_google_doc(document)
    assert md == ("# Week 3\nA **bold** word and a [link](https://x.test)\n- first\n1. step one\n")


def test_flatten_google_doc_empty() -> None:
    assert flatten_google_doc({"body": {"content": []}}) == ""


def _para(*runs: dict[str, Any], style: str = "NORMAL_TEXT") -> dict[str, Any]:
    return {
        "paragraph": {
            "paragraphStyle": {"namedStyleType": style},
            "elements": [{"textRun": r} for r in runs],
        }
    }


def test_flatten_google_doc_inline_styles() -> None:
    doc = {
        "body": {
            "content": [
                _para(
                    {"content": "an ", "textStyle": {}},
                    {"content": "em", "textStyle": {"italic": True}},
                    {"content": " and ", "textStyle": {}},
                    {
                        "content": "mono",
                        "textStyle": {"weightedFontFamily": {"fontFamily": "Roboto Mono"}},
                    },
                    {"content": " and ", "textStyle": {}},
                    {"content": "both", "textStyle": {"bold": True, "italic": True}},
                    {"content": "\n", "textStyle": {}},
                ),
            ]
        }
    }
    assert flatten_google_doc(doc) == "an *em* and `mono` and ***both***\n"


def test_flatten_google_doc_preserves_hard_line_breaks() -> None:
    # A Shift+Enter break is an interior "\n" in one contiguous run.
    doc = {"body": {"content": [_para({"content": "line one\nline two\n", "textStyle": {}})]}}
    assert flatten_google_doc(doc) == "line one\nline two\n"


def test_flatten_google_doc_hard_break_at_a_style_boundary() -> None:
    # The break ends the *bold* run; the next run is plain -> two runs, and the
    # trailing "\n" of the non-final run must not be stripped.
    doc = {
        "body": {
            "content": [
                _para(
                    {"content": "bold\n", "textStyle": {"bold": True}},
                    {"content": "plain\n", "textStyle": {}},
                )
            ]
        }
    }
    assert flatten_google_doc(doc) == "**bold**\nplain\n"


def test_flatten_google_doc_heading_flattens_a_hard_break() -> None:
    doc = {
        "body": {
            "content": [_para({"content": "Title\nsub\n", "textStyle": {}}, style="HEADING_1")]
        }
    }
    assert flatten_google_doc(doc) == "# Title sub\n"


def test_format_drive_read_human_strips_control_chars() -> None:
    from blumkin.skills.drive import format_drive_read_human

    lines = format_drive_read_human({"markdown": "safe\x1b[2Krewritten\nsecond\x07line"})
    assert lines == ["safe[2Krewritten", "secondline"]


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
        mail_signature=MailSignatureConfig(),
        preferences=PreferencesConfig(),
        profile="default",
        provider=ProviderKind.GOOGLE,
        tags=(),
        tenant_id="",
        wo1162425_scopes=False,
    )


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


# --------------------------------------------------------------------------- Google backend


def _google_service(
    *, mime: str = "application/pdf", export_links: dict[str, str] | None = None
) -> MagicMock:
    service = MagicMock()
    files = service.files.return_value
    meta: dict[str, Any] = {"id": "d1", "name": "report.pdf", "mimeType": mime}
    if export_links is not None:
        meta["exportLinks"] = export_links
    files.get.return_value.execute.return_value = meta
    files.get_media.return_value.execute.return_value = b"RAW-BYTES"
    files.export_media.return_value.execute.return_value = b"PDF-BYTES"
    service.documents.return_value.get.return_value.execute.return_value = {
        "title": "Doc",
        "body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "hi\n"}}]}}]},
    }
    return service


_DOC_EXPORT_LINKS = {
    "application/pdf": "u",
    "text/plain": "u",
    "text/csv": "u",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "u",
}


def _google_patched(service: MagicMock):
    return patch.multiple(
        _GOOGLE_MOD,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
        execute=MagicMock(side_effect=lambda request, **_kw: request.execute()),
    )


def test_google_drive_download_writes_bytes(tmp_path: Path) -> None:
    with _google_patched(_google_service(mime="application/pdf")):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_download(
                item_id="d1", out=str(tmp_path / "got.pdf")
            )
        )
    assert (tmp_path / "got.pdf").read_bytes() == b"RAW-BYTES"
    assert payload["bytes"] == 9 and payload["provider"] == "google"


@pytest.mark.parametrize(
    "mime",
    [
        "application/vnd.google-apps.document",
        "application/vnd.google-apps.folder",
        "application/vnd.google-apps.script",
    ],
)
def test_google_drive_download_refuses_native_types(tmp_path: Path, mime: str) -> None:
    svc = _google_service(mime=mime)
    with _google_patched(svc), pytest.raises(DriveDownloadError):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_download(
                item_id="d1", out=str(tmp_path / "x")
            )
        )
    svc.files.return_value.get_media.assert_not_called()


def test_google_drive_export_picks_mime_from_extension(tmp_path: Path) -> None:
    svc = _google_service(
        mime="application/vnd.google-apps.document", export_links=_DOC_EXPORT_LINKS
    )
    with _google_patched(svc):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_export(
                item_id="d1", to=str(tmp_path / "out.csv")
            )
        )
    assert svc.files.return_value.export_media.call_args.kwargs["mimeType"] == "text/csv"
    assert payload["format"] == "csv"
    assert (tmp_path / "out.csv").read_bytes() == b"PDF-BYTES"


def test_google_drive_export_rejects_unavailable_format(tmp_path: Path) -> None:
    svc = _google_service(
        mime="application/vnd.google-apps.document", export_links=_DOC_EXPORT_LINKS
    )
    with _google_patched(svc), pytest.raises(DriveExportError):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_export(
                item_id="d1",
                to=str(tmp_path / "out.xlsx"),  # not in a Doc's export links
            )
        )
    svc.files.return_value.export_media.assert_not_called()


def test_google_drive_export_rejects_non_native_file(tmp_path: Path) -> None:
    svc = _google_service(mime="application/pdf")  # no exportLinks
    with _google_patched(svc), pytest.raises(DriveExportError):
        asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_export(
                item_id="d1", to=str(tmp_path / "out.pdf")
            )
        )


def test_google_drive_read_returns_markdown(tmp_path: Path) -> None:
    svc = _google_service(mime="application/vnd.google-apps.document")
    with _google_patched(svc):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_read(item_id="d1")
        )
    assert payload["markdown"] == "hi\n"
    assert payload["item"] == {"id": "d1", "name": "Doc", "provider": "google"}


def test_google_drive_read_refuses_a_non_doc(tmp_path: Path) -> None:
    # A Sheet / Slides / folder id is listable but is not a Google Doc.
    svc = _google_service(mime="application/vnd.google-apps.spreadsheet")
    with _google_patched(svc), pytest.raises(DriveReadUnsupportedError):
        asyncio.run(GoogleWorkspaceProvider(_google_cfg(tmp_path)).drive_read(item_id="d1"))
    svc.documents.return_value.get.assert_not_called()


# --------------------------------------------------------------------------- Microsoft backend


def _ms_client(*, item: Any, content: bytes = b"BYTES") -> MagicMock:
    client = MagicMock()
    client.content_urls = []

    async def send_async(_ri: Any, _f: Any, _e: Any) -> Any:
        return item

    async def send_primitive_async(ri: Any, _t: Any, _e: Any) -> Any:
        client.content_urls.append(ri.url_template)
        return content

    client.request_adapter.send_async = AsyncMock(side_effect=send_async)
    client.request_adapter.send_primitive_async = AsyncMock(side_effect=send_primitive_async)
    return client


def _ms_run(client: MagicMock, monkeypatch: pytest.MonkeyPatch, method: str, **kw: Any):
    monkeypatch.setattr(f"{_MS_MOD}.create_graph_client", lambda _c: client)
    return asyncio.run(getattr(MicrosoftWorkspaceProvider(_ms_cfg()), method)(**kw))


def test_ms_drive_download_writes_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = SimpleNamespace(name="Report.docx", folder=None)
    client = _ms_client(item=item, content=b"DOCX")
    payload = _ms_run(client, monkeypatch, "drive_download", item_id="d1", out=str(tmp_path))
    assert (tmp_path / "Report.docx").read_bytes() == b"DOCX"
    assert payload["provider"] == "microsoft"
    # A dropped endpoint would silently write raw bytes to the wrong place.
    assert client.content_urls == ["https://graph.microsoft.com/v1.0/me/drive/items/{id}/content"]


def test_ms_drive_download_refuses_a_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = SimpleNamespace(name="Reports", folder=SimpleNamespace(child_count=3))
    client = _ms_client(item=folder)
    with pytest.raises(DriveDownloadError):
        _ms_run(client, monkeypatch, "drive_download", item_id="f1", out=str(tmp_path))
    client.request_adapter.send_primitive_async.assert_not_called()


def test_ms_drive_export_refuses_a_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = SimpleNamespace(name="Reports", folder=SimpleNamespace(child_count=3))
    with pytest.raises(DriveExportError):
        _ms_run(
            _ms_client(item=folder),
            monkeypatch,
            "drive_export",
            item_id="f1",
            to=str(tmp_path / "r.pdf"),
        )


def test_ms_drive_export_pdf_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = SimpleNamespace(name="Report.docx", folder=None)
    client = _ms_client(item=item, content=b"%PDF")
    payload = _ms_run(client, monkeypatch, "drive_export", item_id="d1", to=str(tmp_path / "r.pdf"))
    assert payload["format"] == "pdf"
    assert client.content_urls == [
        "https://graph.microsoft.com/v1.0/me/drive/items/{id}/content?format=pdf"
    ]
    with pytest.raises(DriveExportError):
        _ms_run(
            _ms_client(item=item),
            monkeypatch,
            "drive_export",
            item_id="d1",
            to=str(tmp_path / "r.txt"),
        )


def test_ms_drive_export_refuses_a_non_office_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plain .txt (kind "file") cannot go through Graph's Office->PDF converter.
    plain = SimpleNamespace(name="notes.txt", folder=None)
    client = _ms_client(item=plain)
    with pytest.raises(DriveExportError):
        _ms_run(client, monkeypatch, "drive_export", item_id="d1", to=str(tmp_path / "notes.pdf"))
    assert client.content_urls == []  # never hit Graph /content


def test_ms_drive_read_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(DriveReadUnsupportedError):
        _ms_run(_ms_client(item=None), monkeypatch, "drive_read", item_id="d1")
