"""CLI tests for `blumkin auth setup` (issue #368 guided setup)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from blumkin import cli as cli_module
from blumkin import secret_store
from blumkin.app_secrets import read_app_secret
from blumkin.cli import main
from blumkin.config import load_config
from blumkin.exit_codes import EXIT_OTHER, EXIT_SUCCESS, EXIT_USAGE


class _FakeKeyring:
    """Minimal in-memory stand-in for the ``keyring`` module's module-level API."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password


def _configure(tmp_path: Path, monkeypatch, *, provider: str) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(f'[profiles.default]\nprovider = "{provider}"\n')


def test_auth_setup_google_from_file_non_interactive(tmp_path: Path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch, provider="google")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    oauth_file = tmp_path / "desktop-client.json"
    oauth_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "abc.apps.googleusercontent.com",
                    "client_secret": "GOCSPX-test-secret",
                }
            }
        )
    )
    result = CliRunner().invoke(
        main, ["auth", "setup", "--yes", "--from-file", str(oauth_file), "--json"]
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert read_app_secret(load_config(), "google_client_secret") == "GOCSPX-test-secret"
    assert load_config().client_id == "abc.apps.googleusercontent.com"
    assert "GOCSPX-test-secret" not in (tmp_path / "config.toml").read_text()
    assert json.loads(result.output)["ok"] is True


def test_auth_setup_google_from_file_rejects_mismatched_client_id_flag(
    tmp_path: Path, monkeypatch
) -> None:
    """`--client-id` disagreeing with the file's id must not silently pair a
    different app's id with this file's client_secret (issue #368 review)."""
    _configure(tmp_path, monkeypatch, provider="google")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    oauth_file = tmp_path / "desktop-client.json"
    oauth_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "abc.apps.googleusercontent.com",
                    "client_secret": "GOCSPX-test-secret",
                }
            }
        )
    )
    result = CliRunner().invoke(
        main,
        [
            "auth",
            "setup",
            "--yes",
            "--from-file",
            str(oauth_file),
            "--client-id",
            "other.apps.googleusercontent.com",
        ],
    )
    assert result.exit_code == EXIT_USAGE, result.output
    assert "does not match the client_id" in result.output
    assert read_app_secret(load_config(), "google_client_secret") is None


def test_auth_setup_microsoft_non_interactive(tmp_path: Path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch, provider="microsoft")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    result = CliRunner().invoke(
        main,
        [
            "auth",
            "setup",
            "--yes",
            "--client-id",
            "12345678-1234-1234-1234-123456789012",
            "--tenant-id",
            "contoso.onmicrosoft.com",
            "--account-type",
            "organizational",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert read_app_secret(load_config(), "ms_client_id") == "12345678-1234-1234-1234-123456789012"
    written = load_config()
    assert written.tenant_id == "contoso.onmicrosoft.com"
    assert written.account_type == "organizational"
    assert written.client_id == "12345678-1234-1234-1234-123456789012"


def test_auth_setup_microsoft_succeeds_when_keychain_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    """Unlike Google's client_secret, Microsoft's client_id is also written to
    config.toml, so an unusable keychain must not fail the whole command -
    the profile is already fully functional via the toml copy alone
    (issue #368 review)."""
    _configure(tmp_path, monkeypatch, provider="microsoft")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    result = CliRunner().invoke(
        main,
        [
            "auth",
            "setup",
            "--yes",
            "--client-id",
            "12345678-1234-1234-1234-123456789012",
            "--tenant-id",
            "contoso.onmicrosoft.com",
            "--account-type",
            "organizational",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["vaulted"] is False
    written = load_config()
    assert written.client_id == "12345678-1234-1234-1234-123456789012"
    assert read_app_secret(written, "ms_client_id") is None


def test_auth_setup_microsoft_rejects_bad_client_id_without_writing(
    tmp_path: Path, monkeypatch
) -> None:
    _configure(tmp_path, monkeypatch, provider="microsoft")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    result = CliRunner().invoke(
        main,
        [
            "auth",
            "setup",
            "--yes",
            "--client-id",
            "not-a-guid",
            "--tenant-id",
            "contoso.onmicrosoft.com",
            "--account-type",
            "organizational",
        ],
    )
    assert result.exit_code == EXIT_USAGE, result.output
    assert read_app_secret(load_config(), "ms_client_id") is None


def test_auth_setup_requires_yes_without_a_tty(tmp_path: Path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch, provider="google")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    result = CliRunner().invoke(main, ["auth", "setup"])
    assert result.exit_code == EXIT_USAGE, result.output
    assert "needs a TTY" in result.output


def test_auth_setup_google_from_stdin_non_interactive(tmp_path: Path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch, provider="google")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    result = CliRunner().invoke(
        main,
        ["auth", "setup", "--yes", "--client-id", "abc.apps.googleusercontent.com", "--stdin"],
        input="GOCSPX-stdin-secret\n",
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert read_app_secret(load_config(), "google_client_secret") == "GOCSPX-stdin-secret"


def test_auth_setup_reports_secret_write_failed_when_backend_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    """config.toml is written first (issue #368 review), so a keychain
    write failure must still surface as `secret_write_failed`/`EXIT_OTHER`
    (matching `auth set-app-secret`) with `client_id` already persisted and
    no secret vaulted - not folded into the generic `usage_error` path."""
    _configure(tmp_path, monkeypatch, provider="google")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    result = CliRunner().invoke(
        main,
        ["auth", "setup", "--yes", "--client-id", "abc.apps.googleusercontent.com", "--stdin"],
        input="GOCSPX-stdin-secret\n",
    )
    assert result.exit_code == EXIT_OTHER, result.output
    assert "secret_write_failed" in result.output or "no usable OS keychain" in result.output
    assert load_config().client_id == "abc.apps.googleusercontent.com"
    assert read_app_secret(load_config(), "google_client_secret") is None


def test_auth_setup_google_non_interactive_refuses_inherited_pipe(
    tmp_path: Path, monkeypatch
) -> None:
    """Without ``--stdin``/``--from-file``, a piped (non-TTY) stdin must not be

    silently consumed as the secret - see ``_read_app_secret_value``'s
    docstring for why (issue #368 review finding).
    """
    _configure(tmp_path, monkeypatch, provider="google")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    result = CliRunner().invoke(
        main,
        ["auth", "setup", "--yes", "--client-id", "abc.apps.googleusercontent.com"],
        input="GOCSPX-should-not-be-read\n",
    )
    assert result.exit_code == EXIT_USAGE, result.output
    assert "stdin is not a TTY and --stdin was not given" in result.output
    assert read_app_secret(load_config(), "google_client_secret") is None


def test_auth_setup_non_interactive_requires_client_id_or_from_file(
    tmp_path: Path, monkeypatch
) -> None:
    _configure(tmp_path, monkeypatch, provider="google")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    result = CliRunner().invoke(main, ["auth", "setup", "--yes"])
    assert result.exit_code == EXIT_USAGE, result.output
    assert "--client-id is required" in result.output


def test_auth_setup_microsoft_interactive_walkthrough(tmp_path: Path, monkeypatch) -> None:
    """Exercise the prompt path (no ``--yes``), which every other CLI test in
    this file skips - covers the ``interactive = tty and not yes`` branch and
    every ``click.prompt`` call in ``_collect_microsoft_setup`` (issue #368
    review finding)."""
    _configure(tmp_path, monkeypatch, provider="microsoft")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    monkeypatch.setattr(cli_module, "_stdio_is_tty", lambda: True)
    result = CliRunner().invoke(
        main,
        ["auth", "setup"],
        input="12345678-1234-1234-1234-123456789012\norganizational\ncontoso.onmicrosoft.com\n",
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    written = load_config()
    assert written.tenant_id == "contoso.onmicrosoft.com"
    assert written.account_type == "organizational"
    assert read_app_secret(written, "ms_client_id") == "12345678-1234-1234-1234-123456789012"


def test_auth_setup_google_from_file_flags_override_endpoints(tmp_path: Path, monkeypatch) -> None:
    """``--auth-uri``/``--token-uri``/``--redirect-uri`` must win over the
    Desktop client JSON's own values when both are given, and the file's
    values must still be used when the flags are absent (issue #368 review
    finding: neither precedence direction had a test)."""
    _configure(tmp_path, monkeypatch, provider="google")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    oauth_file = tmp_path / "desktop-client.json"
    oauth_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "abc.apps.googleusercontent.com",
                    "client_secret": "GOCSPX-test-secret",
                    "auth_uri": "https://file.example/auth",
                    "token_uri": "https://file.example/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        )
    )
    result = CliRunner().invoke(
        main,
        [
            "auth",
            "setup",
            "--yes",
            "--from-file",
            str(oauth_file),
            "--auth-uri",
            "https://flag.example/auth",
            "--token-uri",
            "https://flag.example/token",
            "--redirect-uri",
            "http://localhost:8080",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    written = load_config()
    assert written.google_auth_uri == "https://flag.example/auth"
    assert written.google_token_uri == "https://flag.example/token"
    assert written.google_redirect_uris == ("http://localhost:8080",)

    # Re-run with a fresh profile and no override flags: the file's own
    # values must be used instead of being dropped.
    (tmp_path / "config.toml").write_text('[profiles.default]\nprovider = "google"\n')
    result = CliRunner().invoke(
        main,
        ["auth", "setup", "--yes", "--from-file", str(oauth_file)],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    written = load_config()
    assert written.google_auth_uri == "https://file.example/auth"
    assert written.google_token_uri == "https://file.example/token"
    assert written.google_redirect_uris == ("http://localhost",)
