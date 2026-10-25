"""`blumkin loop` CLI commands (issue #501)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_SUCCESS, EXIT_USAGE
from blumkin.loop.service import UNIT_NAME


@pytest.fixture(autouse=True)
def _config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                "[profiles.default]",
                'client_id = "abc-123"',
                "",
                "[loop.triggers.probe-trigger]",
                'task = "probe"',
                'instance = "default"',
                'kind = "manual"',
                "",
                "[cron.eod]",
                'task = "time-sheet"',
                'instance = "current"',
                'schedule = "0 18 * * 1-5"',
                'timezone = "UTC"',
                "working_days_only = true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return tmp_path


def _run(*args: str) -> Any:
    return CliRunner().invoke(main, ["--json", *args])


def _json(result: Any) -> dict[str, Any]:
    return json.loads(result.output)


def test_loop_list_shows_triggers(_config_dir: Path) -> None:
    result = _run("loop", "list")
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _json(result)
    names = [t["name"] for t in payload["triggers"]]
    assert names == ["eod", "probe-trigger"]
    assert payload["triggers"][0]["kind"] == "cron"
    assert payload["triggers"][1]["kind"] == "manual"


def test_loop_run_once_probe(_config_dir: Path) -> None:
    result = _run("loop", "run-once", "probe-trigger")
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _json(result)
    assert payload["skipped"] is False
    assert payload["run"]["outcome"] == "done"
    assert payload["run"]["task"] == "probe"


def test_loop_pause_and_run_once_skips(_config_dir: Path) -> None:
    assert _run("loop", "pause", "probe-trigger").exit_code == EXIT_SUCCESS
    payload = _json(_run("loop", "run-once", "probe-trigger"))
    assert payload["skipped"] is True
    assert payload["skip_reason"] == "paused"
    assert _run("loop", "resume", "probe-trigger").exit_code == EXIT_SUCCESS
    payload = _json(_run("loop", "run-once", "probe-trigger"))
    assert payload["skipped"] is False


def test_loop_status_reports_last_run(_config_dir: Path) -> None:
    _run("loop", "run-once", "probe-trigger")
    payload = _json(_run("loop", "status"))
    assert payload["kill_switch"] is False
    probe = next(t for t in payload["triggers"] if t["name"] == "probe-trigger")
    assert probe["last_run"]["outcome"] == "done"


def test_loop_unknown_trigger_is_not_found(_config_dir: Path) -> None:
    result = _run("loop", "run-once", "missing")
    assert result.exit_code == EXIT_NOT_FOUND
    assert _json(result)["error"] == "not_found"


def test_loop_bad_config_is_usage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "x"\n[loop]\nnope = true\n',
        encoding="utf-8",
    )
    result = _run("loop", "list")
    assert result.exit_code == EXIT_USAGE
    assert _json(result)["error"] == "usage_error"


def test_loop_install_service_writes_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _config_dir: Path
) -> None:
    unit_dir = tmp_path / "systemd-user"
    blumkin_bin = tmp_path / "blumkin"
    blumkin_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    blumkin_bin.chmod(0o755)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # Point install at our fake unit dir via monkeypatch of default.
    monkeypatch.setattr(
        "blumkin.loop.service._default_unit_dir",
        lambda: unit_dir,
    )
    result = _run("loop", "install-service", "--blumkin-path", str(blumkin_bin))
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _json(result)
    assert payload["unit_name"] == UNIT_NAME
    text = Path(payload["unit"]).read_text(encoding="utf-8")
    assert f"ExecStart={blumkin_bin.resolve()} loop run" in text or (
        f'ExecStart="{blumkin_bin.resolve()}" loop run' in text
    )
    assert "BLUMKIN_NONINTERACTIVE=1" in text


def test_unit_text_quotes_paths_with_spaces() -> None:
    from blumkin.loop.service import unit_text

    text = unit_text("/opt/my tools/blumkin")
    assert 'ExecStart="/opt/my tools/blumkin" loop run' in text
