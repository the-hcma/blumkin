"""Google Docs `docs create`: render the shared block model into a real Google Doc.

``documents.create`` makes an empty doc; one ``documents.batchUpdate`` then
inserts the whole body as text and styles it by absolute index (headings, bold /
italic / code / links, lists, fenced code, rules). ``--folder`` is a path: an
existing folder (needs the broad ``drive`` scope, ``DOCS_FOLDER_SCOPES``) or one
created on the spot; a root-level doc needs only ``{documents, drive.file}``.
"""

from __future__ import annotations

from typing import Any

from googleapiclient.errors import HttpError

from blumkin.config import BlumkinConfig, load_config
from blumkin.created_docs import is_blumkin_created_doc, record_created_doc
from blumkin.providers.google.drive import resolve_folder_path
from blumkin.providers.google_auth import DOCS_FOLDER_SCOPES, DOCS_SCOPES, get_credentials
from blumkin.providers.google_http import build_api_service, execute
from blumkin.skills.docs import (
    DocBlock,
    DocBodyError,
    DocSpan,
    parse_body,
    read_body,
    read_update_body,
    require_docs_update_target,
    strip_docx_suffix,
    table_to_text,
)
from blumkin.skills.drive import DriveItemNotFoundError

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

    record_created_doc(cfg, document_id)
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


async def docs_update(
    *,
    document_id: str,
    title: str | None = None,
    body: str | None = None,
    body_file: str | None = None,
    body_format: str = "markdown",
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Re-render an existing Google Doc in place: rename it, replace its body, or both.

    The id, URL, and sharing are untouched - a content update is a
    ``deleteContentRange`` over the whole body followed by the same insert / style
    pipeline ``docs create`` uses, so document-level named styles survive.

    Refuses an id this blumkin install did not create: the ``documents`` scope is
    user-wide, so without the check ``--id`` alone could point at (and clobber)
    any doc the account can edit. The check is the local ``docs create`` record,
    or - Google only - a ``drive.file`` lookup that still 404s for foreign docs.
    """
    doc_id = document_id.strip()
    if not doc_id:
        raise ValueError("--id is required")
    new_title = require_docs_update_target(title=title, body=body, body_file=body_file)
    raw_body = read_update_body(body, body_file)
    blocks = parse_body(raw_body, body_format=body_format) if raw_body is not None else None

    cfg = config or load_config()
    # Rename and the ownership probe both use the narrow drive.file grant, which
    # only ever sees docs this tool created - no need for the broad `drive` scope.
    creds = get_credentials(cfg, allow_interactive=False, required_scopes=DOCS_SCOPES)
    docs = build_api_service("docs", "v1", creds=creds, config=cfg)
    drive = build_api_service("drive", "v3", creds=creds, config=cfg)

    unrecorded = not is_blumkin_created_doc(cfg, doc_id)
    if unrecorded:
        try:
            # drive.file 404s here for a doc blumkin did not create or open.
            execute(drive.files().get(fileId=doc_id, fields="id"))
        except HttpError as exc:
            raise _translate_http(exc, document_id=doc_id) from exc

    try:
        document = execute(docs.documents().get(documentId=doc_id, includeTabsContent=True))
    except HttpError as exc:
        raise _translate_http(exc, document_id=doc_id) from exc

    if blocks is not None and len(document.get("tabs") or []) > 1:
        # The legacy top-level `body` this pipeline edits is only the first tab, so
        # a whole-body replace would silently leave the other tabs untouched.
        raise DocBodyError(
            f"document {doc_id!r} has multiple tabs; `docs update --body` replaces only the "
            "first one - edit a multi-tab document in the browser"
        )

    if blocks is not None:
        requests: list[dict[str, Any]] = []
        end_index = _body_end_index(document)
        if end_index > 2:
            requests.append(
                {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index - 1}}}
            )
        block_requests = _batch_requests(blocks)
        if end_index > 2:
            # The delete above can never remove the body's terminal paragraph
            # mark (Docs forbids it), so that one surviving mark gets pushed
            # past every newly inserted block by `insertText` - past every
            # per-block style reset too - still carrying whatever named style
            # the *old* body's last paragraph had (e.g. a phantom entry left
            # in the Docs heading outline). Reset it too.
            #
            # `new_text_end` counts each nested list item's leading tab, which
            # `createParagraphBullets` (always last in `block_requests` - see
            # `_batch_requests`) strips, shifting every later index down. This
            # request must run *before* those, while the tab-inclusive length
            # is still the real one - inserting right after `insertText`
            # (index 0) guarantees that, since nothing between them changes
            # the document's length.
            new_text_end = 1 + _utf16_len("".join(_block_text(block) for block in blocks))
            block_requests.insert(
                1 if block_requests else 0,
                {
                    "updateParagraphStyle": {
                        "range": {"startIndex": new_text_end, "endIndex": new_text_end + 1},
                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                        "fields": "namedStyleType",
                    }
                },
            )
        requests.extend(block_requests)
        if requests:
            execute(docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests}))

    if new_title is not None:
        new_title = strip_docx_suffix(new_title)
        try:
            execute(drive.files().update(fileId=doc_id, body={"name": new_title}))
        except HttpError as exc:
            raise _translate_http(exc, document_id=doc_id) from exc

    if unrecorded:
        # Record only now that every write has actually landed - the drive.file
        # probe alone also passes for folders / deleted docs this app made, and a
        # bad id in created_docs.json would then skip the probe forever.
        record_created_doc(cfg, doc_id)

    return {
        "document": {
            "id": doc_id,
            "name": new_title or document.get("title"),
            "web_url": f"https://docs.google.com/document/d/{doc_id}/edit",
            "provider": "google",
            "format": "gdoc",
            "folder": None,
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


def _body_end_index(document: dict[str, Any]) -> int:
    """The document's final content index, for sizing the pre-insert delete range."""
    content = _document_body_content(document)
    if not content:
        return 2
    return int(content[-1].get("endIndex", 2))


def _document_body_content(document: dict[str, Any]) -> list[dict[str, Any]]:
    """The single editable body's content list, tabs-content-aware.

    ``docs_update`` fetches with ``includeTabsContent=True`` so the multi-tab
    guard above can see every tab; that flag populates ``tabs`` and leaves the
    legacy top-level ``body`` empty for any document with tabs - which is every
    document since Workspace's 2024 tabs rollout. Reading only ``body`` here
    made the computed end index always fall back to 2, which skipped the
    pre-insert delete unconditionally (`docs update` silently appended instead
    of replacing). Prefer the first tab's body; fall back to the legacy
    top-level field only when the response has no ``tabs`` at all.

    A response that *does* carry ``tabs`` but has no readable first-tab
    ``content`` raises rather than falling back to the (documented-empty)
    legacy field - silently treating that as "content: []" would recompute
    ``end_index`` as 2 and skip the delete again, reintroducing exactly the
    append-instead-of-replace bug this exists to fix, with no error surfaced.
    """
    tabs = document.get("tabs") or []
    if not tabs:
        return ((document.get("body") or {}).get("content")) or []
    content = ((tabs[0].get("documentTab") or {}).get("body") or {}).get("content")
    if content is None:
        raise DocBodyError(
            "documents.get returned `tabs` but no readable first-tab body content - "
            "cannot safely compute the delete range for `docs update`"
        )
    return content


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
    # `docs update`'s delete range can never remove the body's terminal paragraph
    # mark (Docs forbids it - see `_body_end_index`), so newly inserted text can
    # land inside a paragraph that survived from the old body and inherit
    # whatever named style it had (e.g. a stray `HEADING_2`). Reset every
    # block's own range to `NORMAL_TEXT` up front so leftover styling never
    # leaks into new content; the heading branch below overrides it right back
    # for an actual heading.
    requests: list[dict[str, Any]] = [
        {
            "updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "fields": "namedStyleType",
            }
        }
    ]
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
    elif block.kind == "quote":
        # No native Docs "blockquote" - an indent is the closest non-bulleted
        # equivalent (issue #264: a plain leading tab/spaces was silently
        # stripped, and `- ` was the only thing that actually indented, but
        # that draws a bullet).
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "paragraphStyle": {"indentStart": {"magnitude": 36, "unit": "PT"}},
                    "fields": "indentStart",
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


def _translate_http(exc: HttpError, *, document_id: str) -> Exception:
    """A 404 from Docs / Drive means the id is wrong or the doc is not one we can see."""
    resp = getattr(exc, "resp", None)
    status = getattr(exc, "status_code", None) or getattr(resp, "status", None)
    try:
        code = int(status) if status is not None else None
    except TypeError, ValueError:
        code = None
    if code == 404:
        return DriveItemNotFoundError(
            f"no document with id {document_id!r} (blumkin can only update docs it created)"
        )
    return exc


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units - how the Google Docs API counts document indices."""
    return len(text.encode("utf-16-le")) // 2
