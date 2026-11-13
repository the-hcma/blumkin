"""`client_id_available` resolves the Microsoft client id the way sign-in does (issue #735).

`auth status` reads `config.toml` only (one keychain touch); anything that must know whether a
profile can sign in, such as the live test gate, needs the vaulted id counted too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blumkin import app_secrets, secret_store
from blumkin.auth import client_id_available, status_dict
from blumkin.cli import _auth_status_next_steps
from blumkin.config import load_config


class _FakeKeyring:
    class errors:
        class PasswordDeleteError(Exception):
            pass

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password


def _profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, client_id: str = ""):
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    line = f'client_id = "{client_id}"\n' if client_id else ""
    (tmp_path / "config.toml").write_text(f'[profiles.default]\n{line}token_storage = "auto"\n')
    return load_config()


def test_a_vaulted_client_id_counts_though_config_toml_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _profile(tmp_path, monkeypatch)
    fake = _FakeKeyring()  # one store for the write and the read
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    app_secrets.write_app_secret(cfg, "ms_client_id", "11111111-1111-1111-1111-111111111111")

    assert client_id_available(cfg) is True
    assert status_dict(cfg)["client_id_configured"] is False  # status stays config-only


def test_no_client_id_anywhere_is_not_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _profile(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _FakeKeyring())

    assert client_id_available(cfg) is False


def test_a_client_id_in_config_toml_counts_without_touching_the_keychain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _profile(tmp_path, monkeypatch, client_id="from-config")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _FakeKeyring())

    assert client_id_available(cfg) is True


def test_the_status_next_step_says_it_did_not_look_in_the_keychain() -> None:
    steps = _auth_status_next_steps({"client_id_configured": False})

    assert steps[0].startswith("client_id missing in config.toml")
    assert "does not read the OS keychain" in steps[0] and "blumkin doctor" in steps[0]
    assert "blumkin auth setup" in steps[0]
