"""Microsoft OneDrive `drive` skills: list / get / download / export (#208).

Graph addresses items by id (``/me/drive/items/{id}``) or by a native path
(``/me/drive/root:/A/B``). Path segments and the search literal are
percent-/OData-escaped before templating - kiota's ``{+…}`` reserved expansion
would otherwise let a ``#`` truncate the request or an apostrophe unbalance
``search(q='…')``. Raw ``RequestInformation`` calls (same style as
``microsoft_docs``) keep the dependency surface identical to the rest of the
Graph backend.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from kiota_abstractions.method import Method
from kiota_abstractions.request_information import RequestInformation
from kiota_abstractions.serialization.parsable_factory import ParsableFactory
from msgraph.generated.models.drive_item import DriveItem
from msgraph.generated.models.drive_item_collection_response import DriveItemCollectionResponse
from msgraph.generated.models.o_data_errors.o_data_error import ODataError

from blumkin.attachments import resolve_single_download_dest
from blumkin.config import BlumkinConfig, load_config
from blumkin.graph import create_graph_client
from blumkin.skills.drive import (
    DriveDownloadError,
    DriveExportError,
    DriveFolderNotFoundError,
    DriveItemNotFoundError,
    DriveReadUnsupportedError,
    export_mime,
    normalize_order,
    resolve_export_dest,
    split_path,
    validate_folder_selector,
)

# RFC 6570 templates: `{id}` / `{+path}` / `{+q}` are filled by kiota; the
# `?%24…` query string is literal (same trick as microsoft_docs `_UPLOAD_URL`).
_CHILDREN_BY_ID_BASE = "https://graph.microsoft.com/v1.0/me/drive/items/{id}/children"
_CHILDREN_ROOT_BASE = "https://graph.microsoft.com/v1.0/me/drive/root/children"
_CONTENT_URL = "https://graph.microsoft.com/v1.0/me/drive/items/{id}/content"
_ERROR_MAP: dict[str, type[ParsableFactory]] = {"4XX": ODataError, "5XX": ODataError}
_EXPORT_URL = "https://graph.microsoft.com/v1.0/me/drive/items/{id}/content?format=pdf"
_ITEM_BY_ID_URL = "https://graph.microsoft.com/v1.0/me/drive/items/{id}"
_ITEM_BY_PATH_URL = "https://graph.microsoft.com/v1.0/me/drive/root:/{+path}"
_OFFICE_KIND = {"docx": "doc", "doc": "doc", "xlsx": "sheet", "xls": "sheet", "pptx": "slides"}
_ORDER_FIELD = {"modified": "lastModifiedDateTime desc", "name": "name"}
_PAGE = 200
_SEARCH_BASE = "https://graph.microsoft.com/v1.0/me/drive/root/search(q='{+q}')"


async def drive_download(
    *, item_id: str, out: str, config: BlumkinConfig | None = None
) -> dict[str, Any]:
    cfg = config or load_config()
    client = create_graph_client(cfg)
    item = await _send_item(client, _ITEM_BY_ID_URL, {"id": item_id}, missing=item_id)
    if item.folder is not None:
        raise DriveDownloadError(
            f"{item.name!r} is a folder - list it with `drive list --folder-id {item_id}`"
        )
    data = await _send_bytes(client, _CONTENT_URL, {"id": item_id}, missing=item_id)
    dest = resolve_single_download_dest(out, item.name or item_id)
    dest.write_bytes(data)
    return {
        "id": item_id,
        "name": item.name,
        "bytes": len(data),
        "saved_path": str(dest.resolve()),
        "provider": "microsoft",
    }


async def drive_export(
    *, item_id: str, to: str, config: BlumkinConfig | None = None
) -> dict[str, Any]:
    ext, _mime = export_mime(to)
    if ext != "pdf":
        raise DriveExportError(
            f"OneDrive / Graph can only export Office files to PDF (got --to {to!r}); "
            "pull the raw file with `drive download`"
        )
    cfg = config or load_config()
    client = create_graph_client(cfg)
    item = await _send_item(client, _ITEM_BY_ID_URL, {"id": item_id}, missing=item_id)
    if item.folder is not None:
        raise DriveExportError(f"{item.name!r} is a folder - nothing to export")
    data = await _send_bytes(client, _EXPORT_URL, {"id": item_id}, missing=item_id)
    dest = resolve_export_dest(to)
    dest.write_bytes(data)
    return {
        "id": item_id,
        "name": item.name,
        "format": "pdf",
        "bytes": len(data),
        "saved_path": str(dest.resolve()),
        "provider": "microsoft",
    }


async def drive_get(*, item_id: str, config: BlumkinConfig | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    client = create_graph_client(cfg)
    item = await _send_item(client, _ITEM_BY_ID_URL, {"id": item_id}, missing=item_id)
    payload = _to_item(item)
    payload["owners"] = _owners(item)
    # Office docs on OneDrive export to PDF only (Graph limitation).
    payload["export_formats"] = ["pdf"] if payload["kind"] in ("doc", "sheet", "slides") else []
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
    client = create_graph_client(cfg)

    scope_id = folder_id
    if folder is not None:
        scope_id = _require_id(await _resolve_folder(client, folder), folder)

    needle = (query or "").strip()
    if needle:
        entries = await _search(client, needle, 0 if scope_id else top)
        if scope_id:
            entries = [e for e in entries if _parent_id(e) == scope_id]
        entries.sort(key=_entry_sort(order_key), reverse=(order_key == "modified"))
    elif scope_id:
        entries = await _children(client, _CHILDREN_BY_ID_BASE, {"id": scope_id}, order_key, top)
    else:
        entries = await _children(client, _CHILDREN_ROOT_BASE, {}, order_key, top)

    items = [_to_item(entry) for entry in entries]
    if top > 0:
        items = items[:top]
    return {
        "items": items,
        "query": {"folder": folder, "folder_id": folder_id, "text": query or None},
    }


async def drive_read(*, item_id: str, config: BlumkinConfig | None = None) -> dict[str, Any]:
    raise DriveReadUnsupportedError(
        "drive read is not supported for provider=microsoft (Graph has no Word "
        "content API; see docs/DECISIONS.md D11). Use `drive export --to out.pdf` "
        "or open the file in a browser."
    )


def _encoded_path(path: str) -> str:
    """Percent-encode each segment (so ``#`` / ``?`` cannot truncate) but keep ``/``."""
    return "/".join(quote(segment, safe="") for segment in split_path(path))


def _entry_sort(order_key: str) -> Any:
    if order_key == "name":
        return lambda item: (item.name or "").casefold()
    return lambda item: (
        item.last_modified_date_time.isoformat() if item.last_modified_date_time else ""
    )


def _parent_id(entry: DriveItem) -> str | None:
    return entry.parent_reference.id if entry.parent_reference is not None else None


def _require_id(item: DriveItem | None, label: str) -> str:
    if item is None or item.folder is None or not item.id:
        raise DriveFolderNotFoundError(f"no folder at {label!r}")
    return item.id


async def _children(
    client: Any, base: str, params: dict[str, str], order_key: str, top: int
) -> list[DriveItem]:
    """List a folder's children, ordered and capped server-side; stop paging at ``top``."""
    page_size = _PAGE if top <= 0 else min(_PAGE, top)
    url = f"{base}?%24top={page_size}&%24orderby={quote(_ORDER_FIELD[order_key])}"
    out: list[DriveItem] = []
    request_info = RequestInformation(Method.GET, url, params)
    while True:
        page = await _get_page(client, request_info)
        if page is None:
            break
        out.extend(page.value or [])
        if (top > 0 and len(out) >= top) or not page.odata_next_link:
            break
        request_info = RequestInformation(Method.GET, page.odata_next_link, {})
    return out


async def _get_page(
    client: Any, request_info: RequestInformation
) -> DriveItemCollectionResponse | None:
    try:
        return await client.request_adapter.send_async(
            request_info, DriveItemCollectionResponse, _ERROR_MAP
        )
    except ODataError as exc:
        if _status(exc) == 404:
            raise DriveFolderNotFoundError("no such folder") from exc
        raise


async def _resolve_folder(client: Any, folder: str) -> DriveItem | None:
    if not split_path(folder):
        return None
    request_info = RequestInformation(
        Method.GET, _ITEM_BY_PATH_URL, {"path": _encoded_path(folder)}
    )
    try:
        return await client.request_adapter.send_async(request_info, DriveItem, _ERROR_MAP)
    except ODataError as exc:
        if _status(exc) == 404:
            return None
        raise


async def _search(client: Any, needle: str, top: int) -> list[DriveItem]:
    # OData string literal: an apostrophe is doubled; kiota `{+q}` then leaves it
    # inside search(q='…') without re-encoding the slashes a query may contain.
    url = _SEARCH_BASE if top <= 0 else f"{_SEARCH_BASE}?%24top={min(_PAGE, top)}"
    out: list[DriveItem] = []
    request_info = RequestInformation(Method.GET, url, {"q": needle.replace("'", "''")})
    while True:
        page = await _get_page(client, request_info)
        if page is None:
            break
        out.extend(page.value or [])
        if (top > 0 and len(out) >= top) or not page.odata_next_link:
            break
        request_info = RequestInformation(Method.GET, page.odata_next_link, {})
    return out


async def _send_bytes(client: Any, url: str, params: dict[str, str], *, missing: str) -> bytes:
    request_info = RequestInformation(Method.GET, url, params)
    try:
        result = await client.request_adapter.send_primitive_async(
            request_info, "bytes", _ERROR_MAP
        )
    except ODataError as exc:
        if _status(exc) == 404:
            raise DriveItemNotFoundError(f"no drive item with id {missing!r}") from exc
        raise
    if result is None:
        raise DriveItemNotFoundError(f"Graph returned no content for {missing!r}")
    return bytes(result)


async def _send_item(client: Any, url: str, params: dict[str, str], *, missing: str) -> DriveItem:
    request_info = RequestInformation(Method.GET, url, params)
    try:
        item = await client.request_adapter.send_async(request_info, DriveItem, _ERROR_MAP)
    except ODataError as exc:
        if _status(exc) == 404:
            raise DriveItemNotFoundError(f"no drive item with id {missing!r}") from exc
        raise
    if item is None:
        raise DriveItemNotFoundError(f"no drive item with id {missing!r}")
    return item


def _kind(item: DriveItem) -> str:
    if item.folder is not None:
        return "folder"
    name = (item.name or "").rsplit(".", 1)
    ext = name[1].lower() if len(name) == 2 else ""
    return _OFFICE_KIND.get(ext, "file")


def _owners(item: DriveItem) -> list[dict[str, Any]]:
    identity_set = item.created_by or item.last_modified_by
    user = getattr(identity_set, "user", None) if identity_set is not None else None
    if user is None:
        return []
    return [{"name": user.display_name, "email": getattr(user, "additional_data", {}).get("email")}]


def _status(exc: ODataError) -> int | None:
    status = getattr(exc, "response_status_code", None)
    return status if isinstance(status, int) else None


def _to_item(item: DriveItem) -> dict[str, Any]:
    parent = item.parent_reference
    modified = item.last_modified_date_time
    return {
        "id": item.id,
        "name": item.name,
        "mime_type": (item.file.mime_type if item.file is not None else None),
        "kind": _kind(item),
        "size": item.size,
        "modified": modified.isoformat() if modified is not None else None,
        "web_url": item.web_url,
        "parent_id": parent.id if parent is not None else None,
        "provider": "microsoft",
    }
