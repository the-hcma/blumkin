"""`drive` shared layer: provider-neutral helpers, errors, and human formatters.

The Google Drive and Microsoft OneDrive backends both return the same stable
item shape so ``--json`` consumers do not branch on the provider::

    {id, name, mime_type, kind, size, modified, web_url, parent_id, provider}

``kind`` collapses the provider's type vocabulary to
``file | folder | doc | sheet | slides``.
"""

from __future__ import annotations

import mimetypes
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal

from blumkin.output import hyperlink, sanitize_terminal

# Every `drive.*` skill id. The dispatch layer gates the whole set on the
# Microsoft `docs_scopes` toggle (Files.ReadWrite) - the same grant `docs create`
# already needs; Google carries `drive` in its standard scope set.
DRIVE_SKILLS: frozenset[str] = frozenset(
    {
        "drive.download",
        "drive.export",
        "drive.get",
        "drive.list",
        "drive.mkdir",
        "drive.move",
        "drive.read",
        "drive.rename",
        "drive.upload",
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

#: The largest file `drive upload` will send (a sanity limit, not a service limit).
UPLOAD_MAX_BYTES = 2 * 1024**3

# Places that hold credentials, relative to the home directory (a file or a whole folder), and
# file names that conventionally hold secrets. A guard rail for `drive upload`, not a guarantee.
_SECRET_HOME_FILES = (".netrc",)
_SECRET_HOME_FOLDERS = (".aws", ".config/gcloud", ".gnupg", ".ssh")
_MIME_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*")
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


class DriveUploadError(ValueError):
    """The file cannot be uploaded, or the name it would get is taken."""


class DriveReadUnsupportedError(ValueError):
    """``drive read`` on a provider with no document-content API (Microsoft / Graph)."""


@dataclass(frozen=True, slots=True)
class UploadSource:
    """A local file that passed the checks, open for reading, and what it will be called."""

    handle: BinaryIO
    mime_type: str
    mtime_ns: int
    name: str
    path: Path
    size: int

    def __enter__(self) -> UploadSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.handle.close()

    def ensure_unchanged(self) -> None:
        """Refuse when the file grew, shrank or was rewritten since it was checked."""
        info = os.fstat(self.handle.fileno())
        if info.st_size != self.size or info.st_mtime_ns != self.mtime_ns:
            raise DriveUploadError(f"{self.name!r} changed while it was being uploaded")


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


def path_is_inside(path: Path, directory: Path) -> bool:
    """Whether `path` is in `directory`, judged by which directory it is, not how it is spelled.

    A string comparison of resolved paths misses a case-insensitive filesystem (`.CONFIG` is
    `.config`), so each parent is also compared with the directory by device and inode.
    """
    try:
        if path.resolve().is_relative_to(directory.resolve()):
            return True
        target = directory.stat()
    except OSError:
        return False
    for parent in (path, *path.parents):
        try:
            if os.path.samestat(parent.stat(), target):
                return True
        except OSError:
            continue
    return False


def resolve_upload_source(
    file: str,
    *,
    name: str | None,
    mime_type: str | None,
    config_dir: Path,
    denied: Sequence[Path] = (),
) -> UploadSource:
    """Check the local file `drive upload` was asked to send, before anything leaves the machine.

    The file is opened once, without following a symlink in its final place, and every check
    and every later read uses that open file, so it cannot be swapped for another after the
    check. It must be a regular file with a single link (a hard link can be a secret under
    another name), no larger than `UPLOAD_MAX_BYTES`, and must not be inside blumkin's own
    config directory (tokens and auth records), one of the well-known credential places under
    the home directory, an `.env` file, or one of the `denied` files (for example the Google
    OAuth client file). The name defaults to the file's own (as given, so a symlink keeps its
    link's name) and must be a plain file name; the type is detected from it unless given.
    Close the result when done.
    """
    text = file.strip()
    if not text:
        raise DriveUploadError("--file must name a local file")
    given = Path(text).expanduser() if text.startswith("~/") else Path(text)
    resolved = given.resolve()
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(resolved, flags)
    except OSError as exc:
        raise DriveUploadError(f"no such file to upload: {file!r}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise DriveUploadError(f"{file!r} is not a regular file")
        if info.st_nlink > 1:
            raise DriveUploadError(
                f"{file!r} has {info.st_nlink} hard links: refusing to upload it "
                "(a link can be a secret under another name); copy it first"
            )
        _refuse_secrets(resolved, info, config_dir, denied)
        if info.st_size > UPLOAD_MAX_BYTES:
            raise DriveUploadError(
                f"expected a file of at most {UPLOAD_MAX_BYTES} bytes, got {info.st_size}"
            )
        chosen = _upload_name(name if name is not None else given.name)
        if mime_type is not None:
            if not _MIME_TYPE.fullmatch(mime_type.strip()):
                raise DriveUploadError(
                    f"--mime-type must look like type/subtype, got {mime_type!r}"
                )
            kind = mime_type.strip()
        else:
            kind = mimetypes.guess_type(chosen)[0] or "application/octet-stream"
        handle = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    return UploadSource(
        handle=handle,
        mime_type=kind,
        mtime_ns=info.st_mtime_ns,
        name=chosen,
        path=resolved,
        size=info.st_size,
    )


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
        lines.append(f"  {_drive_weblink(item)}")
    exports = item.get("export_formats") or []
    if exports:
        lines.append(f"  export: {', '.join(exports)}")
    return lines


def format_drive_list_human(payload: dict[str, Any]) -> list[str]:
    items = payload.get("items") or []
    if not items:
        return ["(no items)"]
    lines = [f"{len(items)} item(s):"]
    for item in items:
        size = "" if item.get("size") is None else f"  {item['size']}b"
        lines.append(f"  [{item.get('kind'):>6}] {item.get('name')}{size}  id={item.get('id')}")
    return lines


def format_drive_mkdir_human(payload: dict[str, Any]) -> list[str]:
    folder = payload.get("folder") or {}
    verb = "exists" if payload.get("created") is False else "created"
    lines = [f"Folder {verb}: {folder.get('path')!r}", f"  id={folder.get('id')}"]
    if folder.get("web_url"):
        lines.append(f"  {_drive_weblink(folder, label=str(folder.get('path') or ''))}")
    return lines


def format_drive_move_human(payload: dict[str, Any]) -> list[str]:
    item = payload.get("item") or {}
    return [f"Moved {item.get('name')!r} (id={item.get('id')}) to folder {payload.get('moved_to')}"]


def format_drive_read_human(payload: dict[str, Any]) -> list[str]:
    # A shared Doc is remote content from the same trust boundary as a mail body
    # or Teams message - strip control chars before it reaches the terminal.
    lines: list[str] = sanitize_terminal(str(payload.get("markdown") or "")).splitlines()
    return lines or ["(empty document)"]


def format_drive_rename_human(payload: dict[str, Any]) -> list[str]:
    item = payload.get("item") or {}
    return [f"Renamed to {item.get('name')!r} (id={item.get('id')}; url unchanged)"]


def format_drive_upload_human(payload: dict[str, Any]) -> list[str]:
    item = payload["item"]
    size = f" ({payload['bytes']} bytes)" if payload.get("bytes") is not None else ""
    lines = [f"uploaded {sanitize_terminal(str(item.get('name')))}{size} -> id {item.get('id')}"]
    if item.get("web_url"):
        lines.append(f"  {item['web_url']}")
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


def _drive_weblink(item: dict[str, Any], *, label: str = "") -> str:
    """A drive item's web URL as an OSC 8 hyperlink (plain URL off a TTY)."""
    url = str(item.get("web_url") or "")
    text = (label or str(item.get("name") or "")).strip()
    return hyperlink(text or url, url) if url else url


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


def _refuse_secrets(
    resolved: Path, info: os.stat_result, config_dir: Path, denied: Sequence[Path]
) -> None:
    """Refuse a file that is, or sits in, a place that holds credentials."""
    if path_is_inside(resolved, config_dir):
        raise DriveUploadError(
            f"{resolved.name!r} is inside blumkin's config directory (tokens and auth "
            "records): refusing to upload it"
        )
    secret = f"{resolved.name!r} is a secret: refusing to upload it"
    if resolved.name == ".env" or resolved.name.startswith(".env."):
        raise DriveUploadError(secret + " (an .env file)")
    home = Path.home()
    for relative in _SECRET_HOME_FOLDERS:
        if (home / relative).is_dir() and path_is_inside(resolved, home / relative):
            raise DriveUploadError(secret + f" (~/{relative})")
    for relative in _SECRET_HOME_FILES:
        try:
            if os.path.samestat(info, (home / relative).stat()):
                raise DriveUploadError(secret + f" (~/{relative})")
        except OSError:
            continue
    for entry in denied:
        try:
            if os.path.samestat(info, entry.stat()) or path_is_inside(resolved, entry):
                raise DriveUploadError(
                    secret + " (a credentials file blumkin is configured to use)"
                )
        except OSError:
            continue


def _upload_name(name: str) -> str:
    """`name` if it is a plain file name for the drive, else refuse."""
    cleaned = name.strip()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or "/" in cleaned
        or "\\" in cleaned
        or any(ord(char) < 32 or ord(char) == 127 for char in cleaned)
        or len(cleaned.encode("utf-8")) > 255
    ):
        raise DriveUploadError(f"expected a plain file name of at most 255 bytes, got {name!r}")
    return cleaned


def validate_folder_selector(folder: str | None, folder_id: str | None) -> None:
    """`drive list` takes at most one of ``--folder`` / ``--folder-id``."""
    if folder is not None and folder_id is not None:
        raise DriveSelectorError("pass at most one of --folder or --folder-id, not both")


def validate_move_selector(dest_path: str | None, dest_folder_id: str | None) -> None:
    """`drive move` takes exactly one of ``--to`` / ``--to-id`` (a blank value counts as absent)."""
    has_path = bool((dest_path or "").strip())
    has_id = bool((dest_folder_id or "").strip())
    if has_path == has_id:
        raise DriveSelectorError("pass exactly one of --to or --to-id (and it must not be blank)")
