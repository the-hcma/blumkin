"""Local record of documents this blumkin install created, so ``docs update`` fails closed.

``docs update`` overwrites a document's whole body / renames it. The Graph
``Files.ReadWrite`` scope and the Google ``documents`` scope are both user-wide,
so an ``--id`` alone is not proof the target is one of blumkin's own documents -
without a check, ``docs update --id <any file the user can write>`` would silently
destroy it.

``docs create`` appends the new id here; ``docs update`` refuses an id that is
not recorded (``not_found`` / exit 5). The file sits next to the token cache
under ``~/.config/blumkin/`` (never committed) and is per-profile, so it is a
per-machine record: a document created on another machine, or before this
version, is not updatable through blumkin (open it in the browser instead). The
Google backend additionally accepts an id the narrow ``drive.file`` grant can
still see, which covers the cross-machine case there.
"""

from __future__ import annotations

import json

from blumkin.config import BlumkinConfig

# A defensive cap: the list is one short id-string per `docs create`, so this is
# generous, but it stops an unbounded file if something loops on create.
_MAX_TRACKED_IDS = 10_000


def is_blumkin_created_doc(config: BlumkinConfig, doc_id: str) -> bool:
    """True when ``doc_id`` was recorded by a ``docs create`` on this profile."""
    return doc_id in _load_ids(config)


def record_created_doc(config: BlumkinConfig, doc_id: str) -> None:
    """Append ``doc_id`` to this profile's created-docs record (deduped, newest last)."""
    if not doc_id:
        return
    path = config.created_docs_path
    if not path.parent.is_dir():
        # No profile dir yet means no auth cache, so `docs create` could not have
        # reached here in a real run - and we must not scatter dirs on a bad path.
        return
    ids = [existing for existing in _load_ids(config) if existing != doc_id]
    ids.append(doc_id)
    del ids[:-_MAX_TRACKED_IDS]
    path.write_text(json.dumps(ids, indent=2) + "\n")


def _load_ids(config: BlumkinConfig) -> list[str]:
    try:
        raw = json.loads(config.created_docs_path.read_text())
    except OSError, ValueError:
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]
