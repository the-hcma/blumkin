"""Logout must clear in-memory MSAL cache so atexit cannot restore secrets."""

from __future__ import annotations

import json
from pathlib import Path

from blumkin import auth
from blumkin.auth import logout, save_token_cache
from blumkin.config import load_config


def test_logout_clears_bound_cache_so_atexit_cannot_rewrite(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("BLUMKIN_CLIENT_ID", "test-client")
    cache_path = tmp_path / "msal_token_cache.json"
    record_path = tmp_path / "auth_record.json"
    cache_path.write_text(json.dumps({"RefreshToken": {"r1": {"secret": "x"}}}))
    record_path.write_text("{}")
    (tmp_path / "config.toml").write_text('client_id = "test-client"\n')

    cfg = load_config()
    auth.reload_token_cache_from_disk(cfg)
    assert auth._cache_bound_path == str(cache_path)

    logout(cfg)
    assert not cache_path.is_file()
    assert not record_path.is_file()
    assert auth._cache_bound_path is None
    assert auth._token_cache.has_state_changed is False

    # Even if something marks the empty cache dirty later, bound path is cleared.
    auth._token_cache.has_state_changed = True
    auth._save_bound_token_cache_at_exit()
    assert not cache_path.is_file()
    save_token_cache(cfg)
    assert not cache_path.is_file()
