"""`blumkin agent status`/`lock` CLI wiring (issue #328, foundation layer)."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from blumkin.agent import client as agent_client
from blumkin.agent.client import AgentUnavailableError
from blumkin.cli import main
from blumkin.exit_codes import EXIT_OTHER, EXIT_SUCCESS


def test_agent_lock_json_when_not_running_is_a_no_op() -> None:
    with patch("blumkin.cli.agent_client.call", side_effect=AgentUnavailableError("no agent")):
        result = CliRunner().invoke(main, ["agent", "lock", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert '"agent_running": false' in result.output
    assert '"locked": true' in result.output


def test_agent_lock_json_when_running() -> None:
    with patch("blumkin.cli.agent_client.call", return_value={"ok": True}):
        result = CliRunner().invoke(main, ["agent", "lock", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert '"agent_running": true' in result.output
    assert '"locked": true' in result.output


def test_agent_lock_json_when_unreachable_does_not_claim_it_locked() -> None:
    """An unresponsive agent may still hold state - this must not report `locked: true`."""
    with patch(
        "blumkin.cli.agent_client.call",
        side_effect=agent_client.AgentUnreachableError("did not reply in time"),
    ):
        result = CliRunner().invoke(main, ["agent", "lock", "--json"])
    assert result.exit_code == EXIT_OTHER
    assert '"agent_running": true' in result.output
    assert '"reachable": false' in result.output
    assert '"locked": false' in result.output
    assert '"ok": false' in result.output


def test_agent_lock_never_spawns_an_agent() -> None:
    """Locking must always call with `spawn=False` - never start one to lock it."""
    with patch("blumkin.cli.agent_client.call", return_value={"ok": True}) as call:
        CliRunner().invoke(main, ["agent", "lock", "--json"])
    call.assert_called_once_with("lock", spawn=False)


def test_agent_lock_text_when_unreachable_does_not_say_locked_and_fails() -> None:
    """A lock that did not happen must not read `locked:` (it prints `locked: ...`

    on both success branches) and must exit non-zero, not silently succeed
    while `locked` is `false` in the equivalent JSON output (see PR #329
    review).
    """
    with patch(
        "blumkin.cli.agent_client.call",
        side_effect=agent_client.AgentUnreachableError("did not reply in time"),
    ):
        result = CliRunner().invoke(main, ["agent", "lock"])
    assert result.exit_code == EXIT_OTHER
    assert "lock failed" in result.output
    assert not result.output.startswith("locked:")


def test_agent_status_json_when_not_running() -> None:
    with patch("blumkin.cli.agent_client.call", side_effect=AgentUnavailableError("no agent")):
        result = CliRunner().invoke(main, ["agent", "status", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert '"agent_running": false' in result.output


def test_agent_status_json_when_running() -> None:
    fake_response = {
        "ok": True,
        "agent_version": "1.4.0",
        "agent_pid": 999,
        "cached_profiles": [],
    }
    with patch("blumkin.cli.agent_client.call", return_value=fake_response):
        result = CliRunner().invoke(main, ["agent", "status", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert '"agent_running": true' in result.output
    assert '"agent_pid": 999' in result.output


def test_agent_status_json_when_unreachable() -> None:
    """A live-but-unresponsive agent must not be reported as "not running"."""
    with patch(
        "blumkin.cli.agent_client.call",
        side_effect=agent_client.AgentUnreachableError("did not reply in time"),
    ):
        result = CliRunner().invoke(main, ["agent", "status", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert '"agent_running": true' in result.output
    assert '"reachable": false' in result.output


def test_agent_status_never_spawns_an_agent() -> None:
    with patch("blumkin.cli.agent_client.call", return_value={"ok": True}) as call:
        CliRunner().invoke(main, ["agent", "status", "--json"])
    call.assert_called_once_with("status", spawn=False)


def test_agent_status_text_when_running() -> None:
    fake_response = {
        "ok": True,
        "agent_version": "1.4.0",
        "agent_pid": 999,
        "cached_profiles": ["work"],
    }
    with patch("blumkin.cli.agent_client.call", return_value=fake_response):
        result = CliRunner().invoke(main, ["agent", "status"])
    assert result.exit_code == EXIT_SUCCESS
    assert "agent_pid: 999" in result.output
    assert "cached_profiles: work" in result.output
