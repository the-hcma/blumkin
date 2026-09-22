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


def test_clear_composed_forgets_the_record(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    record_composed(cfg, "draft-1")
    clear_composed(cfg, "draft-1")
    assert seconds_since_composed(cfg, "draft-1") is None


def test_concurrent_record_composed_calls_do_not_clobber_each_other(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: two overlapping load-modify-save transactions for different
    artifact ids must both survive - the flock'd _locked_update transaction
    prevents the second writer's save from discarding the first's entry
    (see issue #365 review)."""
    cfg = _cfg(tmp_path, monkeypatch)
    record_composed(cfg, "draft-a")
    record_composed(cfg, "draft-b")
    on_disk = json.loads(cfg.compose_state_path.read_text())
    assert "draft-a" in on_disk
    assert "draft-b" in on_disk


def test_no_profile_dir_yet_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    assert seconds_since_composed(cfg, "draft-1") is None
    clear_composed(cfg, "draft-1")  # no-op, must not raise


def test_now_iso_preserves_sub_second_precision(monkeypatch) -> None:
    """Regression: truncating to whole seconds could let a draft composed at
    T.999 pass a cooldown nearly a full second early (see issue #365 review)."""
    from blumkin import compose_state

    fixed = datetime(2026, 1, 1, 12, 0, 0, 999_000, tzinfo=UTC)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(compose_state, "datetime", _FixedDatetime)
    assert compose_state._now_iso() == fixed.isoformat()


def test_prune_window_extends_to_cover_a_longer_configured_cooldown(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: a cooldown longer than _MAX_ENTRY_AGE_SECONDS must not be
    starved by the prune - the entry has to outlive the cooldown it gates
    (see issue #365 review)."""
    long_cooldown = _MAX_ENTRY_AGE_SECONDS + 3600
    cfg = _cfg(tmp_path, monkeypatch, cooldown_seconds=long_cooldown)
    past_default_prune = (
        datetime.now(UTC) - timedelta(seconds=_MAX_ENTRY_AGE_SECONDS + 60)
    ).isoformat()
    cfg.compose_state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.compose_state_path.write_text(json.dumps({"draft-1": past_default_prune}))
    elapsed = seconds_since_composed(cfg, "draft-1")
    assert elapsed is not None
    assert elapsed < long_cooldown


def test_record_composed_resets_the_clock_on_re_compose(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    stale = (datetime.now(UTC) - timedelta(seconds=100)).isoformat()
    cfg.compose_state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.compose_state_path.write_text(json.dumps({"draft-1": stale}))
    record_composed(cfg, "draft-1")
    elapsed = seconds_since_composed(cfg, "draft-1")
    assert elapsed is not None
    assert elapsed < 60


def test_record_then_seconds_since_composed_is_near_zero(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    record_composed(cfg, "draft-1")
    elapsed = seconds_since_composed(cfg, "draft-1")
    assert elapsed is not None
    assert 0 <= elapsed < 60


def test_seconds_since_composed_unknown_id_is_none(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch)
    assert seconds_since_composed(cfg, "never-composed") is None


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


def _cfg(tmp_path: Path, monkeypatch, *, cooldown_seconds: int | None = None):
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    prefs = "" if cooldown_seconds is None else f"confirm_cooldown_seconds = {cooldown_seconds}\n"
    (tmp_path / "config.toml").write_text(
        f'[profiles.default]\nclient_id = "abc"\n[profiles.default.preferences]\n{prefs}'
    )
    return load_config()
