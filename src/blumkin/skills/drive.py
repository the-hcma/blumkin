"""`drive` shared layer: provider-neutral helpers, errors, and human formatters.

The Google Drive and Microsoft OneDrive backends both return the same stable
item shape so ``--json`` consumers do not branch on the provider::

    {id, name, mime_type, kind, size, modified, web_url, parent_id, provider}

``kind`` collapses the provider's type vocabulary to
``file | folder | doc | sheet | slides``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from blumkin.output import sanitize_terminal

# Every `drive.*` skill id. The dispatch layer gates the whole set on the
# Microsoft `docs_scopes` toggle (Files.ReadWrite) - the same grant `docs create`
# already needs; Google carries `drive` in its standard scope set.
DRIVE_SKILLS: frozenset[str] = frozenset(
    {
        "drive.download",
        "drive.export",
        "drive.get",
        "drive.list",
        "drive.read",
    }
)

DriveKind = Literal["file", "folder", "doc", "sheet", "slides"]

# `drive export` target extension -> the MIME type to ask the provider for. Google
# uses `files.export`; Microsoft only honours `pdf` (Graph `?format=`).
EXPORT_MIME: dict[str, str] = {
    "csv": "text/csv",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "html": "text/html",
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
# Google-native types have no byte stream - `drive download` refuses them and
# points at `drive export`.
GOOGLE_NATIVE_MIMES: frozenset[str] = frozenset(
    {
        "application/vnd.google-apps.document",
        "application/vnd.google-apps.presentation",
        "application/vnd.google-apps.spreadsheet",
        "application/vnd.google-apps.drawing",
    }
)

_ORDERS = ("modified", "name")


class DriveFolderNotFoundError(LookupError):
    """A ``--folder`` path did not resolve to a folder."""


class DriveFolderAmbiguousError(ValueError):
    """A ``--folder`` path segment matched more than one folder (Google has no real paths)."""


class DriveItemNotFoundError(LookupError):
    """No drive item has the given id."""


class DriveSelectorError(ValueError):
    """Mutually exclusive selectors (``--folder`` / ``--folder-id``) were both given, or a
    required one was missing."""


class DriveExportError(ValueError):
    """An unusable ``--to`` extension, or an export the provider cannot do."""


class DriveDownloadError(ValueError):
    """``drive download`` was pointed at something with no byte stream (a Google-native doc)."""


class DriveReadUnsupportedError(ValueError):
    """``drive read`` on a provider with no document-content API (Microsoft / Graph)."""


def export_mime(to: str) -> tuple[str, str]:
    """``--to`` path -> (extension, MIME). Raises :class:`DriveExportError` for a bad extension."""
    ext = Path(to).suffix.lstrip(".").lower()
    if ext not in EXPORT_MIME:
        raise DriveExportError(
            f"--to must end in one of {', '.join(sorted(EXPORT_MIME))} (got {to!r})"
        )
    return ext, EXPORT_MIME[ext]


def resolve_export_dest(to: str) -> Path:
    """``--to`` is always a file path for `drive export` - never a directory."""
    path = Path(to)
    if path.is_dir():
        raise DriveExportError(f"--to must be a file path, not a directory: {to}")
    if path.exists():
        raise DriveExportError(f"--to already exists, refusing to overwrite: {to}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def format_drive_download_human(payload: dict[str, Any]) -> list[str]:
    return [f"Saved {payload.get('bytes')} bytes to {payload.get('saved_path')}"]


def format_drive_export_human(payload: dict[str, Any]) -> list[str]:
    return [
        f"Exported {payload.get('name')!r} as {payload.get('format')} "
        f"to {payload.get('saved_path')} ({payload.get('bytes')} bytes)"
    ]


def format_drive_get_human(payload: dict[str, Any]) -> list[str]:
    item = payload.get("item") or {}
    lines = [f"{item.get('name')!r}  ({item.get('kind')}, {item.get('provider')})"]
    lines.append(f"  id={item.get('id')}")
    if item.get("mime_type"):
        lines.append(f"  mime: {item['mime_type']}")
    if item.get("size") is not None:
        lines.append(f"  size: {item['size']} bytes")
    if item.get("modified"):
        lines.append(f"  modified: {item['modified']}")
    if item.get("parent_id"):
        lines.append(f"  parent: {item['parent_id']}")
    if item.get("web_url"):
        lines.append(f"  {item['web_url']}")
    exports = item.get("export_formats") or []
    if exports:
        lines.append(f"  export: {', '.join(exports)}")
    return lines


def format_drive_read_human(payload: dict[str, Any]) -> list[str]:
    # A shared Doc is remote content from the same trust boundary as a mail body
    # or Teams message - strip control chars before it reaches the terminal.
    lines: list[str] = sanitize_terminal(str(payload.get("markdown") or "")).splitlines()
    return lines or ["(empty document)"]


def format_drive_list_human(payload: dict[str, Any]) -> list[str]:
    items = payload.get("items") or []
    if not items:
        return ["(no items)"]
    lines = [f"{len(items)} item(s):"]
    for item in items:
        size = "" if item.get("size") is None else f"  {item['size']}b"
        lines.append(f"  [{item.get('kind'):>6}] {item.get('name')}{size}  id={item.get('id')}")
    return lines


def flatten_google_doc(document: dict[str, Any]) -> str:
    """Flatten a Docs API ``documents.get`` response to the `docs create` Markdown subset.

    Headings, ``**bold**`` / ``*italic*`` / ``` `code` ``` / ``[text](url)`` inline,
    bullet + numbered lists (indented by nesting level), and plain paragraphs.
    Table cells are joined with `` | `` per row. Anything else degrades to its text.
    """
    lists = document.get("lists") or {}
    out: list[str] = []
    for element in (document.get("body") or {}).get("content") or []:
        paragraph = element.get("paragraph")
        if paragraph is not None:
            out.append(_paragraph_markdown(paragraph, lists))
            continue
        table = element.get("table")
        if table is not None:
            for row in table.get("tableRows") or []:
                cells = [
                    " ".join(
                        _paragraph_markdown(pe["paragraph"], lists)
                        for pe in (cell.get("content") or [])
                        if pe.get("paragraph") is not None
                    ).strip()
                    for cell in row.get("tableCells") or []
                ]
                out.append(" | ".join(cells))
    text = "\n".join(line for line in out)
    return text.strip() + "\n" if text.strip() else ""


def normalize_order(order: str | None) -> str:
    value = (order or "modified").strip().lower()
    if value not in _ORDERS:
        raise DriveSelectorError(f"--order must be one of {', '.join(_ORDERS)} (got {order!r})")
    return value


_HEADING_LEVEL = {f"HEADING_{n}": n for n in range(1, 7)}


def _inline_markdown(text_run: dict[str, Any]) -> str:
    # Keep every "\n" here. Only the paragraph's *final* run ends with the
    # paragraph terminator; a "\n" that ends a non-final run (a Shift+Enter break
    # at a style boundary) or sits inside a run is a hard line break and must
    # survive. `_paragraph_markdown` trims the one trailing terminator.
    content = text_run.get("content") or ""
    if not content:
        return ""
    stripped = content.strip()
    if not stripped:
        return content
    style = text_run.get("textStyle") or {}
    lead = content[: len(content) - len(content.lstrip())]
    trail = content[len(content.rstrip()) :]
    body = stripped
    styled = False
    if style.get("weightedFontFamily", {}).get("fontFamily", "") in _CODE_FONTS:
        body, styled = f"`{body}`", True
    else:
        if style.get("bold"):
            body, styled = f"**{body}**", True
        if style.get("italic"):
            body, styled = f"*{body}*", True
    link = (style.get("link") or {}).get("url")
    if link:
        body, styled = f"[{body}]({link})", True
    if styled and "\n" in body:
        body = body.replace("\n", " ")  # inline markup cannot span a line break
    return f"{lead}{body}{trail}"


def _paragraph_markdown(paragraph: dict[str, Any], lists: dict[str, Any]) -> str:
    runs = "".join(
        _inline_markdown(el["textRun"])
        for el in paragraph.get("elements") or []
        if el.get("textRun") is not None
    ).strip()
    named = (paragraph.get("paragraphStyle") or {}).get("namedStyleType", "")
    one_line = " ".join(runs.split("\n"))  # headings / list items are single-line
    if named in _HEADING_LEVEL and one_line:
        return f"{'#' * _HEADING_LEVEL[named]} {one_line}"
    bullet = paragraph.get("bullet")
    if bullet is not None:
        level = int(bullet.get("nestingLevel") or 0)
        glyph = (
            (lists.get(bullet.get("listId"), {}).get("listProperties") or {})
            .get("nestingLevels", [{}] * (level + 1))[level]
            .get("glyphType", "")
        )
        marker = "1." if any(g in glyph for g in ("DECIMAL", "ALPHA", "ROMAN")) else "-"
        return f"{'  ' * level}{marker} {one_line}"
    return runs


_CODE_FONTS = frozenset({"Roboto Mono", "Consolas", "Courier New", "Source Code Pro"})


def split_path(path: str) -> list[str]:
    """Split a drive path into its non-empty segments (``/A//B/`` -> ``['A', 'B']``)."""
    return [segment.strip() for segment in path.split("/") if segment.strip()]


def validate_folder_selector(folder: str | None, folder_id: str | None) -> None:
    """`drive list` takes at most one of ``--folder`` / ``--folder-id``."""
    if folder is not None and folder_id is not None:
        raise DriveSelectorError("pass at most one of --folder or --folder-id, not both")
