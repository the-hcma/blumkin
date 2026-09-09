"""Google Drive `drive` skills: list / get / download / export / read (issue #208).

One ``drive`` v3 discovery client, built on the shared timed + retrying transport.
Google has no real paths - a ``--folder`` path is resolved by walking from
``root`` and matching folder names one segment at a time; an ambiguous segment
raises rather than guessing. Every verb gates on the item's Drive ``mimeType``
before hitting a type-specific API, so a listable-but-wrong-kind id is a clean
``usage_error``, not a raw Docs / media 400.
"""

from __future__ import annotations

from typing import Any

from googleapiclient.errors import HttpError

from blumkin.attachments import resolve_single_download_dest
from blumkin.config import BlumkinConfig, load_config
from blumkin.providers.google_auth import DRIVE_SCOPES, get_credentials
from blumkin.providers.google_http import build_api_service, execute
from blumkin.skills.drive import (
    GOOGLE_NATIVE_MIMES,
    DriveDownloadError,
    DriveExportError,
    DriveFolderAmbiguousError,
    DriveFolderNotFoundError,
    DriveItemNotFoundError,
    DriveReadUnsupportedError,
    export_mime,
    flatten_google_doc,
    normalize_order,
    resolve_export_dest,
    split_path,
    validate_folder_selector,
    validate_move_selector,
)

_FOLDER_MIME = "application/vnd.google-apps.folder"
_GET_FIELDS = (
    "id,name,mimeType,size,modifiedTime,webViewLink,parents,owners(displayName,emailAddress),"
    "exportLinks"
)
_KINDS = {
    "application/vnd.google-apps.folder": "folder",
    "application/vnd.google-apps.document": "doc",
    "application/vnd.google-apps.spreadsheet": "sheet",
    "application/vnd.google-apps.presentation": "slides",
}
_LIST_FIELDS = "nextPageToken,files(id,name,mimeType,size,modifiedTime,webViewLink,parents)"
_ORDER_BY = {"modified": "modifiedTime desc", "name": "name_natural"}
# files.list caps pageSize at 1000; keep headroom under --top 0 (unbounded) walks.
_PAGE_SIZE = 200
# Drive v3 hides Shared Drive items unless these are set. blumkin does not manage
# Shared Drives (create *into* one - #212 non-goal), but read / move / rename of a
# file the user already has in one should not silently 404.
_SHARED: dict[str, Any] = {"supportsAllDrives": True}
_SHARED_LIST: dict[str, Any] = {"supportsAllDrives": True, "includeItemsFromAllDrives": True}


async def drive_download(
    *, item_id: str, out: str, config: BlumkinConfig | None = None
) -> dict[str, Any]:
    cfg = config or load_config()
    service = _drive_service(cfg)
    try:
        meta = execute(
            service.files().get(fileId=item_id, fields="id,name,mimeType,size", **_SHARED)
        )
    except HttpError as exc:
        raise _translate(exc, item_id=item_id) from exc
    mime = meta.get("mimeType") or ""
    if mime.startswith("application/vnd.google-apps."):
        # Folders, shortcuts, Forms, Sites, Apps Script, and Docs/Sheets/Slides all
        # lack a raw byte stream. Only the last three have an export path.
        kind = mime.rsplit(".", 1)[-1]
        hint = (
            "use `drive export`"
            if mime in GOOGLE_NATIVE_MIMES
            else "it has no downloadable content"
        )
        raise DriveDownloadError(f"{meta.get('name')!r} is a Google-native {kind} - {hint}")
    try:
        data = bytes(execute(service.files().get_media(fileId=item_id)))
    except HttpError as exc:
        raise _translate(exc, item_id=item_id) from exc
    dest = resolve_single_download_dest(out, meta.get("name") or item_id)
    dest.write_bytes(data)
    return {
        "id": item_id,
        "name": meta.get("name"),
        "bytes": len(data),
        "saved_path": str(dest.resolve()),
        "provider": "google",
    }


async def drive_export(
    *, item_id: str, to: str, config: BlumkinConfig | None = None
) -> dict[str, Any]:
    _ext, mime = export_mime(to)
    cfg = config or load_config()
    service = _drive_service(cfg)
    try:
        meta = execute(
            service.files().get(fileId=item_id, fields="id,name,mimeType,exportLinks", **_SHARED)
        )
    except HttpError as exc:
        raise _translate(exc, item_id=item_id) from exc
    available = set(meta.get("exportLinks") or {})
    if not available:
        raise DriveExportError(
            f"{meta.get('name')!r} is not a Google-native document - use `drive download` "
            "for its raw bytes"
        )
    if mime not in available:
        formats = ", ".join(sorted(_EXPORT_EXT[m] for m in available if m in _EXPORT_EXT))
        raise DriveExportError(
            f"{meta.get('name')!r} cannot export to {_ext} - available: {formats or '(none)'}"
        )
    try:
        data = bytes(execute(service.files().export_media(fileId=item_id, mimeType=mime)))
    except HttpError as exc:
        raise _translate(exc, item_id=item_id) from exc
    dest = resolve_export_dest(to)
    dest.write_bytes(data)
    return {
        "id": item_id,
        "name": meta.get("name"),
        "format": _ext,
        "bytes": len(data),
        "saved_path": str(dest.resolve()),
        "provider": "google",
    }


async def drive_get(*, item_id: str, config: BlumkinConfig | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    service = _drive_service(cfg)
    try:
        item = execute(service.files().get(fileId=item_id, fields=_GET_FIELDS, **_SHARED))
    except HttpError as exc:
        raise _translate(exc, item_id=item_id) from exc
    payload = _to_item(item)
    payload["owners"] = [
        {"name": owner.get("displayName"), "email": owner.get("emailAddress")}
        for owner in item.get("owners") or []
    ]
    payload["export_formats"] = sorted(
        _EXPORT_EXT[mime] for mime in (item.get("exportLinks") or {}) if mime in _EXPORT_EXT
    )
    return {"item": payload}


async def drive_list(
    *,
    folder_id: str | None = None,
    folder: str | None = None,
    query: str | None = None,
    order: str = "modified",
    top: int = 50,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    validate_folder_selector(folder, folder_id)
    order_key = normalize_order(order)
    cfg = config or load_config()
    service = _drive_service(cfg)

    parent = folder_id
    if parent is None and folder:
        parent, _ = _resolve_folder_path(service, folder)

    clauses = ["trashed = false"]
    if parent:
        clauses.append(f"{_quote(parent)} in parents")
    needle = (query or "").strip()
    if needle:
        clauses.append(f"(name contains {_quote(needle)} or fullText contains {_quote(needle)})")

    # Only reach into Shared Drives when the listing is scoped to a folder (which
    # may itself live in one). An unscoped root list stays My Drive - pulling in
    # every file from every Shared Drive would silently widen the verb.
    items = _list_all(
        service,
        q=" and ".join(clauses),
        order_by=_ORDER_BY[order_key],
        top=top,
        all_drives=parent is not None,
    )
    return {
        "items": [_to_item(entry) for entry in items],
        "query": {"folder": folder, "folder_id": folder_id, "text": query or None},
    }


async def drive_mkdir(*, path: str, config: BlumkinConfig | None = None) -> dict[str, Any]:
    if not split_path(path):
        raise ValueError("--path must have at least one folder name")
    cfg = config or load_config()
    service = _drive_service(cfg)
    # Probe first so a no-op reports created=False without a write.
    try:
        existing_id, existing = _resolve_folder_path(service, path)
        created = False
        folder = existing
        folder_id = existing_id
    except DriveFolderNotFoundError:
        folder_id, folder = _resolve_folder_path(service, path, create=True)
        created = True
    return {
        "ok": True,
        "created": created,
        "folder": {
            "id": folder_id,
            "name": folder.get("name") or split_path(path)[-1],
            "path": "/".join(split_path(path)),
            "web_url": folder.get("webViewLink"),
            "provider": "google",
        },
    }


async def drive_move(
    *,
    item_id: str,
    dest_folder_id: str | None = None,
    dest_path: str | None = None,
    make_parents: bool = False,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    dest_id = (dest_folder_id or "").strip() or None
    dest_p = (dest_path or "").strip() or None
    validate_move_selector(dest_p, dest_id)
    cfg = config or load_config()
    service = _drive_service(cfg)

    # Validate the source item first, so a failed move (bad --id, even with
    # --make-parents) is a true no-op and never leaves stray folders behind.
    try:
        current = execute(
            service.files().get(fileId=item_id, fields="id,name,mimeType,parents", **_SHARED)
        )
    except HttpError as exc:
        raise _translate(exc, item_id=item_id) from exc
    parents = current.get("parents") or []
    if len(parents) > 1:
        # Legacy multi-parent item: removing it from *all* parents (Google's move
        # needs the exact parent being left, which the id-only input cannot name)
        # would change visibility in folders shared with other people.
        raise ValueError(
            f"{current.get('name')!r} is in {len(parents)} folders at once - move it in "
            "the Drive web UI so the right copy is affected"
        )
    source_is_folder = current.get("mimeType") == _FOLDER_MIME

    if dest_id is not None:
        target = _validate_dest_folder_id(service, dest_id)
    else:
        assert dest_p is not None
        if not split_path(dest_p):
            raise ValueError("--to must name a folder, not the drive root")
        # Moving a folder into a path that would be created under it (F -> F/Sub)
        # creates the chain and then fails the move. Require the destination to
        # already exist when the source is a folder, so nothing is left behind.
        create = make_parents and not source_is_folder
        try:
            target, _ = _resolve_folder_path(service, dest_p, create=create)
        except DriveFolderNotFoundError as exc:
            hint = (
                "create the destination first when moving a folder"
                if source_is_folder and make_parents
                else f"pass --make-parents to create {dest_p!r}, or --to-id"
            )
            raise ValueError(f"{exc} - {hint}") from exc
    if source_is_folder and target == item_id:
        raise ValueError("a folder cannot be moved into itself")

    try:
        moved = execute(
            service.files().update(
                fileId=item_id,
                addParents=target,
                removeParents=",".join(parents),
                fields=_GET_FIELDS,
                **_SHARED,
            )
        )
    except HttpError as exc:
        raise _translate(exc, item_id=item_id) from exc
    return {"ok": True, "item": _to_item(moved), "moved_to": target}


async def drive_read(*, item_id: str, config: BlumkinConfig | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    creds = get_credentials(cfg, allow_interactive=False, required_scopes=DRIVE_SCOPES)
    # Gate on the Drive mimeType first: `documents.get` on a Sheet / Slides /
    # folder id 400s, which would surface as a misleading not_found / graph_error
    # instead of the usage_error the sibling verbs give.
    drive = build_api_service("drive", "v3", creds=creds, config=cfg)
    try:
        meta = execute(drive.files().get(fileId=item_id, fields="id,name,mimeType", **_SHARED))
    except HttpError as exc:
        raise _translate(exc, item_id=item_id) from exc
    if meta.get("mimeType") != "application/vnd.google-apps.document":
        kind = _KINDS.get(meta.get("mimeType", ""), "file")
        raise DriveReadUnsupportedError(
            f"{meta.get('name')!r} is a {kind}, not a Google Doc - `drive read` only "
            "flattens Docs; use `drive export` or `drive get`"
        )
    docs = build_api_service("docs", "v1", creds=creds, config=cfg)
    try:
        document = execute(docs.documents().get(documentId=item_id))
    except HttpError as exc:
        raise _translate(exc, item_id=item_id) from exc
    return {
        "item": {"id": item_id, "name": document.get("title"), "provider": "google"},
        "markdown": flatten_google_doc(document),
    }


async def drive_rename(
    *, item_id: str, name: str, config: BlumkinConfig | None = None
) -> dict[str, Any]:
    if not name.strip():
        raise ValueError("--name must not be empty")
    cfg = config or load_config()
    service = _drive_service(cfg)
    try:
        renamed = execute(
            service.files().update(
                fileId=item_id, body={"name": name.strip()}, fields=_GET_FIELDS, **_SHARED
            )
        )
    except HttpError as exc:
        raise _translate(exc, item_id=item_id) from exc
    return {"ok": True, "item": _to_item(renamed)}


_EXPORT_EXT = {
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/html": "html",
    "text/csv": "csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}


def _drive_service(cfg: BlumkinConfig) -> Any:
    creds = get_credentials(cfg, allow_interactive=False, required_scopes=DRIVE_SCOPES)
    return build_api_service("drive", "v3", creds=creds, config=cfg)


def _list_all(
    service: Any, *, q: str, order_by: str, top: int, all_drives: bool
) -> list[dict[str, Any]]:
    """Page ``files.list`` until ``top`` items (``top <= 0`` = every page).

    ``all_drives`` folds Shared Drive items into the results - only set it for a
    folder-scoped listing, never the unfiltered My Drive root.
    """
    shared = _SHARED_LIST if all_drives else _SHARED
    out: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        remaining = _PAGE_SIZE if top <= 0 else min(_PAGE_SIZE, top - len(out))
        if remaining <= 0:
            break
        response = execute(
            service.files().list(
                q=q,
                orderBy=order_by,
                pageSize=remaining,
                fields=_LIST_FIELDS,
                pageToken=page_token,
                **shared,
            )
        )
        out.extend(response.get("files") or [])
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return out if top <= 0 else out[:top]


def _quote(value: str) -> str:
    """Escape a value for the Drive query language (single-quoted literal)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _validate_dest_folder_id(service: Any, folder_id: str) -> str:
    """`--to-id` must name an existing folder; a missing / non-folder id is a usage
    error (exit 2). A transient 429/5xx or a 403 propagates unchanged."""
    try:
        meta = execute(service.files().get(fileId=folder_id, fields="id,mimeType", **_SHARED))
    except HttpError as exc:
        translated = _translate(exc, item_id=folder_id)
        if isinstance(translated, DriveItemNotFoundError):
            raise ValueError(f"--to-id {folder_id!r} does not name a drive item") from exc
        raise translated from exc
    if meta.get("mimeType") != _FOLDER_MIME:
        raise ValueError(f"--to-id {folder_id!r} is not a folder")
    return folder_id


def _resolve_folder_path(
    service: Any, path: str, *, create: bool = False
) -> tuple[str, dict[str, Any]]:
    """Walk ``path`` from ``root`` a segment at a time; return ``(folder_id, last_folder)``.

    ``last_folder`` is the raw ``files`` resource for the final segment (empty for
    the drive root). With ``create`` a missing segment is created (``mkdir -p``);
    without it a missing segment raises :class:`DriveFolderNotFoundError`.
    """
    parent = "root"
    last: dict[str, Any] = {}
    for segment in split_path(path):
        q = (
            f"{_quote(parent)} in parents and name = {_quote(segment)} "
            f"and mimeType = {_quote(_FOLDER_MIME)} and trashed = false"
        )
        found = execute(
            service.files().list(
                q=q, fields="files(id,name,webViewLink)", pageSize=2, **_SHARED_LIST
            )
        ).get("files", [])
        if len(found) > 1:
            raise DriveFolderAmbiguousError(
                f"folder segment {segment!r} in {path!r} matches {len(found)} folders; "
                "pass --folder-id instead"
            )
        if found:
            last = found[0]
        elif create:
            last = execute(
                service.files().create(
                    body={"name": segment, "mimeType": _FOLDER_MIME, "parents": [parent]},
                    fields="id,name,webViewLink",
                    **_SHARED,
                )
            )
        else:
            raise DriveFolderNotFoundError(
                f"no folder {segment!r} under {path!r} (Drive has no real paths; try --folder-id)"
            )
        parent = last["id"]
    return parent, last


def _to_item(entry: dict[str, Any]) -> dict[str, Any]:
    raw_size = entry.get("size")
    parents = entry.get("parents") or []
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "mime_type": entry.get("mimeType"),
        "kind": _KINDS.get(entry.get("mimeType", ""), "file"),
        "size": int(raw_size) if raw_size not in (None, "") else None,
        "modified": entry.get("modifiedTime"),
        "web_url": entry.get("webViewLink"),
        "parent_id": parents[0] if parents else None,
        "provider": "google",
    }


def _translate(exc: HttpError, *, item_id: str) -> Exception:
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "resp", None), "status", None
    )
    try:
        status = int(status) if status is not None else None
    except TypeError, ValueError:
        status = None
    if status == 404:
        return DriveItemNotFoundError(f"no drive item with id {item_id!r}")
    return exc
