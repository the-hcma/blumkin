"""Hermetic tests for the Microsoft `docs create` backend and its scope gate."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from docx import Document
from kiota_abstractions.method import Method
from msgraph.generated.models.o_data_errors.o_data_error import ODataError

from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.providers.kind import ProviderKind
from blumkin.providers.microsoft import MicrosoftWorkspaceProvider
from blumkin.skills.dispatch import run_skill
from blumkin.skills.errors import ScopeAddonDisabledError

_MOD = "blumkin.providers.microsoft_docs"
_UPLOADED = SimpleNamespace(
    id="01ABC", name="Brief.docx", web_url="https://onedrive.example/Brief.docx"
)


def _cfg(
    *, provider: ProviderKind = ProviderKind.MICROSOFT, docs_scopes: bool = True
) -> BlumkinConfig:
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
        provider=provider,
        tags=(),
        tenant_id="brk.tech",
        wo1162425_scopes=False,
    )


def _client(*, folder_exists: bool = False) -> MagicMock:
    client = MagicMock()
    calls: list[Any] = []

    async def send_async(request_info: Any, _factory: Any, _error_map: Any) -> Any:
        calls.append(request_info)
        if ":/content" in request_info.url_template:
            return _UPLOADED
        if request_info.http_method == Method.GET:  # folder existence probe
            return SimpleNamespace(id="existing-folder") if folder_exists else None
        return SimpleNamespace(id="new-folder")  # POST children (folder create)

    client.request_adapter.send_async = AsyncMock(side_effect=send_async)
    client.calls = calls
    return client


def _run(client: MagicMock, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> dict[str, Any]:
    monkeypatch.setattr(f"{_MOD}.create_graph_client", lambda _cfg: client)
    return asyncio.run(MicrosoftWorkspaceProvider(_cfg()).docs_create(**kwargs))


def _style_names(document: Any) -> list[str]:
    return [p.style.name if p.style else "" for p in document.paragraphs]


def test_docs_create_creates_a_missing_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(folder_exists=False)
    payload = _run(client, monkeypatch, title="Weekly: status?", body="hi", folder="/Reports/Q3/")
    assert payload["document"]["folder"] == "Reports/Q3"
    gets = [c for c in client.calls if c.http_method == Method.GET]
    posts = [c for c in client.calls if c.http_method == Method.POST]
    assert [g.path_parameters["path"] for g in gets] == ["Reports", "Reports/Q3"]
    assert len(posts) == 2  # both segments created
    (upload,) = [c for c in client.calls if ":/content" in c.url_template]
    assert upload.path_parameters["path"] == "Reports/Q3/Weekly status.docx"


def test_docs_create_on_google_bypasses_the_docs_scopes_gate() -> None:
    provider = MagicMock()
    provider.docs_create = AsyncMock(return_value={"document": {"id": "d1"}})
    payload = asyncio.run(
        run_skill(
            "docs.create",
            {"title": "T", "body": "x"},
            config=_cfg(provider=ProviderKind.GOOGLE, docs_scopes=False),
            provider=provider,
        )
    )
    assert payload == {"document": {"id": "d1"}}
    provider.docs_create.assert_awaited_once()


def test_docs_create_on_microsoft_needs_the_docs_scopes_toggle() -> None:
    with pytest.raises(ScopeAddonDisabledError, match="Files.ReadWrite"):
        asyncio.run(
            run_skill(
                "docs.create",
                {"title": "T", "body": "x"},
                config=_cfg(docs_scopes=False),
                provider=MicrosoftWorkspaceProvider(_cfg(docs_scopes=False)),
            )
        )


def test_docs_create_propagates_a_non_404_folder_probe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    throttled = ODataError()
    throttled.response_status_code = 429

    async def send_async(request_info: Any, _f: Any, _e: Any) -> Any:
        if request_info.http_method == Method.GET:
            raise throttled
        raise AssertionError("must not write after a throttled probe")

    client.request_adapter.send_async = AsyncMock(side_effect=send_async)
    monkeypatch.setattr(f"{_MOD}.create_graph_client", lambda _cfg: client)
    with pytest.raises(ODataError):
        asyncio.run(
            MicrosoftWorkspaceProvider(_cfg()).docs_create(title="T", body="hi", folder="Reports")
        )


def test_docs_create_reads_a_body_file_as_plain_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    src = tmp_path / "note.txt"
    src.write_text("# not a heading\n\nsecond para", encoding="utf-8")
    client = _client()
    _run(client, monkeypatch, title="T", body_file=str(src), body_format="text")
    (upload,) = [c for c in client.calls if ":/content" in c.url_template]
    texts = [p.text for p in Document(io.BytesIO(bytes(upload.content))).paragraphs]
    assert texts == ["# not a heading", "second para"]


def test_docs_create_renders_every_supported_block(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        "# Title\n\n"
        "para text\n\n"
        "- b0\n  - b1\n\n"
        "1. n0\n\n"
        "---\n\n"
        "```\ncode line\n```\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n"
    )
    client = _client()
    _run(client, monkeypatch, title="T", body=body)
    (upload,) = [c for c in client.calls if ":/content" in c.url_template]
    document = Document(io.BytesIO(bytes(upload.content)))
    styles = _style_names(document)
    assert "Heading 1" in styles
    assert "List Bullet" in styles and "List Bullet 2" in styles
    assert "1. n0" in [p.text for p in document.paragraphs]  # numbered marker is literal text


def test_docs_create_restarts_numbering_for_each_ordered_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    _run(client, monkeypatch, title="T", body="1. a\n2. b\n\n# Next\n\n1. c\n2. d")
    (upload,) = [c for c in client.calls if ":/content" in c.url_template]
    texts = [p.text for p in Document(io.BytesIO(bytes(upload.content))).paragraphs]
    # The second list restarts at 1, it does not continue as 3./4.
    assert texts == ["1. a", "2. b", "Next", "1. c", "2. d"]


def test_docs_create_renders_markdown_links_as_real_hyperlinks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    _run(client, monkeypatch, title="T", body="See [the guide](https://x.test/guide) now.")
    (upload,) = [c for c in client.calls if ":/content" in c.url_template]
    (paragraph,) = Document(io.BytesIO(bytes(upload.content))).paragraphs
    assert paragraph.text == "See the guide now."
    # A real w:hyperlink with an external relationship, not just underlined text.
    assert [(h.address, h.text) for h in paragraph.hyperlinks] == [
        ("https://x.test/guide", "the guide")
    ]


def test_docs_create_reuses_an_existing_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(folder_exists=True)
    _run(client, monkeypatch, title="T", body="hi", folder="Reports")
    assert [c for c in client.calls if c.http_method == Method.POST] == []


def test_docs_create_strips_url_breaking_chars_from_title_and_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(folder_exists=True)
    payload = _run(client, monkeypatch, title="Sprint #12: plan?", body="hi", folder="Team #2/Q4")
    assert payload["document"]["folder"] == "Team 2/Q4"
    (upload,) = [c for c in client.calls if ":/content" in c.url_template]
    assert upload.path_parameters["path"] == "Team 2/Q4/Sprint 12 plan.docx"
    assert "#" not in upload.path_parameters["path"]


def test_docs_create_uploads_a_real_docx(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    payload = _run(
        client, monkeypatch, title="Brief", body="# Brief\n\n- one\n- two\n\n`code` and **bold**"
    )
    assert payload["document"] == {
        "id": "01ABC",
        "name": "Brief.docx",
        "web_url": "https://onedrive.example/Brief.docx",
        "provider": "microsoft",
        "format": "docx",
        "folder": None,
    }
    (upload,) = [c for c in client.calls if ":/content" in c.url_template]
    assert upload.path_parameters["path"] == "Brief.docx"
    assert upload.http_method == Method.PUT
    # Never silently overwrite an existing file at the same title.
    assert "conflictBehavior=rename" in upload.url_template
    document = Document(io.BytesIO(bytes(upload.content)))
    assert document.paragraphs[0].text == "Brief"
    assert _style_names(document)[0] == "Heading 1"
