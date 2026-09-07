"""`drive` shared layer: provider-neutral helpers, errors, and human formatters.

The Google Drive and Microsoft OneDrive backends both return the same stable
item shape so ``--json`` consumers do not branch on the provider::

    {id, name, mime_type, kind, size, modified, web_url, parent_id, provider}

``kind`` collapses the provider's type vocabulary to
``file | folder | doc | sheet | slides``.
"""

from __future__ import annotations

from typing import Any, Literal

# Every `drive.*` skill id. The dispatch layer gates the whole set on the
# Microsoft `docs_scopes` toggle (Files.ReadWrite) - the same grant `docs create`
# already needs; Google carries `drive` in its standard scope set.
DRIVE_SKILLS: frozenset[str] = frozenset(
    {
        "drive.get",
        "drive.list",
    }
)

DriveKind = Literal["file", "folder", "doc", "sheet", "slides"]

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


def format_drive_list_human(payload: dict[str, Any]) -> list[str]:
    items = payload.get("items") or []
    if not items:
        return ["(no items)"]
    lines = [f"{len(items)} item(s):"]
    for item in items:
        size = "" if item.get("size") is None else f"  {item['size']}b"
        lines.append(f"  [{item.get('kind'):>6}] {item.get('name')}{size}  id={item.get('id')}")
    return lines


def normalize_order(order: str | None) -> str:
    value = (order or "modified").strip().lower()
    if value not in _ORDERS:
        raise DriveSelectorError(f"--order must be one of {', '.join(_ORDERS)} (got {order!r})")
    return value


def split_path(path: str) -> list[str]:
    """Split a drive path into its non-empty segments (``/A//B/`` -> ``['A', 'B']``)."""
    return [segment.strip() for segment in path.split("/") if segment.strip()]


def validate_folder_selector(folder: str | None, folder_id: str | None) -> None:
    """`drive list` takes at most one of ``--folder`` / ``--folder-id``."""
    if folder is not None and folder_id is not None:
        raise DriveSelectorError("pass at most one of --folder or --folder-id, not both")
