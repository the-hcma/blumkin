"""One lock for every read-modify-write of config.toml (issue #545 review)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from blumkin.config_lock import config_file_lock

from blumkin.browser.init_task import init_task
from blumkin.config import set_profile_email, set_profile_fields

_BASE = '[profiles.default]\nclient_id = "abc-123"\n'


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(_BASE, encoding="utf-8")
    return path


def _blocks_until_released(config_path: Path, action: Callable[[], object]) -> None:
    """`action` must wait for the lock; once it is released, `action` finishes."""
    finished = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            action()
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)
        finally:
            finished.set()

    with config_file_lock(config_path):
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert not finished.wait(0.4), "the writer did not wait for the lock"
    assert finished.wait(10), "the writer never finished after the lock was released"
    assert errors == []


def test_the_lock_is_a_private_sidecar_file_next_to_config_toml(config_path: Path) -> None:
    with config_file_lock(config_path):
        siblings = {path.name for path in config_path.parent.iterdir()}
    assert ".config.toml.lock" in siblings
    assert config_path.read_text(encoding="utf-8") == _BASE


def test_set_profile_fields_waits_for_the_lock(config_path: Path) -> None:
    _blocks_until_released(
        config_path,
        lambda: set_profile_fields(config_path, profile="default", fields={"provider": "google"}),
    )
    assert 'provider = "google"' in config_path.read_text(encoding="utf-8")


def test_set_profile_email_waits_for_the_lock(config_path: Path) -> None:
    _blocks_until_released(
        config_path,
        lambda: set_profile_email(config_path, profile="default", email="me@example.com"),
    )
    assert "me@example.com" in config_path.read_text(encoding="utf-8")


def test_init_task_waits_for_the_lock(config_path: Path) -> None:
    _blocks_until_released(
        config_path,
        lambda: init_task(config_path.parent, "teams-transcript", "teams-transcript", write=True),
    )
    assert "[browser.tasks.teams-transcript]" in config_path.read_text(encoding="utf-8")
