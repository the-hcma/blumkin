"""CLI usage / exit-code mapping (no live Graph)."""

from __future__ import annotations

from click.testing import CliRunner

from blumkin.cli import main
from blumkin.exit_codes import EXIT_AUTH, EXIT_USAGE


def test_calendar_create_without_yes_exits_usage() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "calendar",
            "create",
            "--subject",
            "x",
            "--with",
            "a@b.com",
            "--start",
            "2026-08-26T11:00",
        ],
    )
    assert result.exit_code == EXIT_USAGE


def test_calendar_today_invalid_tz_exits_usage() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--tz", "Not/ARealZone", "calendar", "today"])
    assert result.exit_code == EXIT_USAGE


def test_calendar_view_accepts_subcommand_tz() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["calendar", "view", "--from", "2026-08-25", "--to", "2026-08-26", "--tz", "Not/ARealZone"],
    )
    assert result.exit_code == EXIT_USAGE
    assert "No such option" not in (result.output or "")


def test_doctor_auth_cache_incomplete_exits_auth(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'client_id = "00000000-0000-0000-0000-000000000001"\n'
        'tenant_id = "example.onmicrosoft.com"\n'
    )
    runner = CliRunner()
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == EXIT_AUTH
