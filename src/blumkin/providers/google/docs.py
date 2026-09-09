"""Google Docs `docs create`: render the shared block model into a real Google Doc.

``documents.create`` makes an empty doc; one ``documents.batchUpdate`` then
inserts the whole body as text and styles it by absolute index (headings, bold /
italic / code / links, lists, fenced code, rules). ``--folder`` is a path: an
existing folder (needs the broad ``drive`` scope, ``DOCS_FOLDER_SCOPES``) or one
created on the spot; a root-level doc needs only ``{documents, drive.file}``.
"""

from __future__ import annotations

from typing import Any

from blumkin.config import BlumkinConfig, load_config
from blumkin.providers.google.drive import resolve_folder_path
from blumkin.providers.google_auth import DOCS_FOLDER_SCOPES, DOCS_SCOPES, get_credentials
from blumkin.providers.google_http import build_api_service, execute
from blumkin.skills.docs import DocBlock, DocSpan, parse_body, read_body, table_to_text

_CODE_FONT = "Roboto Mono"
# An OptionalColor: the {"color": {...}} wrapper is required by the Docs schema
# (same shape as borderBottom.color below).
_CODE_SHADE = {"color": {"rgbColor": {"red": 0.95, "green": 0.95, "blue": 0.95}}}
_HEADING_STYLES = {
    1: "HEADING_1",
    2: "HEADING_2",
    3: "HEADING_3",
    4: "HEADING_4",
    5: "HEADING_5",
    6: "HEADING_6",
}
_LIST_PRESETS = {"bullet": "BULLET_DISC_CIRCLE_SQUARE", "number": "NUMBERED_DECIMAL_ALPHA_ROMAN"}


async def docs_create(
    *,
    title: str,
    body: str | None = None,
    body_file: str | None = None,
    body_format: str = "markdown",
    folder: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Create a Google Doc titled ``title`` from authored ``body`` content."""
    if not title.strip():
        raise ValueError("--title is required")
    cfg = config or load_config()
    blocks = parse_body(read_body(body, body_file), body_format=body_format)

    folder_name = folder.strip() if folder else None
    # Only `--folder <path>` needs the broad `drive` scope; a root-level doc runs
    # fine on the narrow {documents, drive.file} grant.
    required = DOCS_FOLDER_SCOPES if folder_name else DOCS_SCOPES
    creds = get_credentials(cfg, allow_interactive=False, required_scopes=required)

    # Resolve --folder BEFORE minting the doc, so an ambiguous / missing segment
    # is a side-effect-free usage_error instead of orphaning an authored doc in
    # the Drive root (which every MCP retry would then multiply).
    folder_id: str | None = None
    if folder_name:
        drive = build_api_service("drive", "v3", creds=creds, config=cfg)
        folder_id, _ = resolve_folder_path(drive, folder_name, create=True)

    docs = build_api_service("docs", "v1", creds=creds, config=cfg)
    document = execute(docs.documents().create(body={"title": title.strip()}))
    document_id = str(document["documentId"])

    requests = _batch_requests(blocks)
    if requests:
        execute(docs.documents().batchUpdate(documentId=document_id, body={"requests": requests}))

    if folder_id is not None:
        _reparent(drive, document_id=document_id, folder_id=folder_id)

    return {
        "document": {
            "id": document_id,
            "name": title.strip(),
            "web_url": f"https://docs.google.com/document/d/{document_id}/edit",
            "provider": "google",
            "format": "gdoc",
            "folder": folder_name,
        }
    }


def _batch_requests(blocks: list[DocBlock]) -> list[dict[str, Any]]:
    """Assemble one insertText plus absolute-index styling for every block."""
    chunks = [_block_text(block) for block in blocks]
    full_text = "".join(chunks)
    if not full_text:
        return []

    requests: list[dict[str, Any]] = [{"insertText": {"location": {"index": 1}, "text": full_text}}]
    bullets: list[dict[str, Any]] = []
    cursor = 1
    for block, chunk in zip(blocks, chunks, strict=True):
        start, end = cursor, cursor + _utf16_len(chunk)
        cursor = end
        requests.extend(_style_requests(block, start, end))
        if block.kind in _LIST_PRESETS:
            bullets.append(
                {
                    "createParagraphBullets": {
                        "range": {"startIndex": start, "endIndex": end},
                        "bulletPreset": _LIST_PRESETS[block.kind],
                    }
                }
            )
    # Bullets last, back-to-front: createParagraphBullets can shift later indices.
    requests.extend(reversed(bullets))
    return requests


def _block_text(block: DocBlock) -> str:
    if block.kind == "code":
        return f"{block.code_text}\n"
    if block.kind == "rule":
        return "\n"
    if block.kind == "table":
        return f"{table_to_text(block.rows)}\n"
    if block.kind in _LIST_PRESETS:
        # createParagraphBullets reads leading tabs to set the nesting level, then
        # strips them - the documented way to nest without a separate request.
        return f"{'\t' * block.level}{_spans_text(block.spans)}\n"
    return f"{_spans_text(block.spans)}\n"


def _reparent(drive: Any, *, document_id: str, folder_id: str) -> None:
    # documents.create drops the doc in the Drive root; removeParents="root" moves
    # it rather than adding a second parent - otherwise the doc shows in both.
    execute(
        drive.files().update(
            fileId=document_id,
            addParents=folder_id,
            removeParents="root",
            fields="id, parents",
        )
    )


def _span_style(span: DocSpan, start: int, end: int) -> dict[str, Any] | None:
    text_style: dict[str, Any] = {}
    fields: list[str] = []
    if span.bold:
        text_style["bold"] = True
        fields.append("bold")
    if span.italic:
        text_style["italic"] = True
        fields.append("italic")
    if span.code:
        text_style["weightedFontFamily"] = {"fontFamily": _CODE_FONT}
        fields.append("weightedFontFamily")
    if span.link:
        text_style["link"] = {"url": span.link}
        fields.append("link")
    if not fields:
        return None
    return {
        "updateTextStyle": {
            "range": {"startIndex": start, "endIndex": end},
            "textStyle": text_style,
            "fields": ",".join(fields),
        }
    }


def _style_requests(block: DocBlock, start: int, end: int) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    if block.kind == "heading":
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "paragraphStyle": {
                        "namedStyleType": _HEADING_STYLES.get(block.level, "HEADING_6")
                    },
                    "fields": "namedStyleType",
                }
            }
        )
    elif block.kind == "rule":
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "paragraphStyle": {
                        "borderBottom": {
                            "width": {"magnitude": 1, "unit": "PT"},
                            "padding": {"magnitude": 1, "unit": "PT"},
                            "dashStyle": "SOLID",
                            "color": {
                                "color": {"rgbColor": {"red": 0.6, "green": 0.6, "blue": 0.6}}
                            },
                        }
                    },
                    "fields": "borderBottom",
                }
            }
        )
        return requests
    elif block.kind in ("code", "table"):
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "paragraphStyle": {"shading": {"backgroundColor": _CODE_SHADE}},
                    "fields": "shading",
                }
            }
        )
        requests.append(
            {
                "updateTextStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "textStyle": {"weightedFontFamily": {"fontFamily": _CODE_FONT}},
                    "fields": "weightedFontFamily",
                }
            }
        )
        return requests

    # List items carry `block.level` leading tabs in the inserted text (see
    # `_block_text`); the spans start after them.
    span_start = start + (block.level if block.kind in _LIST_PRESETS else 0)
    for span in block.spans:
        span_end = span_start + _utf16_len(span.text)
        styled = _span_style(span, span_start, span_end)
        if styled is not None:
            requests.append(styled)
        span_start = span_end
    return requests


def _spans_text(spans: tuple[DocSpan, ...]) -> str:
    return "".join(span.text for span in spans)


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units - how the Google Docs API counts document indices."""
    return len(text.encode("utf-16-le")) // 2
