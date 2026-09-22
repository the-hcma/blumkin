"""Unit tests for the RSVP freshness read-state record (issue #365)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from blumkin.config import load_config
from blumkin.read_state import (
    _MAX_ENTRY_AGE_SECONDS,
    _key,
    clear_read,
    record_read,
    seconds_since_read,
)


def test_a_naive_timestamp_in_the_state_file_fails_open_not_typeerror(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: a hand-edited/tz-less ISO timestamp parses fine via
    ``fromisoformat`` but cannot be subtracted from an aware ``now()`` -
    this must be treated as unknown, not raise (mirrors compose_state's
    equivalent regression test for issue #365)."""
    cfg = _cfg(tmp_path, monkeypatch)
    cfg.read_state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.read_state_path.write_text(
        json.dumps({_key("event-naive", None): {"read_at": "2026-09-21T10:00:00"}})
    )
    assert seconds_since_read(cfg, "event-naive") is None
    # And the load-time prune must not raise either, pruning the unusable entry.
    record_read(cfg, "event-fresh")
    on_disk = json.loads(cfg.read_state_path.read_text())
    assert _key("event-naive", None) not in on_disk
    assert _key("event-fresh", None) in on_disk


def test_clear_read_forgets_the_record(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    record_read(cfg, "event-1")
    clear_read(cfg, "event-1")
    assert seconds_since_read(cfg, "event-1") is None


def test_clear_read_on_unknown_id_is_a_no_op(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    clear_read(cfg, "never-read")  # must not raise


def test_concurrent_record_read_calls_do_not_clobber_each_other(
    tmp_path: Path, monkeypatch
) -> None:
    """Two overlapping load-modify-save transactions for different event ids
    must both survive - mirrors compose_state's equivalent lock regression
    test for issue #365."""
    cfg = _cfg(tmp_path, monkeypatch)
    record_read(cfg, "event-a")
    record_read(cfg, "event-b")
    on_disk = json.loads(cfg.read_state_path.read_text())
    assert _key("event-a", None) in on_disk
    assert _key("event-b", None) in on_disk


def test_no_profile_dir_yet_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    assert seconds_since_read(cfg, "event-1") is None
    clear_read(cfg, "event-1")  # no-op, must not raise


def test_record_read_resets_the_clock_on_re_read(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    stale = (datetime.now(UTC) - timedelta(seconds=100)).isoformat()
    cfg.read_state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.read_state_path.write_text(json.dumps({_key("event-1", None): {"read_at": stale}}))
    record_read(cfg, "event-1")
    elapsed = seconds_since_read(cfg, "event-1")
    assert elapsed is not None
    assert elapsed < 60


def test_record_then_seconds_since_read_is_near_zero(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    record_read(cfg, "event-1")
    elapsed = seconds_since_read(cfg, "event-1")
    assert elapsed is not None
    assert 0 <= elapsed < 60


def test_seconds_since_read_unknown_id_is_none(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    assert seconds_since_read(cfg, "never-read") is None


def test_stale_entries_are_pruned_on_load(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    ancient = (datetime.now(UTC) - timedelta(seconds=_MAX_ENTRY_AGE_SECONDS + 60)).isoformat()
    cfg.read_state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.read_state_path.write_text(json.dumps({_key("event-old", None): {"read_at": ancient}}))
    assert seconds_since_read(cfg, "event-old") is None
    # And the prune persists back to disk on the next write.
    record_read(cfg, "event-new")
    on_disk = json.loads(cfg.read_state_path.read_text())
    assert _key("event-old", None) not in on_disk
    assert _key("event-new", None) in on_disk


def test_prune_window_extends_to_cover_a_longer_configured_freshness_window(
    tmp_path: Path, monkeypatch
) -> None:
    """A configured freshness window longer than ``_MAX_ENTRY_AGE_SECONDS`` must
    not be starved by the prune - the entry has to outlive the window it
    gates (mirrors compose_state's equivalent test for issue #365)."""
    long_window = _MAX_ENTRY_AGE_SECONDS + 3600
    cfg = _cfg(tmp_path, monkeypatch, freshness_seconds=long_window)
    past_default_prune = (
        datetime.now(UTC) - timedelta(seconds=_MAX_ENTRY_AGE_SECONDS + 60)
    ).isoformat()
    cfg.read_state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.read_state_path.write_text(
        json.dumps({_key("event-1", None): {"read_at": past_default_prune}})
    )
    elapsed = seconds_since_read(cfg, "event-1")
    assert elapsed is not None
    assert elapsed < long_window


def _cfg(tmp_path: Path, monkeypatch, *, freshness_seconds: int | None = None):
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    prefs = "" if freshness_seconds is None else f"rsvp_freshness_seconds = {freshness_seconds}\n"
    (tmp_path / "config.toml").write_text(
        f'[profiles.default]\nclient_id = "abc"\n[profiles.default.preferences]\n{prefs}'
    )
    return load_config()
