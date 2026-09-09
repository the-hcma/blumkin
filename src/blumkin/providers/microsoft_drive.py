"""Microsoft OneDrive `drive` skills: list / get / download / export, and the
organize verbs mkdir / move / rename (#208, #212).

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
from msgraph.generated.models.folder import Folder
from msgraph.generated.models.item_reference import ItemReference
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
    validate_move_selector,
)

# RFC 6570 templates: `{id}` / `{+path}` / `{+q}` are filled by kiota; the
# `?%24…` query string is literal (same trick as microsoft_docs `_UPLOAD_URL`).
_CHILDREN_BY_ID_BASE = "https://graph.microsoft.com/v1.0/me/drive/items/{id}/children"
_CHILDREN_ROOT_URL = "https://graph.microsoft.com/v1.0/me/drive/root/children"
_CHILD_OF_PATH_URL = "https://graph.microsoft.com/v1.0/me/drive/root:/{+path}:/children"
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
    if _kind(item) not in ("doc", "sheet", "slides"):
        # Graph's ?format=pdf converts Office files only (matches `drive get`'s
        # export_formats gate) - anything else 400s mid-download.
        what = "a folder" if item.folder is not None else f"a {_kind(item)}"
        raise DriveExportError(
            f"{item.name!r} is {what} - Graph only exports Word/Excel/PowerPoint to PDF; "
            "use `drive download` for its raw bytes"
        )
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
        scope_id = _require_folder(await _resolve_folder(client, folder), folder)

    needle = (query or "").strip()
    if needle:
        entries = await _search(client, needle, 0 if scope_id else top)
        if scope_id:
            entries = [e for e in entries if _parent_id(e) == scope_id]
        entries.sort(key=_entry_sort(order_key), reverse=(order_key == "modified"))
    elif scope_id:
        entries = await _children(client, _CHILDREN_BY_ID_BASE, {"id": scope_id}, order_key, top)
    else:
        entries = await _children(client, _CHILDREN_ROOT_URL, {}, order_key, top)

    items = [_to_item(entry) for entry in entries]
    if top > 0:
        items = items[:top]
    return {
        "items": items,
        "query": {"folder": folder, "folder_id": folder_id, "text": query or None},
    }


async def drive_mkdir(*, path: str, config: BlumkinConfig | None = None) -> dict[str, Any]:
    segments = split_path(path)
    if not segments:
        raise ValueError("--path must have at least one folder name")
    cfg = config or load_config()
    client = create_graph_client(cfg)
    existing = await _get_item_by_path(client, segments)
    if existing is not None:
        # A same-named *file* occupies the slot - OneDrive forbids a folder next to it.
        if existing.folder is None:
            raise ValueError(f"{'/'.join(segments)!r} is a file, not a folder")
        return _mkdir_payload(existing, segments, created=False)
    folder = await _mkdir_p(client, segments)
    return _mkdir_payload(folder, segments, created=True)


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
    client = create_graph_client(cfg)

    # Validate the source item first, so a failed move (bad --id, even with
    # --make-parents) never creates stray folders.
    src = await _send_item(client, _ITEM_BY_ID_URL, {"id": item_id}, missing=item_id)
    source_is_folder = src.folder is not None

    if dest_id is not None:
        # Mirror the --to path branch (and the Google backend): reject a typo'd or
        # non-folder id up front instead of letting Graph 400 the PATCH.
        try:
            dest = await _send_item(client, _ITEM_BY_ID_URL, {"id": dest_id}, missing=dest_id)
        except DriveItemNotFoundError as exc:
            raise ValueError(f"--to-id {dest_id!r} does not name a drive item") from exc
        if dest.folder is None:
            raise ValueError(f"--to-id {dest_id!r} is not a folder")
        target = dest_id
    else:
        assert dest_p is not None
        segments = split_path(dest_p)
        if not segments:
            raise ValueError("--to must name a folder, not the drive root")
        folder = await _get_item_by_path(client, segments)
        if folder is not None and folder.folder is None:
            raise ValueError(f"--to {dest_p!r} is a file, not a folder")
        if folder is None:
            if not make_parents:
                raise ValueError(
                    f"no folder at {dest_p!r} - pass --make-parents to create it, "
                    "or --to-id with a folder id"
                )
            if source_is_folder:
                # Creating the chain could land it under the folder being moved
                # (F -> F/Sub), then Graph 400s the move - stray folders left.
                raise ValueError(
                    f"no folder at {dest_p!r} - create the destination first when moving a folder"
                )
            folder = await _mkdir_p(client, segments)
        target = folder.id or ""
    if source_is_folder and target == item_id:
        raise ValueError("a folder cannot be moved into itself")
    patch = DriveItem(parent_reference=ItemReference(id=target))
    moved = await _patch_item(client, item_id, patch)
    return {"ok": True, "item": _to_item(moved), "moved_to": target}


async def drive_read(*, item_id: str, config: BlumkinConfig | None = None) -> dict[str, Any]:
    raise DriveReadUnsupportedError(
        "drive read is not supported for provider=microsoft (Graph has no Word "
        "content API; see docs/DECISIONS.md D11). Use `drive export --to out.pdf` "
        "or open the file in a browser."
    )


async def drive_rename(
    *, item_id: str, name: str, config: BlumkinConfig | None = None
) -> dict[str, Any]:
    if not name.strip():
        raise ValueError("--name must not be empty")
    cfg = config or load_config()
    client = create_graph_client(cfg)
    renamed = await _patch_item(client, item_id, DriveItem(name=name.strip()))
    return {"ok": True, "item": _to_item(renamed)}


def _encoded_path(segments: list[str]) -> str:
    """Percent-encode each segment (so ``#`` / ``?`` cannot truncate) but keep ``/``."""
    return "/".join(quote(segment, safe="") for segment in segments)


def _entry_sort(order_key: str) -> Any:
    if order_key == "name":
        return lambda item: (item.name or "").casefold()
    return lambda item: (
        item.last_modified_date_time.isoformat() if item.last_modified_date_time else ""
    )


def _mkdir_payload(folder: DriveItem, segments: list[str], *, created: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "created": created,
        "folder": {
            "id": folder.id,
            "name": folder.name or segments[-1],
            "path": "/".join(segments),
            "web_url": folder.web_url,
            "provider": "microsoft",
        },
    }


def _parent_id(entry: DriveItem) -> str | None:
    return entry.parent_reference.id if entry.parent_reference is not None else None


def _require_folder(item: DriveItem | None, label: str) -> str:
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


async def _get_item_by_path(client: Any, segments: list[str]) -> DriveItem | None:
    """``GET /me/drive/root:/A/B`` - ``None`` on a real 404, raises on anything else."""
    request_info = RequestInformation(
        Method.GET, _ITEM_BY_PATH_URL, {"path": _encoded_path(segments)}
    )
    try:
        return await client.request_adapter.send_async(request_info, DriveItem, _ERROR_MAP)
    except ODataError as exc:
        if _status(exc) == 404:
            return None
        raise


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


async def _mkdir_p(client: Any, segments: list[str]) -> DriveItem:
    """Create each missing segment under the drive root; return the leaf folder."""
    parent: list[str] = []
    leaf: DriveItem | None = None
    for segment in segments:
        current = [*parent, segment]
        existing = await _get_item_by_path(client, current)
        if existing is not None:
            if existing.folder is None:
                raise ValueError(f"{'/'.join(current)!r} is a file, not a folder")
            leaf = existing
        else:
            # `conflictBehavior=replace` is not documented for POST /children (it
            # applies to copy/upload), so use the default `fail` and treat a lost
            # race (409) as "already there" by re-reading - keeps mkdir -p idempotent.
            body = DriveItem(name=segment, folder=Folder())
            if parent:
                post = RequestInformation(
                    Method.POST, _CHILD_OF_PATH_URL, {"path": _encoded_path(parent)}
                )
            else:
                post = RequestInformation(Method.POST, _CHILDREN_ROOT_URL, {})
            post.set_content_from_parsable(client.request_adapter, "application/json", body)
            try:
                leaf = await client.request_adapter.send_async(post, DriveItem, _ERROR_MAP)
            except ODataError as exc:
                if _status(exc) != 409:
                    raise
                leaf = await _get_item_by_path(client, current)
                if leaf is None or leaf.folder is None:
                    raise ValueError(f"{'/'.join(current)!r} is a file, not a folder") from exc
        parent = current
    assert leaf is not None
    return leaf


async def _patch_item(client: Any, item_id: str, patch: DriveItem) -> DriveItem:
    request_info = RequestInformation(Method.PATCH, _ITEM_BY_ID_URL, {"id": item_id})
    request_info.set_content_from_parsable(client.request_adapter, "application/json", patch)
    try:
        item = await client.request_adapter.send_async(request_info, DriveItem, _ERROR_MAP)
    except ODataError as exc:
        if _status(exc) == 404:
            raise DriveItemNotFoundError(f"no drive item with id {item_id!r}") from exc
        raise
    if item is None:
        raise DriveItemNotFoundError(f"no drive item with id {item_id!r}")
    return item


async def _resolve_folder(client: Any, folder: str) -> DriveItem | None:
    segments = split_path(folder)
    if not segments:
        return None
    return await _get_item_by_path(client, segments)


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
