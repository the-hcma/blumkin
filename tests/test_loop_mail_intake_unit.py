"""Loop attachment intake directories (issue #503 / #504)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from blumkin.attachments import sanitize_attachment_filename
from blumkin.loop.mail_intake import intake_dir


def test_intake_dir_uses_a_hash_so_sanitized_ids_do_not_collide(tmp_path: Path) -> None:
    # Two Graph-like ids that sanitize to the same filename under the old key.
    left = "AAMkAGI+/x=="
    right = "AAMkAGI_/x=="
    assert sanitize_attachment_filename(left) == sanitize_attachment_filename(right)
    a = intake_dir(tmp_path, trigger="receipts", message_id=left)
    b = intake_dir(tmp_path, trigger="receipts", message_id=right)
    assert a != b
    assert a.name == hashlib.sha256(left.encode()).hexdigest()
    assert b.name == hashlib.sha256(right.encode()).hexdigest()


def test_intake_dir_reuses_a_legacy_sanitized_folder(tmp_path: Path) -> None:
    message_id = "simple-id"
    legacy = tmp_path / "loop" / "intake" / "receipts" / sanitize_attachment_filename(message_id)
    legacy.mkdir(parents=True)
    (legacy / "a.pdf").write_bytes(b"x")
    path = intake_dir(tmp_path, trigger="receipts", message_id=message_id)
    assert path == legacy
    assert (path / "a.pdf").is_file()
