"""Google Drive `drive` skills: list / get (read side, issue #208).

One ``drive`` v3 discovery client, built on the shared timed + retrying transport.
Google has no real paths - a ``--folder`` path is resolved by walking from
``root`` and matching folder names one segment at a time; an ambiguous segment
raises rather than guessing.
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
    DriveFolderAmbiguousError,
    DriveFolderNotFoundError,
    DriveItemNotFoundError,
    export_mime,
    flatten_google_doc,
    normalize_order,
    resolve_export_dest,
    split_path,
    validate_folder_selector,
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


async def drive_download(
    *, item_id: str, out: str, config: BlumkinConfig | None = None
) -> dict[str, Any]:
    cfg = config or load_config()
    service = _drive_service(cfg)
    try:
        meta = execute(service.files().get(fileId=item_id, fields="id,name,mimeType,size"))
    except HttpError as exc:
        raise _translate(exc, item_id=item_id) from exc
    if meta.get("mimeType") in GOOGLE_NATIVE_MIMES:
        raise DriveDownloadError(
            f"{meta.get('name')!r} is a Google-native {meta['mimeType'].split('.')[-1]} - "
            "it has no raw bytes; use `drive export` instead"
        )
    data = bytes(execute(service.files().get_media(fileId=item_id)))
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
        meta = execute(service.files().get(fileId=item_id, fields="id,name,mimeType"))
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


async def drive_read(*, item_id: str, config: BlumkinConfig | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    creds = get_credentials(cfg, allow_interactive=False, required_scopes=DRIVE_SCOPES)
    docs = build_api_service("docs", "v1", creds=creds, config=cfg)
    try:
        document = execute(docs.documents().get(documentId=item_id))
    except HttpError as exc:
        raise _translate(exc, item_id=item_id) from exc
    return {
        "item": {"id": item_id, "name": document.get("title"), "provider": "google"},
        "markdown": flatten_google_doc(document),
    }


async def drive_get(*, item_id: str, config: BlumkinConfig | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    service = _drive_service(cfg)
    try:
        item = execute(service.files().get(fileId=item_id, fields=_GET_FIELDS))
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
        parent = _resolve_folder_path(service, folder)

    clauses = ["trashed = false"]
    if parent:
        clauses.append(f"{_quote(parent)} in parents")
    needle = (query or "").strip()
    if needle:
        clauses.append(f"(name contains {_quote(needle)} or fullText contains {_quote(needle)})")

    items = _list_all(service, q=" and ".join(clauses), order_by=_ORDER_BY[order_key], top=top)
    return {
        "items": [_to_item(entry) for entry in items],
        "query": {"folder": folder, "folder_id": folder_id, "text": query or None},
    }


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


def _list_all(service: Any, *, q: str, order_by: str, top: int) -> list[dict[str, Any]]:
    """Page ``files.list`` until ``top`` items (``top <= 0`` = every page)."""
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


def _resolve_folder_path(service: Any, path: str) -> str:
    parent = "root"
    for segment in split_path(path):
        q = (
            f"{_quote(parent)} in parents and name = {_quote(segment)} "
            f"and mimeType = {_quote(_FOLDER_MIME)} and trashed = false"
        )
        found = execute(service.files().list(q=q, fields="files(id,name)", pageSize=2)).get(
            "files", []
        )
        if not found:
            raise DriveFolderNotFoundError(
                f"no folder {segment!r} under {path!r} (Drive has no real paths; try --folder-id)"
            )
        if len(found) > 1:
            raise DriveFolderAmbiguousError(
                f"folder segment {segment!r} in {path!r} matches {len(found)} folders; "
                "pass --folder-id instead"
            )
        parent = found[0]["id"]
    return parent


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
