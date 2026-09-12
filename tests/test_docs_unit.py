"""Hermetic tests for the shared `docs` Markdown subset and the Google backend."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from blumkin.config import BlumkinConfig, MailSignatureConfig, PreferencesConfig
from blumkin.created_docs import is_blumkin_created_doc, record_created_doc
from blumkin.providers.google_auth import DOCS_SCOPES
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.skills.docs import (
    DocBodyError,
    DocSpan,
    format_docs_create_human,
    format_docs_update_human,
    parse_body,
    parse_markdown,
    read_body,
    render_email_html,
    strip_docx_suffix,
    table_to_text,
)
from blumkin.skills.drive import DriveItemNotFoundError

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
    (block,) = parse_markdown("a paragraph with ~~strikethrough~~ text")
    assert block.kind == "paragraph"
    assert block.spans == (DocSpan("a paragraph with ~~strikethrough~~ text"),)


def test_block_quote_is_its_own_kind_not_a_paragraph() -> None:
    (block,) = parse_markdown("> a quoted line")
    assert block.kind == "quote"
    assert block.spans == (DocSpan("a quoted line"),)


def test_block_quote_joins_consecutive_lines_like_a_paragraph() -> None:
    (block,) = parse_markdown("> line one\n> line two")
    assert block.kind == "quote"
    assert block.spans == (DocSpan("line one line two"),)


def test_block_quote_is_flushed_by_a_following_paragraph() -> None:
    blocks = parse_markdown("> quoted\nnot quoted")
    assert [(b.kind, b.spans[0].text) for b in blocks] == [
        ("quote", "quoted"),
        ("paragraph", "not quoted"),
    ]


def test_html_entities_are_decoded() -> None:
    (block,) = parse_markdown("A &amp; B&nbsp;C &lt;tag&gt;")
    assert block.spans == (DocSpan("A & B\xa0C <tag>"),)


def test_plain_text_format_splits_on_blank_lines_only() -> None:
    blocks = parse_body("# not a heading\nsame para\n\nsecond para", body_format="text")
    assert [b.kind for b in blocks] == ["paragraph", "paragraph"]
    assert blocks[0].spans == (DocSpan("# not a heading same para"),)


def test_render_email_html_maps_blocks_to_semantic_tags() -> None:
    html = render_email_html(
        parse_markdown(
            "## Heading\n\nA para.\n\n- x\n- y\n\n1. one\n2. two\n\n```\ncode()\n```\n\n---"
        )
    )
    assert html == (
        "<h2>Heading</h2><p>A para.</p>"
        "<ul><li>x</li><li>y</li></ul>"
        "<ol><li>one</li><li>two</li></ol>"
        "<pre><code>code()</code></pre><hr>"
    )


def test_render_email_html_wraps_a_quote_in_blockquote() -> None:
    html = render_email_html(parse_markdown("> a quote"))
    assert html == "<blockquote>a quote</blockquote>"


def test_render_email_html_renders_tables_with_a_header_row() -> None:
    table = render_email_html(parse_markdown("| A | B |\n|---|---|\n| 1 | 2 |"))
    assert table == (
        "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
        "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
    )


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


def test_strip_docx_suffix() -> None:
    assert strip_docx_suffix("Q3.docx") == "Q3"
    assert strip_docx_suffix("Q3.DOCX") == "Q3"
    assert strip_docx_suffix("Q3") == "Q3"
    assert strip_docx_suffix("notes.docx.docx") == "notes.docx"
    assert strip_docx_suffix(".docx") == ".docx"  # nothing left - keep as-is


# --------------------------------------------------------------------------- google backend


def _cfg(config_dir: Path) -> BlumkinConfig:
    oauth = config_dir / "client.json"
    oauth.write_text("{}")
    cfg = BlumkinConfig(
        client_id="id.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="America/New_York",
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
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _service() -> MagicMock:
    service = MagicMock()
    documents = service.documents.return_value
    documents.create.return_value.execute.return_value = {"documentId": "doc-123"}
    documents.batchUpdate.return_value.execute.return_value = {}
    documents.get.return_value.execute.return_value = {
        "title": "Old title",
        "body": {"content": [{"endIndex": 1}, {"endIndex": 42}]},
    }
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": []}
    files.create.return_value.execute.return_value = {"id": "folder-1"}
    files.get.return_value.execute.return_value = {"id": "doc-123"}
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


def test_docs_create_renders_a_block_quote_as_an_indented_paragraph(tmp_path: Path) -> None:
    requests = _batch_for(tmp_path, "> a quote")
    assert requests[0]["insertText"]["text"] == "a quote\n"
    indent = next(
        r["updateParagraphStyle"]
        for r in requests
        if "updateParagraphStyle" in r
        and "indentStart" in r["updateParagraphStyle"]["paragraphStyle"]
    )
    assert indent["paragraphStyle"]["indentStart"] == {"magnitude": 36, "unit": "PT"}
    # No bullet - unlike `- `/`1. `, a quote must not draw a list marker.
    assert not any("createParagraphBullets" in r for r in requests)


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
    para = next(
        r["updateParagraphStyle"]
        for r in requests
        if "updateParagraphStyle" in r and "shading" in r["updateParagraphStyle"]["paragraphStyle"]
    )
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
    para = next(
        r["updateParagraphStyle"]
        for r in requests
        if "updateParagraphStyle" in r and "shading" in r["updateParagraphStyle"]["paragraphStyle"]
    )
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


def test_docs_update_replaces_the_body_then_renames(tmp_path: Path) -> None:
    service = _service()
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(
                document_id="doc-123", title="New title", body="# New\n\nbody"
            )
        )
    requests = service.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
    assert requests[0] == {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 41}}}
    assert any("insertText" in r for r in requests)
    assert service.files.return_value.update.call_args.kwargs == {
        "fileId": "doc-123",
        "body": {"name": "New title"},
    }
    assert payload["document"] == {
        "id": "doc-123",
        "name": "New title",
        "web_url": "https://docs.google.com/document/d/doc-123/edit",
        "provider": "google",
        "format": "gdoc",
        "folder": None,
    }


def test_docs_update_computes_the_delete_range_from_tabs_content(tmp_path: Path) -> None:
    # `docs.documents().get(..., includeTabsContent=True)` populates `tabs` and
    # leaves the legacy top-level `body` unpopulated for any document with tabs
    # (every document since Workspace's 2024 tabs rollout). Reading only the
    # top-level `body` here always computed end_index<=2 and silently skipped
    # the delete - `docs update` appended instead of replacing. Regression for
    # https://github.com/the-hcma/blumkin/issues/263.
    service = _service()
    service.documents.return_value.get.return_value.execute.return_value = {
        "title": "Old title",
        "body": {},
        "tabs": [
            {
                "tabId": "t1",
                "documentTab": {"body": {"content": [{"endIndex": 1}, {"endIndex": 42}]}},
            }
        ],
    }
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(document_id="doc-123", body="hi")
        )
    requests = service.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
    assert requests[0] == {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 41}}}


def test_docs_update_resets_a_plain_paragraph_to_normal_text(tmp_path: Path) -> None:
    # The delete range can never remove the body's terminal paragraph mark
    # (Docs forbids it), so a doc whose old body ended on a heading can leave
    # new plain text inheriting that heading's style. Regression for
    # https://github.com/the-hcma/blumkin/issues/263.
    service = _service()
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(
                document_id="doc-123", body="plain text"
            )
        )
    requests = service.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
    # Select by the paragraph's own range (not just "the first
    # updateParagraphStyle") - the trailing-mark reset from
    # test_docs_update_resets_the_surviving_trailing_paragraph_too also sets
    # NORMAL_TEXT and now runs first, so picking the first match would pass
    # even if `_style_requests` never reset the block itself.
    reset = next(
        r["updateParagraphStyle"]
        for r in requests
        if "updateParagraphStyle" in r
        and r["updateParagraphStyle"]["range"] == {"startIndex": 1, "endIndex": 12}
    )
    assert reset["paragraphStyle"] == {"namedStyleType": "NORMAL_TEXT"}


def test_docs_update_refuses_tabs_with_no_readable_first_tab_body(tmp_path: Path) -> None:
    # `includeTabsContent=True` should always populate `documentTab` for a
    # single-tab response; a `tabs` entry with no readable body content is an
    # unexpected shape blumkin cannot safely compute a delete range for -
    # silently falling back to the (documented-empty) legacy `body` field
    # would recompute end_index as 2 and skip the delete again, reintroducing
    # the append-instead-of-replace bug. Regression for
    # https://github.com/the-hcma/blumkin/issues/263 (review finding).
    service = _service()
    service.documents.return_value.get.return_value.execute.return_value = {
        "title": "Old title",
        "body": {},
        "tabs": [{"tabId": "t1"}],  # no `documentTab` at all
    }
    with _patched(service), pytest.raises(DocBodyError, match="readable"):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(document_id="doc-123", body="hi")
        )
    service.documents.return_value.batchUpdate.assert_not_called()


def test_docs_update_resets_the_surviving_trailing_paragraph_too(tmp_path: Path) -> None:
    # The per-block NORMAL_TEXT resets only cover the *new* blocks' own
    # ranges. The delete can never remove the body's terminal paragraph mark,
    # so that mark survives and `insertText` pushes it past every new block -
    # past every one of those resets - still carrying the *old* body's
    # trailing style (e.g. a phantom entry left in the Docs heading outline).
    # Regression for https://github.com/the-hcma/blumkin/issues/263 (review
    # finding).
    service = _service()
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(
                document_id="doc-123", body="# New\n\nbody"
            )
        )
    requests = service.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
    # "New\nbody\n" is 9 UTF-16 units; the surviving mark lands right after it.
    trailing = next(
        r["updateParagraphStyle"]
        for r in requests
        if "updateParagraphStyle" in r
        and r["updateParagraphStyle"]["range"] == {"startIndex": 10, "endIndex": 11}
    )
    assert trailing["paragraphStyle"] == {"namedStyleType": "NORMAL_TEXT"}


def test_docs_update_resets_the_trailing_paragraph_before_bullets_strip_tabs(
    tmp_path: Path,
) -> None:
    # createParagraphBullets strips each nested list item's leading tab and
    # shifts every later index down; the trailing reset is computed from the
    # tab-inclusive text length, so it must run before any such request, not
    # after. Regression for https://github.com/the-hcma/blumkin/issues/263
    # (review finding).
    service = _service()
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(
                document_id="doc-123", body="- a\n  - b"
            )
        )
    requests = service.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
    # "a\n\tb\n" (the nested item's leading tab included) is 5 UTF-16 units.
    trailing_index = next(
        i
        for i, r in enumerate(requests)
        if "updateParagraphStyle" in r
        and r["updateParagraphStyle"]["range"] == {"startIndex": 6, "endIndex": 7}
    )
    bullet_indices = [i for i, r in enumerate(requests) if "createParagraphBullets" in r]
    assert bullet_indices  # sanity: the nested list did produce bullet requests
    assert all(trailing_index < i for i in bullet_indices)


def test_docs_update_body_only_keeps_the_existing_name(tmp_path: Path) -> None:
    service = _service()
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(document_id="doc-123", body="hi")
        )
    service.files.return_value.update.assert_not_called()
    assert payload["document"]["name"] == "Old title"


def test_docs_update_title_only_skips_the_batch_update(tmp_path: Path) -> None:
    service = _service()
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(document_id="doc-123", title="X")
        )
    service.documents.return_value.batchUpdate.assert_not_called()
    service.files.return_value.update.assert_called_once()


def test_docs_update_on_an_empty_doc_skips_the_delete(tmp_path: Path) -> None:
    service = _service()
    service.documents.return_value.get.return_value.execute.return_value = {
        "title": "Empty",
        "body": {"content": [{"endIndex": 2}]},
    }
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(document_id="doc-123", body="hi")
        )
    requests = service.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
    assert not any("deleteContentRange" in r for r in requests)


def test_docs_update_refuses_a_multi_tab_document(tmp_path: Path) -> None:
    service = _service()
    service.documents.return_value.get.return_value.execute.return_value = {
        "title": "Multi",
        "tabs": [{"tabId": "t1"}, {"tabId": "t2"}],
        "body": {"content": [{"endIndex": 1}, {"endIndex": 42}]},
    }
    with _patched(service), pytest.raises(DocBodyError, match="multiple tabs"):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(document_id="doc-123", body="hi")
        )
    service.documents.return_value.batchUpdate.assert_not_called()


def test_docs_update_allows_a_multi_tab_document_for_a_rename_only(tmp_path: Path) -> None:
    service = _service()
    service.documents.return_value.get.return_value.execute.return_value = {
        "title": "Multi",
        "tabs": [{"tabId": "t1"}, {"tabId": "t2"}],
        "body": {"content": [{"endIndex": 42}]},
    }
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(document_id="doc-123", title="New")
        )
    service.files.return_value.update.assert_called_once()


def test_docs_update_needs_at_least_one_target(tmp_path: Path) -> None:
    with _patched(_service()), pytest.raises(DocBodyError, match="at least one of"):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(document_id="doc-123"))


def test_docs_update_rejects_a_blank_title(tmp_path: Path) -> None:
    with _patched(_service()), pytest.raises(DocBodyError, match="blank"):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(document_id="doc-123", title="  ")
        )


def test_docs_update_rejects_both_body_sources(tmp_path: Path) -> None:
    with _patched(_service()), pytest.raises(DocBodyError, match="not both"):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(
                document_id="doc-123", body="a", body_file="/tmp/x"
            )
        )


def test_docs_update_refuses_a_doc_this_install_did_not_create(tmp_path: Path) -> None:
    """The drive.file probe 404s for a foreign doc; body must never be touched."""
    service = _service()
    resp = type("Resp", (), {"status": 404, "reason": "Not Found"})()
    service.files.return_value.get.return_value.execute.side_effect = HttpError(resp, b"nope")
    with _patched(service), pytest.raises(DriveItemNotFoundError):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(document_id="0xFOREIGN", body="x")
        )
    service.documents.return_value.batchUpdate.assert_not_called()


def test_docs_update_accepts_a_doc_the_drive_file_grant_can_still_see(tmp_path: Path) -> None:
    """Cross-machine case on Google: not in the local record, but drive.file finds it."""
    service = _service()
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(document_id="doc-123", body="hi")
        )
    service.files.return_value.get.assert_called_once()
    service.documents.return_value.batchUpdate.assert_called_once()
    # Recorded, so the probe is skipped next time.
    assert is_blumkin_created_doc(_cfg(tmp_path), "doc-123")


def test_docs_create_records_the_new_doc_id(tmp_path: Path) -> None:
    with _patched(_service()):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).docs_create(title="Brief", body="hi"))
    cfg = _cfg(tmp_path)
    assert is_blumkin_created_doc(cfg, "doc-123")


def test_docs_update_id_deleted_since_creation_is_not_found(tmp_path: Path) -> None:
    service = _service()
    cfg = _cfg(tmp_path)
    record_created_doc(cfg, "doc-123")
    resp = type("Resp", (), {"status": 404, "reason": "Not Found"})()
    service.documents.return_value.get.return_value.execute.side_effect = HttpError(resp, b"nope")
    with _patched(service), pytest.raises(DriveItemNotFoundError):
        asyncio.run(GoogleWorkspaceProvider(cfg).docs_update(document_id="doc-123", body="hi"))


def test_docs_update_does_not_record_a_probe_visible_id_that_fails_to_update(
    tmp_path: Path,
) -> None:
    """A folder id passes the drive.file probe but 404s at documents.get - never record it."""
    service = _service()
    cfg = _cfg(tmp_path)
    resp = type("Resp", (), {"status": 404, "reason": "Not Found"})()
    service.documents.return_value.get.return_value.execute.side_effect = HttpError(resp, b"nope")
    with _patched(service), pytest.raises(DriveItemNotFoundError):
        asyncio.run(GoogleWorkspaceProvider(cfg).docs_update(document_id="folder-id", body="hi"))
    assert not is_blumkin_created_doc(cfg, "folder-id")


def test_docs_update_strips_a_docx_suffix_from_a_google_rename(tmp_path: Path) -> None:
    service = _service()
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(
                document_id="doc-123", title="Q3.docx"
            )
        )
    assert service.files.return_value.update.call_args.kwargs["body"] == {"name": "Q3"}


def test_docs_update_reads_a_body_file(tmp_path: Path) -> None:
    src = tmp_path / "body.md"
    src.write_text("# From a file\n\nfile body text", encoding="utf-8")
    service = _service()
    with _patched(service):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(
                document_id="doc-123", body_file=str(src)
            )
        )
    requests = service.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
    inserted = next(r["insertText"]["text"] for r in requests if "insertText" in r)
    assert "From a file" in inserted and "file body text" in inserted


def test_docs_update_gates_on_the_narrow_docs_scopes_subset(tmp_path: Path) -> None:
    get_creds = MagicMock(return_value=MagicMock())
    with patch.multiple(
        _DOCS_MOD,
        get_credentials=get_creds,
        build_api_service=MagicMock(return_value=_service()),
        execute=MagicMock(side_effect=lambda request, **_kw: request.execute()),
    ):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).docs_update(document_id="doc-123", title="X")
        )
    assert get_creds.call_args.kwargs["required_scopes"] is DOCS_SCOPES


def test_format_docs_update_human_reads_cleanly() -> None:
    lines = format_docs_update_human(
        {
            "document": {
                "name": "Brief",
                "format": "gdoc",
                "id": "d1",
                "web_url": "https://docs.google.com/document/d/d1/edit",
            }
        }
    )
    assert lines[0] == "Document updated: 'Brief' (gdoc)"
    # Off a TTY the web URL stays plain (label + parenthesised address); see #233.
    assert lines[-1] == "  Brief (https://docs.google.com/document/d/d1/edit)"


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
    assert lines[-1] == "  Brief (https://docs.google.com/document/d/d1/edit)"
