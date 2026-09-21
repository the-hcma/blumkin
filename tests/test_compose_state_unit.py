"""Unit tests for the compose-state cooldown record (issue #365)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from blumkin.compose_state import (
    _MAX_ENTRY_AGE_SECONDS,
    clear_composed,
    record_composed,
    seconds_since_composed,
)
from blumkin.config import load_config


def _cfg(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.default]\nclient_id = "abc"\n')
    return load_config()


def test_record_then_seconds_since_composed_is_near_zero(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    record_composed(cfg, "draft-1")
    elapsed = seconds_since_composed(cfg, "draft-1")
    assert elapsed is not None
    assert 0 <= elapsed < 2


def test_seconds_since_composed_unknown_id_is_none(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    assert seconds_since_composed(cfg, "never-composed") is None


def test_clear_composed_forgets_the_record(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    record_composed(cfg, "draft-1")
    clear_composed(cfg, "draft-1")
    assert seconds_since_composed(cfg, "draft-1") is None


def test_record_composed_resets_the_clock_on_re_compose(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    stale = (datetime.now(UTC) - timedelta(seconds=100)).isoformat()
    cfg.compose_state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.compose_state_path.write_text(json.dumps({"draft-1": stale}))
    record_composed(cfg, "draft-1")
    elapsed = seconds_since_composed(cfg, "draft-1")
    assert elapsed is not None
    assert elapsed < 2


def test_stale_entries_are_pruned_on_load(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    ancient = (datetime.now(UTC) - timedelta(seconds=_MAX_ENTRY_AGE_SECONDS + 60)).isoformat()
    cfg.compose_state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.compose_state_path.write_text(json.dumps({"draft-old": ancient}))
    assert seconds_since_composed(cfg, "draft-old") is None
    # And the prune persists back to disk on the next write.
    record_composed(cfg, "draft-new")
    on_disk = json.loads(cfg.compose_state_path.read_text())
    assert "draft-old" not in on_disk
    assert "draft-new" in on_disk


def test_no_profile_dir_yet_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    assert seconds_since_composed(cfg, "draft-1") is None
    clear_composed(cfg, "draft-1")  # no-op, must not raise


def test_a_naive_timestamp_in_the_state_file_fails_open_not_typeerror(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: a hand-edited/tz-less ISO timestamp parses fine via
    ``fromisoformat`` but cannot be subtracted from an aware ``now()`` -
    this must be treated as unknown, not raise (see issue #365 review)."""
    cfg = _cfg(tmp_path, monkeypatch)
    cfg.compose_state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.compose_state_path.write_text(json.dumps({"draft-naive": "2026-09-21T10:00:00"}))
    assert seconds_since_composed(cfg, "draft-naive") is None
    # And the load-time prune must not raise either, pruning the unusable entry.
    record_composed(cfg, "draft-fresh")
    on_disk = json.loads(cfg.compose_state_path.read_text())
    assert "draft-naive" not in on_disk
    assert "draft-fresh" in on_disk
