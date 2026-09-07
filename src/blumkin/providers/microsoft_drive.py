"""Microsoft OneDrive `drive` skills: list / get (read side, issue #208).

Graph addresses items by id (``/me/drive/items/{id}``) or by a native path
(``/me/drive/root:/A/B``), so ``--folder`` needs no name-walk here. Raw
``RequestInformation`` calls (same style as ``microsoft_docs``) keep the
dependency surface identical to the rest of the Graph backend.
"""

from __future__ import annotations

from typing import Any

from kiota_abstractions.method import Method
from kiota_abstractions.request_information import RequestInformation
from kiota_abstractions.serialization.parsable_factory import ParsableFactory
from msgraph.generated.models.drive_item import DriveItem
from msgraph.generated.models.drive_item_collection_response import DriveItemCollectionResponse
from msgraph.generated.models.o_data_errors.o_data_error import ODataError

from blumkin.config import BlumkinConfig, load_config
from blumkin.graph import create_graph_client
from blumkin.skills.drive import (
    DriveFolderNotFoundError,
    DriveItemNotFoundError,
    normalize_order,
    validate_folder_selector,
)

_CHILDREN_BY_ID_URL = "https://graph.microsoft.com/v1.0/me/drive/items/{id}/children?%24top=200"
_CHILDREN_BY_PATH_URL = (
    "https://graph.microsoft.com/v1.0/me/drive/root:/{+path}:/children?%24top=200"
)
_CHILDREN_ROOT_URL = "https://graph.microsoft.com/v1.0/me/drive/root/children?%24top=200"
_ERROR_MAP: dict[str, type[ParsableFactory]] = {"4XX": ODataError, "5XX": ODataError}
_ITEM_BY_ID_URL = "https://graph.microsoft.com/v1.0/me/drive/items/{id}"
_OFFICE_KIND = {"docx": "doc", "doc": "doc", "xlsx": "sheet", "xls": "sheet", "pptx": "slides"}
_SEARCH_URL = "https://graph.microsoft.com/v1.0/me/drive/root/search(q='{+q}')?%24top=200"


async def drive_get(*, item_id: str, config: BlumkinConfig | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    client = create_graph_client(cfg)
    item = await _send_item(client, Method.GET, _ITEM_BY_ID_URL, {"id": item_id}, missing=item_id)
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

    needle = (query or "").strip()
    if needle:
        url, params = _SEARCH_URL, {"q": needle}
    elif folder_id:
        url, params = _CHILDREN_BY_ID_URL, {"id": folder_id}
    elif folder:
        url, params = _CHILDREN_BY_PATH_URL, {"path": folder.strip("/")}
    else:
        url, params = _CHILDREN_ROOT_URL, {}

    entries = await _collect(client, url, params)
    if needle and (folder_id or folder):
        entries = [e for e in entries if _under(e, folder_id=folder_id, folder=folder)]
    items = [_to_item(entry) for entry in entries]
    items.sort(key=_SORTERS[order_key], reverse=(order_key == "modified"))
    if top > 0:
        items = items[:top]
    return {
        "items": items,
        "query": {"folder": folder, "folder_id": folder_id, "text": query or None},
    }


_SORTERS = {
    "modified": lambda item: item.get("modified") or "",
    "name": lambda item: (item.get("name") or "").casefold(),
}


async def _collect(client: Any, url: str, params: dict[str, str]) -> list[DriveItem]:
    """Follow ``@odata.nextLink`` from the first page onward."""
    out: list[DriveItem] = []
    request_info = RequestInformation(Method.GET, url, params)
    while True:
        try:
            page = await client.request_adapter.send_async(
                request_info, DriveItemCollectionResponse, _ERROR_MAP
            )
        except ODataError as exc:
            if _status(exc) == 404:
                raise DriveFolderNotFoundError(
                    f"no folder at {params.get('path') or params.get('id') or 'root'!r}"
                ) from exc
            raise
        if page is None:
            break
        out.extend(page.value or [])
        nxt = page.odata_next_link
        if not nxt:
            break
        request_info = RequestInformation(Method.GET, nxt, {})
    return out


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


async def _send_item(
    client: Any, method: Method, url: str, params: dict[str, str], *, missing: str
) -> DriveItem:
    request_info = RequestInformation(method, url, params)
    try:
        item = await client.request_adapter.send_async(request_info, DriveItem, _ERROR_MAP)
    except ODataError as exc:
        if _status(exc) == 404:
            raise DriveItemNotFoundError(f"no drive item with id {missing!r}") from exc
        raise
    if item is None:
        raise DriveItemNotFoundError(f"no drive item with id {missing!r}")
    return item


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


def _under(entry: DriveItem, *, folder_id: str | None, folder: str | None) -> bool:
    parent = entry.parent_reference
    if parent is None:
        return False
    if folder_id:
        return parent.id == folder_id
    path = (parent.path or "").split("root:", 1)[-1].strip("/")
    return path.endswith((folder or "").strip("/"))
