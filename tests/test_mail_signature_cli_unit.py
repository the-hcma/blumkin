"""CLI surface for the Outlook auto-signature probe: login, doctor, mail signature, logout."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from blumkin.cli import _refresh_signature_probe, main
from blumkin.config import load_config
from blumkin.mail_signature_state import load_signature_state, record_signature_state

_CONFIG = (
    '[profiles.default]\nclient_id = "abc"\ntenant_id = "example.com"\ndefault_tz = "UTC"\n'
    '[profiles.default.mail.signature]\nenabled = true\nname = "Ada"\n'
)


class _Probe:
    """Minimal provider stand-in exposing an async probe_mail_signature."""

    def __init__(self, result: bool | None) -> None:
        self._result = result

    async def probe_mail_signature(self) -> bool | None:
        return self._result


def test_refresh_signature_probe_records_a_positive_result(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_CONFIG)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    cfg = load_config()
    with patch("blumkin.cli._workspace", return_value=_Probe(True)):
        _refresh_signature_probe(cfg)
    assert load_signature_state(cfg).detected is True


def test_refresh_signature_probe_swallows_a_failed_state_write(tmp_path: Path, monkeypatch) -> None:
    """An OSError persisting the result must not break an otherwise successful login."""
    (tmp_path / "config.toml").write_text(_CONFIG)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    cfg = load_config()
    monkeypatch.setattr(
        "blumkin.cli.record_signature_state",
        MagicMock(side_effect=OSError("read-only config dir")),
    )
    with patch("blumkin.cli._workspace", return_value=_Probe(True)):
        _refresh_signature_probe(cfg)  # must not raise


def test_refresh_signature_probe_swallows_a_broken_provider(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_CONFIG)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    cfg = load_config()
    with patch("blumkin.cli._workspace", return_value=MagicMock()):
        _refresh_signature_probe(cfg)  # asyncio.run on a MagicMock raises; must be caught
    assert load_signature_state(cfg).detected is None


def test_mail_signature_reports_suppressed(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_CONFIG)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    record_signature_state(load_config(), detected=True)
    result = CliRunner().invoke(main, ["mail", "signature", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["suppressed"] is True
    assert payload["outlook_signature_detected"] is True
    human = CliRunner().invoke(main, ["mail", "signature"])
    assert "auto-inserts" in human.output


def test_mail_signature_not_suppressed_without_a_probe(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_CONFIG)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    payload = json.loads(CliRunner().invoke(main, ["mail", "signature", "--json"]).stdout)
    assert payload["suppressed"] is False
    assert payload["outlook_signature_detected"] is None


def _doctor_provider(probe_result: bool | None) -> MagicMock:
    provider = MagicMock()
    provider.auth_status.return_value = {
        "client_id_configured": True,
        "token_cache": True,
        "auth_record": True,
        "requested_scopes": [],
        "missing_scopes": [],
    }
    provider.account_email.return_value = ""
    provider.probe_mail_signature = AsyncMock(return_value=probe_result)
    return provider


def test_doctor_reports_the_signature_suppression(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_CONFIG)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    with patch("blumkin.cli._workspace", return_value=_doctor_provider(True)):
        result = CliRunner().invoke(main, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    assert payload["mail_signature"] == {
        "configured": True,
        "outlook_signature_detected": True,
        "suppressed": True,
    }
    assert any("double signature" in w for w in payload["warnings"])


def test_doctor_re_probe_clears_a_stale_positive(tmp_path: Path, monkeypatch) -> None:
    """User turns the Outlook auto-signature off, runs doctor: the state must flip."""
    (tmp_path / "config.toml").write_text(_CONFIG)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    record_signature_state(load_config(), detected=True)
    with patch("blumkin.cli._workspace", return_value=_doctor_provider(False)):
        result = CliRunner().invoke(main, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    assert payload["mail_signature"]["outlook_signature_detected"] is False
    assert payload["mail_signature"]["suppressed"] is False
    assert not any("double signature" in w for w in payload["warnings"])
    assert load_signature_state(load_config()).detected is False


def test_auth_login_runs_the_probe_and_reports_it(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_CONFIG + 'email = "ada@example.com"\n')
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    provider = MagicMock()
    provider.auth_status.return_value = {"token_cache": True, "auth_record": True}
    provider.probe_mail_signature = AsyncMock(return_value=True)
    with patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(main, ["auth", "login", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["outlook_signature_detected"] is True
    assert load_signature_state(load_config()).detected is True


def test_auth_logout_clears_the_probe_state(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(_CONFIG)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    cfg = load_config()
    record_signature_state(cfg, detected=True)
    provider = MagicMock()
    with patch("blumkin.cli._workspace", return_value=provider):
        result = CliRunner().invoke(main, ["auth", "logout", "--json"])
    assert result.exit_code == 0
    assert not cfg.mail_signature_state_path.is_file()
