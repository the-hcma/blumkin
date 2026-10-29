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

import asyncio
import contextlib
import ipaddress
import json
import math
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from kiota_abstractions.method import Method
from kiota_abstractions.request_information import RequestInformation
from kiota_abstractions.serialization.parsable_factory import ParsableFactory
from msgraph.generated.models.drive_item import DriveItem
from msgraph.generated.models.drive_item_collection_response import DriveItemCollectionResponse
from msgraph.generated.models.file import File
from msgraph.generated.models.folder import Folder
from msgraph.generated.models.item_reference import ItemReference
from msgraph.generated.models.o_data_errors.o_data_error import ODataError
from msgraph.generated.models.upload_session import UploadSession

from blumkin.attachments import resolve_single_download_dest, write_new_private_file
from blumkin.config import BlumkinConfig, load_config
from blumkin.graph import create_graph_client
from blumkin.skills.drive import (
    DriveDownloadError,
    DriveExportError,
    DriveFolderNotFoundError,
    DriveItemNotFoundError,
    DriveReadUnsupportedError,
    DriveUploadError,
    UploadSource,
    export_mime,
    normalize_order,
    resolve_export_dest,
    resolve_upload_source,
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
_ONEDRIVE_DEVICE_NAMES = frozenset(
    {"AUX", "CON", "NUL", "PRN"}
    | {f"COM{digit}" for digit in range(1, 10)}
    | {f"LPT{digit}" for digit in range(1, 10)}
)
_ORDER_FIELD = {"modified": "lastModifiedDateTime desc", "name": "name"}
_PAGE = 200
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_SEARCH_BASE = "https://graph.microsoft.com/v1.0/me/drive/root/search(q='{+q}')"
_SESSION_PARENT_URL = (
    "https://graph.microsoft.com/v1.0/me/drive/items/{parent}:/{+path}:/createUploadSession"
)
_SESSION_ROOT_URL = "https://graph.microsoft.com/v1.0/me/drive/root:/{+path}:/createUploadSession"
# Graph's simple PUT takes at most 4 MB.
_SIMPLE_UPLOAD_MAX = 4_000_000
# A session takes chunks that are multiples of 320 KiB.
_UPLOAD_CHUNK = 320 * 1024 * 12
# `fail` so an upload can never replace an existing file of that name (Graph's default is replace).
_UPLOAD_PARENT_URL = (
    "https://graph.microsoft.com/v1.0/me/drive/items/{parent}:/{+path}:/content"
    "?%40microsoft.graph.conflictBehavior=fail"
)
_UPLOAD_RETRIES = 3
_UPLOAD_ROOT_URL = (
    "https://graph.microsoft.com/v1.0/me/drive/root:/{+path}:/content"
    "?%40microsoft.graph.conflictBehavior=fail"
)


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
    write_new_private_file(dest, data)
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
    write_new_private_file(dest, data)
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
        if not folder.id:
            raise DriveFolderNotFoundError(f"no folder id resolved for {dest_p!r}")
        target = folder.id
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


async def drive_upload(
    *,
    file: str,
    folder: str | None = None,
    folder_id: str | None = None,
    mime_type: str | None = None,
    name: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Upload a local file as a new item; never replaces one of the same name."""
    validate_folder_selector(folder, folder_id)
    cfg = config or load_config()
    # The Google client file is judged too: the setting is kept whatever the provider.
    denied = [cfg.google_oauth_client_file] if cfg.google_oauth_client_file else []
    source = resolve_upload_source(
        file, name=name, mime_type=mime_type, config_dir=cfg.config_dir, denied=denied
    )
    with source:
        _check_onedrive_name(source.name)
        client = create_graph_client(cfg)
        parent: str | None = None
        if folder_id is not None:
            found = await _send_item(client, _ITEM_BY_ID_URL, {"id": folder_id}, missing=folder_id)
            parent = _require_folder(found, folder_id)
        elif folder and split_path(folder):
            parent = _require_folder(await _resolve_folder(client, folder), folder)
        params = {"path": _encoded_path([source.name])}
        if parent is not None:
            params["parent"] = parent
        try:
            if source.size <= _SIMPLE_UPLOAD_MAX:
                url = _UPLOAD_ROOT_URL if parent is None else _UPLOAD_PARENT_URL
                data = source.handle.read()
                source.ensure_unchanged()
                item = _to_item(await _put_small(client, data, source.mime_type, url, params))
                _ensure_unchanged_after(source, item)
            else:
                url = _SESSION_ROOT_URL if parent is None else _SESSION_PARENT_URL
                item = await _put_in_session(client, source, url, params, cfg.graph_timeout_seconds)
        except ODataError as exc:
            if _status(exc) == 409:
                raise _exists(source.name) from exc
            raise
    return {"item": item, "bytes": source.size}


def _cancel_session(http: httpx.Client, upload_url: str) -> None:
    with contextlib.suppress(httpx.HTTPError):
        http.delete(upload_url)


def _check_onedrive_name(name: str) -> None:
    """OneDrive refuses some names Google accepts; say so before anything is sent."""
    lowered = name.lower()
    if (
        any(char in name for char in '"*:<>?/\\|')
        or name.endswith(".")
        or name.split(".", 1)[0].upper() in _ONEDRIVE_DEVICE_NAMES
        or lowered in {".lock", "desktop.ini"}
        or lowered.startswith("~$")
        or "_vti_" in lowered
    ):
        raise DriveUploadError(
            f'OneDrive cannot hold the name {name!r}: it refuses names with " * : < > ? / \\ |, '
            "a trailing dot, device names (CON, NUL, COM1...), .lock, desktop.ini, ~$ prefixes "
            "and _vti_"
        )


def _check_session_url(url: str) -> None:
    """The service's session URL must be a public https address (never a local one)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    local = host == "localhost"
    try:
        ipaddress.ip_address(host)
        local = True
    except ValueError:
        pass
    if parts.scheme != "https" or not host or local:
        raise DriveUploadError("expected a public https upload session URL from the service")


def _encoded_path(segments: list[str]) -> str:
    """Percent-encode each segment (so ``#`` / ``?`` cannot truncate) but keep ``/``."""
    return "/".join(quote(segment, safe="") for segment in segments)


def _entry_sort(order_key: str) -> Any:
    if order_key == "name":
        return lambda item: (item.name or "").casefold()
    return lambda item: (
        item.last_modified_date_time.isoformat() if item.last_modified_date_time else ""
    )


def _exists(name: str) -> DriveUploadError:
    return DriveUploadError(
        f"a file named {name!r} already exists in that folder - pick another --name or folder "
        "(upload never replaces or duplicates)"
    )


def _finished_item(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise DriveUploadError("expected a JSON answer from the service, got another") from exc
    if not isinstance(data, dict):
        raise DriveUploadError("expected a JSON object from the service, got another")
    return _item_from_json(data)


def _maybe_done(name: str) -> DriveUploadError:
    return DriveUploadError(
        f"the service answered that {name!r} exists after a retried chunk: the upload may have "
        "completed - check the folder before trying again"
    )


def _item_from_json(data: dict[str, Any]) -> dict[str, Any]:
    """The stable item shape for the DriveItem JSON an upload session ends with."""
    item = DriveItem()
    item.id = data.get("id")
    item.name = data.get("name")
    item.size = data.get("size")
    item.web_url = data.get("webUrl")
    mime = (data.get("file") or {}).get("mimeType")
    if mime:
        item.file = File()
        item.file.mime_type = mime
    parent = (data.get("parentReference") or {}).get("id")
    if parent:
        item.parent_reference = ItemReference()
        item.parent_reference.id = parent
    stamp = data.get("lastModifiedDateTime")
    if isinstance(stamp, str):
        with contextlib.suppress(ValueError):
            item.last_modified_date_time = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return _to_item(item)


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


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _require_folder(item: DriveItem | None, label: str) -> str:
    if item is None or item.folder is None or not item.id:
        raise DriveFolderNotFoundError(f"no folder at {label!r}")
    return item.id


def _send_chunk(
    http: httpx.Client, upload_url: str, chunk: bytes, offset: int, end: int, total: int
) -> tuple[httpx.Response, bool]:
    """PUT one chunk; retry a throttled or failing service a few times (Retry-After honoured).

    Returns the answer and whether a retry happened: after one, the service may already hold
    the chunk, so a 416 or 409 does not mean what it would on a first try.
    """
    headers = {"Content-Range": f"bytes {offset}-{end}/{total}"}
    retried = False
    for attempt in range(_UPLOAD_RETRIES + 1):
        last = attempt == _UPLOAD_RETRIES
        try:
            response = http.put(upload_url, content=chunk, headers=headers)
        except httpx.TransportError as exc:
            if last:
                raise DriveUploadError(
                    f"expected the upload to continue, got {type(exc).__name__}"
                ) from exc
            _pause(2.0**attempt)
            retried = True
            continue
        if response.status_code in _RETRY_STATUSES and not last:
            _pause(_retry_delay(response, attempt))
            retried = True
            continue
        return response, retried
    raise AssertionError("unreachable")  # pragma: no cover


def _next_offset(http: httpx.Client, upload_url: str) -> int | None:
    """Where the session says the next byte should go, or None when it cannot say."""
    try:
        response = http.get(upload_url)
        ranges = response.json().get("nextExpectedRanges") if response.status_code == 200 else None
        return int(str(ranges[0]).split("-", 1)[0]) if ranges else None
    except httpx.HTTPError, ValueError, AttributeError, IndexError, TypeError:
        return None


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying: the service's Retry-After when it sent a number."""
    try:
        wait = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return 2.0**attempt
    return min(max(wait, 0.0), 60.0) if math.isfinite(wait) else 2.0**attempt


def _session_client(timeout: float = 60.0) -> httpx.Client:
    """The HTTP client for an upload session's pre-authorized URL (no token is ever sent).

    It honours the proxy and certificate environment, like the Graph client does.
    """
    return httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False)


def _upload_chunks(upload_url: str, source: UploadSource, timeout: float = 60.0) -> dict[str, Any]:
    """Send the file to an upload session in aligned chunks; cancel the session on any failure."""
    _check_session_url(upload_url)
    with _session_client(timeout) as http:
        try:
            source.handle.seek(0)
            offset = 0
            while True:
                chunk = source.handle.read(_UPLOAD_CHUNK)
                if not chunk:
                    raise DriveUploadError(
                        "expected the upload to finish with the last chunk, got no answer"
                    )
                source.ensure_unchanged()
                end = offset + len(chunk) - 1
                response, retried = _send_chunk(http, upload_url, chunk, offset, end, source.size)
                status = response.status_code
                if status in (200, 201):
                    item = _finished_item(response)
                    _ensure_unchanged_after(source, item)
                    return item
                if status == 416 and retried:
                    resume = _next_offset(http, upload_url)
                    if resume is None or resume <= offset:
                        raise DriveUploadError(
                            "expected the upload to resume where the service says, got no answer"
                        )
                    source.handle.seek(resume)
                    offset = resume
                    continue
                if status == 409:
                    raise _maybe_done(source.name) if retried else _exists(source.name)
                if status != 202:
                    raise DriveUploadError(f"expected the upload to succeed, got HTTP {status}")
                offset = end + 1
        except BaseException:
            _cancel_session(http, upload_url)
            raise


def _ensure_unchanged_after(source: UploadSource, item: dict[str, Any]) -> None:
    """Check the file once the copy exists: if it changed, name the copy that was left."""
    try:
        source.ensure_unchanged()
    except DriveUploadError as exc:
        raise DriveUploadError(
            f"{exc}; the copy already created (id {item.get('id')}) was left in the drive"
        ) from exc


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


async def _put_in_session(
    client: Any, source: UploadSource, url: str, params: dict[str, str], timeout: float
) -> dict[str, Any]:
    """Create an upload session (failing on a name clash) and send the file through it."""
    info = RequestInformation(Method.POST, url, params)
    body = {"item": {"@microsoft.graph.conflictBehavior": "fail", "name": source.name}}
    info.set_stream_content(json.dumps(body).encode(), "application/json")
    session = await client.request_adapter.send_async(info, UploadSession, _ERROR_MAP)
    upload_url = getattr(session, "upload_url", None)
    if not upload_url:
        raise DriveUploadError("expected an upload session from the service, got none")
    # Blocking HTTP and retry waits: off the event loop, so a large upload does not stall it.
    return await asyncio.to_thread(_upload_chunks, upload_url, source, timeout)


async def _put_small(
    client: Any, data: bytes, mime_type: str, url: str, params: dict[str, str]
) -> DriveItem:
    """One PUT of a small file; a name clash is a 409 (conflictBehavior=fail)."""
    info = RequestInformation(Method.PUT, url, params)
    info.set_stream_content(data, mime_type)
    item = await client.request_adapter.send_async(info, DriveItem, _ERROR_MAP)
    if item is None:
        raise RuntimeError("Graph returned no DriveItem for the uploaded file")
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
