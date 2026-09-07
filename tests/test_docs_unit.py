"""Hermetic tests for the shared `docs` Markdown subset and the Google backend."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.providers.google_auth import DOCS_SCOPES
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.skills.docs import (
    DocBodyError,
    DocSpan,
    format_docs_create_human,
    parse_body,
    parse_markdown,
    read_body,
    table_to_text,
)

_DOCS_MOD = "blumkin.providers.google.docs"


# --------------------------------------------------------------------------- parser


def test_headings_and_paragraphs() -> None:
    blocks = parse_markdown("# Title\n\nA first paragraph\nwrapped over two lines.\n\n## Sub")
    assert [(b.kind, b.level) for b in blocks] == [
        ("heading", 1),
        ("paragraph", 0),
        ("heading", 2),
    ]
    assert blocks[1].spans == (DocSpan("A first paragraph wrapped over two lines."),)


def test_inline_formatting() -> None:
    (block,) = parse_markdown("**bold** and *italic* and `code` and [a link](https://x.test)")
    assert block.spans == (
        DocSpan("bold", bold=True),
        DocSpan(" and "),
        DocSpan("italic", italic=True),
        DocSpan(" and "),
        DocSpan("code", code=True),
        DocSpan(" and "),
        DocSpan("a link", link="https://x.test"),
    )


def test_nested_emphasis_and_intra_word_asterisks_are_literal() -> None:
    (block,) = parse_markdown("***both*** but 3*4 = 12")
    assert block.spans[0] == DocSpan("both", bold=True, italic=True)
    assert "".join(s.text for s in block.spans) == "both but 3*4 = 12"


def test_lists_bullet_and_numbered_with_one_nesting_level() -> None:
    blocks = parse_markdown("- top\n  - nested\n1. first\n2. second")
    assert [(b.kind, b.level) for b in blocks] == [
        ("bullet", 0),
        ("bullet", 1),
        ("number", 0),
        ("number", 0),
    ]


def test_fenced_code_and_horizontal_rule() -> None:
    blocks = parse_markdown("intro\n\n```\nx = 1\ny = 2\n```\n\n---\n\nend")
    assert blocks[1].kind == "code"
    assert blocks[1].code_text == "x = 1\ny = 2"
    assert blocks[2].kind == "rule"


def test_pipe_table() -> None:
    (block,) = parse_markdown("| Name | Role |\n|------|------|\n| Ada | Lead |\n| Bo | Dev |")
    assert block.kind == "table"
    assert len(block.rows) == 3
    assert block.rows[0][0] == (DocSpan("Name"),)
    assert block.rows[2] == ((DocSpan("Bo"),), (DocSpan("Dev"),))


def test_ragged_pipe_table_renders_without_raising() -> None:
    (block,) = parse_markdown("| A | B |\n|---|---|\n| 1 | 2 | 3 |\n| 4 |")
    text = table_to_text(block.rows)
    assert text.splitlines()[0].split(" | ")[:2] == ["A", "B"]
    assert "3" in text and "4" in text  # long row kept, short row padded


def test_out_of_subset_markdown_degrades_to_text() -> None:
    (block,) = parse_markdown("> a blockquote with an ![image](x.png)")
    assert block.kind == "paragraph"
    assert block.spans[0].text.startswith("> a blockquote")


def test_plain_text_format_splits_on_blank_lines_only() -> None:
    blocks = parse_body("# not a heading\nsame para\n\nsecond para", body_format="text")
    assert [b.kind for b in blocks] == ["paragraph", "paragraph"]
    assert blocks[0].spans == (DocSpan("# not a heading same para"),)


def test_parse_body_rejects_an_unknown_format() -> None:
    with pytest.raises(DocBodyError):
        parse_body("x", body_format="rst")


# --------------------------------------------------------------------------- read_body


def test_read_body_requires_exactly_one_source() -> None:
    with pytest.raises(DocBodyError):
        read_body(None, None)
    with pytest.raises(DocBodyError):
        read_body("x", "y")


def test_read_body_reads_a_utf8_file(tmp_path: Path) -> None:
    path = tmp_path / "brief.md"
    path.write_text("# Héllo", encoding="utf-8")
    assert read_body(None, str(path)) == "# Héllo"


def test_read_body_rejects_an_oversize_body() -> None:
    with pytest.raises(DocBodyError, match="larger than"):
        read_body("x" * 1_000_001, None)


# --------------------------------------------------------------------------- google backend


def _cfg(config_dir: Path) -> BlumkinConfig:
    oauth = config_dir / "client.json"
    oauth.write_text("{}")
    return BlumkinConfig(
        client_id="id.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="America/New_York",
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


def _service() -> MagicMock:
    service = MagicMock()
    documents = service.documents.return_value
    documents.create.return_value.execute.return_value = {"documentId": "doc-123"}
    documents.batchUpdate.return_value.execute.return_value = {}
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": []}
    files.create.return_value.execute.return_value = {"id": "folder-1"}
    files.update.return_value.execute.return_value = {"id": "doc-123"}
    return service


def _patched(service: MagicMock):
    return patch.multiple(
        _DOCS_MOD,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
        execute=MagicMock(side_effect=lambda request, **_kw: request.execute()),
    )


def test_docs_create_returns_the_stable_json_shape(tmp_path: Path) -> None:
    with _patched(_service()):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_create(
                title="Brief", body="# Brief\n\nBody text."
            )
        )
    assert payload["document"] == {
        "id": "doc-123",
        "name": "Brief",
        "web_url": "https://docs.google.com/document/d/doc-123/edit",
        "provider": "google",
        "format": "gdoc",
        "folder": None,
    }


def test_docs_create_gates_on_the_narrow_docs_scopes_subset(tmp_path: Path) -> None:
    get_creds = MagicMock(return_value=MagicMock())
    with patch.multiple(
        _DOCS_MOD,
        get_credentials=get_creds,
        build_api_service=MagicMock(return_value=_service()),
        execute=MagicMock(side_effect=lambda request, **_kw: request.execute()),
    ):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).docs_create(title="T", body="hi"))
    assert get_creds.call_args.kwargs["required_scopes"] is DOCS_SCOPES
    assert get_creds.call_args.kwargs["allow_interactive"] is False


def test_docs_create_emits_insert_text_then_styling(tmp_path: Path) -> None:
    service = _service()
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_create(
                title="T", body="# Head\n\n**bold** word"
            )
        )
    requests = service.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
    assert requests[0]["insertText"] == {
        "location": {"index": 1},
        "text": "Head\nbold word\n",
    }
    kinds = {next(iter(req)) for req in requests}
    assert "updateParagraphStyle" in kinds  # heading
    assert "updateTextStyle" in kinds  # bold run


def _batch_for(tmp_path: Path, body: str) -> list[dict]:
    service = _service()
    with _patched(service):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).docs_create(title="T", body=body))
    return service.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]


def test_docs_create_renders_a_bullet_list_with_nesting(tmp_path: Path) -> None:
    requests = _batch_for(tmp_path, "- top\n  - nested")
    assert requests[0]["insertText"]["text"] == "top\n\tnested\n"
    bullets = [r["createParagraphBullets"] for r in requests if "createParagraphBullets" in r]
    assert len(bullets) == 2
    assert all(b["bulletPreset"] == "BULLET_DISC_CIRCLE_SQUARE" for b in bullets)
    # The nested item's range covers its leading tab so Docs infers the level.
    assert {b["range"]["startIndex"] for b in bullets} == {1, 5}


def test_docs_create_renders_a_numbered_list(tmp_path: Path) -> None:
    requests = _batch_for(tmp_path, "1. one\n2. two")
    assert requests[0]["insertText"]["text"] == "one\ntwo\n"
    presets = {
        r["createParagraphBullets"]["bulletPreset"]
        for r in requests
        if "createParagraphBullets" in r
    }
    assert presets == {"NUMBERED_DECIMAL_ALPHA_ROMAN"}


def test_docs_create_renders_a_fenced_code_block(tmp_path: Path) -> None:
    requests = _batch_for(tmp_path, "```\nx = 1\ny = 2\n```")
    assert requests[0]["insertText"]["text"] == "x = 1\ny = 2\n"
    para = next(r["updateParagraphStyle"] for r in requests if "updateParagraphStyle" in r)
    # backgroundColor is an OptionalColor - the {"color": {...}} wrapper is required.
    assert para["paragraphStyle"]["shading"] == {
        "backgroundColor": {"color": {"rgbColor": {"red": 0.95, "green": 0.95, "blue": 0.95}}}
    }
    assert para["range"] == {"startIndex": 1, "endIndex": 13}
    font = next(r["updateTextStyle"] for r in requests if "updateTextStyle" in r)
    assert font["textStyle"]["weightedFontFamily"]["fontFamily"] == "Roboto Mono"


def test_docs_create_styles_inline_runs_inside_a_nested_list_item(tmp_path: Path) -> None:
    requests = _batch_for(tmp_path, "- top\n  - **nested**")
    assert requests[0]["insertText"]["text"] == "top\n\tnested\n"
    bold = next(r["updateTextStyle"] for r in requests if "updateTextStyle" in r)
    # second item starts at index 5; its leading tab is excluded, so "nested" is [6, 12).
    assert bold["range"] == {"startIndex": 6, "endIndex": 12}
    assert bold["textStyle"] == {"bold": True}


def test_docs_create_renders_a_horizontal_rule(tmp_path: Path) -> None:
    requests = _batch_for(tmp_path, "a\n\n---\n\nb")
    assert requests[0]["insertText"]["text"] == "a\n\nb\n"
    rule = next(
        r["updateParagraphStyle"]
        for r in requests
        if "updateParagraphStyle" in r
        and "borderBottom" in r["updateParagraphStyle"]["paragraphStyle"]
    )
    assert rule["range"] == {"startIndex": 3, "endIndex": 4}  # the lone "\n"


def test_docs_create_renders_a_pipe_table_as_a_monospace_grid(tmp_path: Path) -> None:
    requests = _batch_for(tmp_path, "| A | BB |\n|---|----|\n| 1 | 2 |")
    text = requests[0]["insertText"]["text"]
    assert text == "A | BB\n--+---\n1 | 2 \n"
    para = next(r["updateParagraphStyle"] for r in requests if "updateParagraphStyle" in r)
    assert "shading" in para["paragraphStyle"]


def test_docs_create_styles_by_utf16_offset_past_an_astral_char(tmp_path: Path) -> None:
    service = _service()
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_create(title="T", body="🎉 **bold**")
        )
    requests = service.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
    text_style = next(r["updateTextStyle"] for r in requests if "updateTextStyle" in r)
    # insertText is "🎉 bold\n"; "bold" starts after U+1F389 (2 UTF-16 units) + " " =
    # index 1 + 3. A code-point count would wrongly say index 3.
    assert text_style["range"] == {"startIndex": 4, "endIndex": 8}


def test_docs_create_creates_and_files_under_a_folder(tmp_path: Path) -> None:
    service = _service()
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_create(
                title="T", body="hi", folder="Briefs"
            )
        )
    assert payload["document"]["folder"] == "Briefs"
    service.files.return_value.create.assert_called_once()
    update_kwargs = service.files.return_value.update.call_args.kwargs
    assert update_kwargs["fileId"] == "doc-123"
    assert update_kwargs["addParents"] == "folder-1"
    # Move, not add a second parent - otherwise the doc shows in root and the folder.
    assert update_kwargs["removeParents"] == "root"


def test_docs_create_survives_a_ragged_pipe_table(tmp_path: Path) -> None:
    service = _service()
    body = "| Name | Role |\n|---|---|\n| Ada | Lead | Extra |\n| Bo |"
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_create(title="T", body=body)
        )
    assert payload["document"]["id"] == "doc-123"
    service.documents.return_value.batchUpdate.assert_called_once()


def test_docs_create_reuses_an_existing_folder_this_tool_made(tmp_path: Path) -> None:
    service = _service()
    service.files.return_value.list.return_value.execute.return_value = {"files": [{"id": "f-9"}]}
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_create(
                title="T", body="hi", folder="O'Brien\\Docs"
            )
        )
    service.files.return_value.create.assert_not_called()
    assert service.files.return_value.update.call_args.kwargs["addParents"] == "f-9"
    # The folder name is escaped into the Drive `q` string, not interpolated raw.
    query = service.files.return_value.list.call_args.kwargs["q"]
    assert "name = 'O\\'Brien\\\\Docs'" in query


def test_docs_create_propagates_a_folder_lookup_failure(tmp_path: Path) -> None:
    service = _service()
    resp = type("Resp", (), {"status": 503, "reason": "Service Unavailable"})()
    service.files.return_value.list.return_value.execute.side_effect = HttpError(resp, b"busy")
    with _patched(service), pytest.raises(HttpError):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_create(
                title="T", body="hi", folder="Briefs"
            )
        )
    # A failed lookup must not fall through to creating a (duplicate) folder.
    service.files.return_value.create.assert_not_called()


def test_docs_create_rejects_a_blank_title(tmp_path: Path) -> None:
    with _patched(_service()), pytest.raises(ValueError, match="title"):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).docs_create(title="  ", body="x"))


def test_format_docs_create_human_reads_cleanly() -> None:
    lines = format_docs_create_human(
        {
            "document": {
                "name": "Brief",
                "format": "gdoc",
                "folder": "Briefs",
                "id": "d1",
                "web_url": "https://docs.google.com/document/d/d1/edit",
            }
        }
    )
    assert lines[0] == "Document created: 'Brief' (gdoc)"
    assert "  folder: Briefs" in lines
    assert lines[-1].endswith("/d1/edit")
