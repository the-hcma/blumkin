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


def test_set_app_secret_refuses_non_tty_stdin_without_explicit_stdin_flag(
    tmp_path: Path, monkeypatch
) -> None:
    """A non-interactive invocation (e.g. an agent's inherited pipe) must
    never be silently read as the secret value unless `--stdin` is given
    explicitly - inferring stdin-vs-prompt purely from `isatty()` would let a
    script's unrelated piped input become the effective secret (issue #368
    review, round 2)."""
    _configure(tmp_path, monkeypatch)
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    result = CliRunner().invoke(
        main,
        ["auth", "set-app-secret", "--kind", "ms_client_id"],
        input="unrelated-piped-input\n",
    )
    assert result.exit_code == EXIT_USAGE, result.output
    assert "stdin is not a TTY" in result.output
    assert read_app_secret(load_config(), "ms_client_id") is None


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


def test_set_app_secret_from_file_rejects_desktop_json_without_client_secret(
    tmp_path: Path, monkeypatch
) -> None:
    """A Desktop client JSON with an `installed`/`web` object but no usable
    `client_secret` must be rejected, not vaulted whole as raw text - the
    latter would silently shadow the working file value with garbage (e.g. a
    service-account key) until `--delete` is run (issue #368 review)."""
    _configure(tmp_path, monkeypatch)
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    oauth_file = tmp_path / "service-account.json"
    oauth_file.write_text(json.dumps({"installed": {"client_id": "file-client-id"}}))
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
    assert result.exit_code == EXIT_USAGE, result.output
    assert "no usable client_secret" in result.output
    assert read_app_secret(load_config(), "google_client_secret") is None


def test_set_app_secret_from_file_rejects_a_json_object_without_installed_or_web(
    tmp_path: Path, monkeypatch
) -> None:
    """A JSON object that is not a Desktop client at all - a Google
    service-account key is the concrete example, no `installed`/`web` key -
    must also be rejected, not vaulted whole as raw text (issue #368 review,
    round 2: the earlier fix only caught the `installed`-without-secret
    case, not this one)."""
    _configure(tmp_path, monkeypatch)
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    service_account_file = tmp_path / "service-account.json"
    service_account_file.write_text(
        json.dumps({"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----"})
    )
    result = CliRunner().invoke(
        main,
        [
            "auth",
            "set-app-secret",
            "--kind",
            "google_client_secret",
            "--from-file",
            str(service_account_file),
        ],
    )
    assert result.exit_code == EXIT_USAGE, result.output
    assert "no installed/web client object" in result.output
    assert read_app_secret(load_config(), "google_client_secret") is None


def test_set_app_secret_delete_reports_failure_when_backend_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    """`--delete` must not report success (and must emit the structured
    `secret_write_failed` error, matching the write path) when the keychain
    backend is unavailable - reporting success would let an existing vaulted
    entry silently survive undetected (issue #368 review)."""
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    result = CliRunner().invoke(
        main,
        ["auth", "set-app-secret", "--kind", "ms_client_id", "--delete"],
    )
    assert result.exit_code == EXIT_OTHER, result.output
    assert "no usable OS keychain" in result.output


def test_set_app_secret_delete_json_reports_secret_write_failed(
    tmp_path: Path, monkeypatch
) -> None:
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    result = CliRunner().invoke(
        main,
        ["auth", "set-app-secret", "--kind", "ms_client_id", "--delete", "--json"],
    )
    assert result.exit_code == EXIT_OTHER, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "secret_write_failed"
