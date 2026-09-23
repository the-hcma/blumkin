"""CLI tests for `blumkin auth set-app-secret` (issue #368)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from blumkin import secret_store
from blumkin.app_secrets import read_app_secret
from blumkin.cli import main
from blumkin.config import load_config
from blumkin.exit_codes import EXIT_OTHER, EXIT_SUCCESS, EXIT_USAGE


class _FakeKeyring:
    """Minimal in-memory stand-in for the ``keyring`` module's module-level API."""

    class errors:
        class PasswordDeleteError(Exception):
            pass

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise self.errors.PasswordDeleteError("not found")
        del self.store[(service, username)]

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password


def _configure(tmp_path: Path, monkeypatch, *, token_storage: str = "auto") -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        f'[profiles.default]\nclient_id = "abc"\ntoken_storage = "{token_storage}"\n'
    )


def test_set_app_secret_from_stdin(tmp_path: Path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    result = CliRunner().invoke(
        main,
        ["auth", "set-app-secret", "--kind", "ms_client_id", "--stdin"],
        input="vaulted-client-id\n",
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert read_app_secret(load_config(), "ms_client_id") == "vaulted-client-id"


def test_set_app_secret_from_file_extracts_google_desktop_json_secret(
    tmp_path: Path, monkeypatch
) -> None:
    _configure(tmp_path, monkeypatch)
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    oauth_file = tmp_path / "desktop-client.json"
    oauth_file.write_text(
        json.dumps({"installed": {"client_id": "file-client-id", "client_secret": "file-secret"}})
    )
    result = CliRunner().invoke(
        main,
        [
            "auth",
            "set-app-secret",
            "--kind",
            "google_client_secret",
            "--from-file",
            str(oauth_file),
        ],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert read_app_secret(load_config(), "google_client_secret") == "file-secret"


def test_set_app_secret_from_file_uses_raw_text_for_ms_client_id(
    tmp_path: Path, monkeypatch
) -> None:
    _configure(tmp_path, monkeypatch)
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    raw_file = tmp_path / "client-id.txt"
    raw_file.write_text("  raw-client-id  \n")
    result = CliRunner().invoke(
        main,
        ["auth", "set-app-secret", "--kind", "ms_client_id", "--from-file", str(raw_file)],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert read_app_secret(load_config(), "ms_client_id") == "raw-client-id"


def test_set_app_secret_delete_removes_vaulted_value(tmp_path: Path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    CliRunner().invoke(
        main,
        ["auth", "set-app-secret", "--kind", "ms_client_id", "--stdin"],
        input="vaulted-client-id\n",
    )
    result = CliRunner().invoke(
        main, ["auth", "set-app-secret", "--kind", "ms_client_id", "--delete"]
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert read_app_secret(load_config(), "ms_client_id") is None


def test_set_app_secret_delete_rejects_from_file(tmp_path: Path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _FakeKeyring())
    result = CliRunner().invoke(
        main,
        [
            "auth",
            "set-app-secret",
            "--kind",
            "ms_client_id",
            "--delete",
            "--from-file",
            str(tmp_path / "config.toml"),
        ],
    )
    assert result.exit_code == EXIT_USAGE
    assert "cannot be combined" in result.output


def test_set_app_secret_raises_when_no_usable_backend(tmp_path: Path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    result = CliRunner().invoke(
        main,
        ["auth", "set-app-secret", "--kind", "ms_client_id", "--stdin"],
        input="value\n",
    )
    assert result.exit_code == EXIT_OTHER
    assert "no usable OS keychain" in result.output


def test_set_app_secret_rejects_token_storage_file(tmp_path: Path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch, token_storage="file")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _FakeKeyring())
    result = CliRunner().invoke(
        main,
        ["auth", "set-app-secret", "--kind", "ms_client_id", "--stdin"],
        input="value\n",
    )
    assert result.exit_code == EXIT_OTHER
    assert 'token_storage = "file"' in result.output
