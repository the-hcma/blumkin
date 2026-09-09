"""The local record of documents `docs create` made, used to gate `docs update`."""

from __future__ import annotations

import json
from pathlib import Path

from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.created_docs import (
    _MAX_TRACKED_IDS,
    is_blumkin_created_doc,
    record_created_doc,
)
from blumkin.providers.kind import ProviderKind


def _cfg(tmp_path: Path) -> BlumkinConfig:
    return BlumkinConfig(
        client_id="x",
        config_dir=tmp_path,
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=None,
        graph_timeout_seconds=60.0,
        legacy_flat=True,
        mail_signature=MailSignatureConfig(),
        profile="default",
        provider=ProviderKind.MICROSOFT,
        tags=(),
        tenant_id="t",
        wo1162425_scopes=False,
    )


def test_unknown_id_is_not_recorded(tmp_path: Path) -> None:
    assert is_blumkin_created_doc(_cfg(tmp_path), "doc-1") is False


def test_record_then_recognise(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    record_created_doc(cfg, "doc-1")
    record_created_doc(cfg, "doc-2")
    assert is_blumkin_created_doc(cfg, "doc-1")
    assert is_blumkin_created_doc(cfg, "doc-2")
    assert not is_blumkin_created_doc(cfg, "doc-3")


def test_record_is_deduped(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    record_created_doc(cfg, "doc-1")
    record_created_doc(cfg, "doc-1")
    assert json.loads(cfg.created_docs_path.read_text()) == ["doc-1"]


def test_empty_id_is_a_noop(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    record_created_doc(cfg, "")
    assert not cfg.created_docs_path.is_file()


def test_corrupt_record_reads_as_empty(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.created_docs_path.write_text("}{ not json")
    assert is_blumkin_created_doc(cfg, "doc-1") is False
    # A subsequent record overwrites the junk cleanly.
    record_created_doc(cfg, "doc-1")
    assert is_blumkin_created_doc(cfg, "doc-1")


def test_record_is_capped(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.created_docs_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.created_docs_path.write_text(json.dumps([f"d{i}" for i in range(_MAX_TRACKED_IDS)]))
    record_created_doc(cfg, "newest")
    ids = json.loads(cfg.created_docs_path.read_text())
    assert len(ids) == _MAX_TRACKED_IDS
    assert ids[-1] == "newest"
    assert "d0" not in ids  # oldest dropped
