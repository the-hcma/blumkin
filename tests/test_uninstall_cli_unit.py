"""CLI coverage for `blumkin uninstall`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
from click.testing import CliRunner

from blumkin import cli
from blumkin import uninstall as un
from blumkin.cli import main
from blumkin.exit_codes import EXIT_OTHER, EXIT_SUCCESS, EXIT_USAGE
from blumkin.install_method import METHOD_UV_TOOL, Install


def _invoke(args: list[str], **kwargs: Any) -> Any:
    return CliRunner().invoke(main, args, **kwargs)


def _outcome(
    category: un.Category,
    label: str,
    outcome: un.OutcomeName,
    **kwargs: Any,
) -> un.Outcome:
    return un.Outcome(
        category=category, label=label, outcome=outcome, detail=kwargs.pop("detail", ""), **kwargs
    )


def _plan() -> un.Plan:
    return un.Plan(
        agent=un.Target(category="agent", label="agent", present=True, detail="agent detail"),
        config=un.Target(category="config", label="config", present=True, detail="config detail"),
        keyring=un.Target(
            category="keyring", label="keyring", present=True, detail="keyring detail"
        ),
        mcp=(
            un.Target(
                category="mcp",
                client="cursor",
                detail="cursor user",
                label="cursor user",
                present=True,
                scope="user",
            ),
            un.Target(
                category="mcp",
                client="claude",
                detail="claude user",
                label="claude user",
                present=True,
                scope="user",
            ),
            un.Target(
                category="mcp",
                client="copilot",
                detail="copilot project",
                label="copilot project",
                present=False,
                scope="project",
            ),
        ),
        package=un.Target(
            category="package", label="package", present=True, detail="package detail"
        ),
        _install=Install(checkout=None, managed_path=Path("/x"), method=METHOD_UV_TOOL),
        _keyring_probe_error=None,
        _profiles=(),
    )


def _wire(monkeypatch, *, package_outcome: un.OutcomeName = "removed") -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(un, "build_plan", lambda **kwargs: _plan())
    monkeypatch.setattr(cli, "_stdio_is_tty", lambda: False)
    monkeypatch.setattr(
        un,
        "remove_agent",
        lambda: calls.append("agent") or _outcome("agent", "agent", "removed"),
    )
    monkeypatch.setattr(
        un,
        "remove_mcp",
        lambda client, scope, **kwargs: (
            calls.append(f"mcp:{client}:{scope}")
            or _outcome("mcp", f"{client} {scope}", "removed", client=client, scope=scope)
        ),
    )
    monkeypatch.setattr(
        un,
        "remove_package",
        lambda install: (
            calls.append("package")
            or _outcome(
                "package",
                "package",
                package_outcome,
                detail="boom" if package_outcome == "failed" else "",
            )
        ),
    )
    monkeypatch.setattr(
        un,
        "remove_config",
        lambda: calls.append("config") or _outcome("config", "config", "removed"),
    )
    monkeypatch.setattr(
        un,
        "remove_keyring",
        lambda profiles, **kwargs: (
            calls.append("keyring") or _outcome("keyring", "keyring", "removed")
        ),
    )
    return calls


def test_cli_uninstall_dry_run_never_removes(monkeypatch) -> None:
    calls = _wire(monkeypatch)

    result = _invoke(["uninstall", "--dry-run", "--json"])

    assert result.exit_code == EXIT_SUCCESS
    assert calls == []
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["categories"]["agent"]["outcome"] == "would_remove"


def test_cli_uninstall_noninteractive_without_flags_is_usage_error(monkeypatch) -> None:
    _wire(monkeypatch)

    result = _invoke(["uninstall", "--json"])

    assert result.exit_code == EXIT_USAGE
    assert json.loads(result.output)["error"] == "usage_error"


def test_cli_uninstall_noninteractive_without_yes_is_usage_error(monkeypatch) -> None:
    _wire(monkeypatch)

    result = _invoke(["uninstall", "--agent", "--json"])

    assert result.exit_code == EXIT_USAGE
    assert json.loads(result.output)["error"] == "usage_error"


def test_cli_uninstall_noninteractive_with_agent_flag_only_runs_agent(monkeypatch) -> None:
    calls = _wire(monkeypatch)

    result = _invoke(["uninstall", "--agent", "--yes", "--json"])

    assert result.exit_code == EXIT_SUCCESS
    assert calls == ["agent"]
    payload = json.loads(result.output)
    assert payload["categories"]["agent"]["outcome"] == "removed"
    assert payload["categories"]["package"]["outcome"] == "skipped"


def test_cli_uninstall_interactive_decline_everything(monkeypatch) -> None:
    calls = _wire(monkeypatch)
    monkeypatch.setattr(cli, "_stdio_is_tty", lambda: True)
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: False)

    result = _invoke(["uninstall", "--json"])

    assert result.exit_code == EXIT_SUCCESS
    assert calls == []
    payload = json.loads(result.output)
    assert payload["categories"]["agent"]["outcome"] == "skipped"
    assert payload["categories"]["mcp"][0]["outcome"] == "skipped"


def test_cli_uninstall_interactive_confirm_everything_runs_in_category_order(monkeypatch) -> None:
    calls = _wire(monkeypatch)
    monkeypatch.setattr(cli, "_stdio_is_tty", lambda: True)
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: True)

    result = _invoke(["uninstall", "--json"])

    assert result.exit_code == EXIT_SUCCESS
    assert calls == ["agent", "mcp:cursor:user", "mcp:claude:user", "package", "config", "keyring"]


def test_cli_uninstall_failure_sets_ok_false_and_exit_other(monkeypatch) -> None:
    _wire(monkeypatch, package_outcome="failed")

    result = _invoke(["uninstall", "--package", "--yes", "--json"])

    assert result.exit_code == EXIT_OTHER
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["categories"]["package"]["outcome"] == "failed"


def test_cli_uninstall_mcp_specific_flag_overrides_blanket_decline(monkeypatch) -> None:
    calls = _wire(monkeypatch)

    result = _invoke(["uninstall", "--mcp-cursor", "--no-mcp", "--yes", "--json"])

    assert result.exit_code == EXIT_SUCCESS
    assert calls == ["mcp:cursor:user"]
    payload = json.loads(result.output)
    assert payload["categories"]["mcp"][0]["outcome"] == "removed"
    assert payload["categories"]["mcp"][1]["outcome"] == "skipped"


def test_cli_uninstall_mcp_specific_flags_target_each_client(monkeypatch) -> None:
    calls = _wire(monkeypatch)

    result = _invoke(["uninstall", "--mcp-claude", "--yes", "--json"])

    assert result.exit_code == EXIT_SUCCESS
    assert calls == ["mcp:claude:user"]

    calls = _wire(monkeypatch)
    plan = _plan()
    monkeypatch.setattr(
        un,
        "build_plan",
        lambda **kwargs: un.Plan(
            agent=plan.agent,
            config=plan.config,
            keyring=plan.keyring,
            mcp=(
                un.Target(
                    category="mcp",
                    client="copilot",
                    detail="copilot user",
                    label="copilot user",
                    present=True,
                    scope="user",
                ),
            ),
            package=plan.package,
            _install=plan._install,
            _keyring_probe_error=plan._keyring_probe_error,
            _profiles=plan._profiles,
        ),
    )

    result = _invoke(["uninstall", "--mcp-copilot", "--yes", "--json"])

    assert result.exit_code == EXIT_SUCCESS
    assert calls == ["mcp:copilot:user"]


def test_cli_uninstall_mcp_failure_sets_ok_false_and_exit_other(monkeypatch) -> None:
    def _remove_mcp(client: str, scope: un.Scope, *, cwd: Path | None = None) -> un.Outcome:
        return un.Outcome(
            category="mcp",
            client=client,
            detail="boom",
            label=f"{client}:{scope}",
            outcome="failed",
            scope=scope,
        )

    monkeypatch.setattr(un, "build_plan", lambda cwd=None: _plan())
    monkeypatch.setattr(un, "remove_agent", lambda: _outcome("agent", "agent", "removed"))
    monkeypatch.setattr(un, "remove_mcp", _remove_mcp)
    monkeypatch.setattr(
        un,
        "remove_package",
        lambda install: _outcome("package", "package", "removed"),
    )
    monkeypatch.setattr(un, "remove_config", lambda: _outcome("config", "config", "removed"))
    monkeypatch.setattr(
        un,
        "remove_keyring",
        lambda profiles, **kwargs: _outcome("keyring", "keyring", "removed"),
    )

    result = _invoke(["uninstall", "--mcp-cursor", "--yes", "--json"])

    assert result.exit_code == EXIT_OTHER
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["categories"]["mcp"][0]["outcome"] == "failed"


def test_cli_uninstall_passes_keyring_probe_error_to_remove_keyring(monkeypatch) -> None:
    seen: dict[str, str | None] = {}
    plan = _plan()

    monkeypatch.setattr(
        un,
        "build_plan",
        lambda **kwargs: un.Plan(
            agent=plan.agent,
            config=plan.config,
            keyring=plan.keyring,
            mcp=plan.mcp,
            package=plan.package,
            _install=plan._install,
            _keyring_probe_error="boom",
            _profiles=plan._profiles,
        ),
    )
    monkeypatch.setattr(cli, "_stdio_is_tty", lambda: False)
    monkeypatch.setattr(un, "remove_agent", lambda: _outcome("agent", "agent", "removed"))
    monkeypatch.setattr(
        un,
        "remove_mcp",
        lambda client, scope, **kwargs: _outcome(
            "mcp", f"{client} {scope}", "skipped", client=client, scope=scope
        ),
    )
    monkeypatch.setattr(
        un,
        "remove_package",
        lambda install: _outcome("package", "package", "skipped"),
    )
    monkeypatch.setattr(un, "remove_config", lambda: _outcome("config", "config", "skipped"))

    def _remove_keyring(
        profiles: tuple[tuple[str, Any], ...], *, probe_error: str | None = None
    ) -> un.Outcome:
        seen["probe_error"] = probe_error
        return _outcome("keyring", "keyring", "failed", detail=str(probe_error))

    monkeypatch.setattr(un, "remove_keyring", _remove_keyring)

    result = _invoke(["uninstall", "--keyring", "--yes", "--json"])

    assert result.exit_code == EXIT_OTHER
    assert seen["probe_error"] == "boom"


def test_format_uninstall_human_renders_mcp_labels_and_failure_trailer() -> None:
    payload = {
        "ok": False,
        "dry_run": False,
        "categories": {
            "agent": {
                "label": "agent",
                "outcome": "removed",
                "detail": "runtime removed",
            },
            "mcp": [
                {
                    "label": "Remove MCP registration for Cursor (user)",
                    "outcome": "removed",
                    "detail": "registration removed",
                },
                {
                    "label": "Remove MCP registration for Claude Code (project)",
                    "outcome": "failed",
                    "detail": "boom",
                },
            ],
            "package": {
                "label": "package",
                "outcome": "skipped",
                "detail": "not confirmed",
            },
            "config": {
                "label": "config",
                "outcome": "not_present",
                "detail": "already absent",
            },
            "keyring": {
                "label": "keyring",
                "outcome": "failed",
                "detail": "locked",
            },
        },
    }

    lines = cli._format_uninstall_human(payload)

    assert "  Remove MCP registration for Cursor (user): removed - registration removed" in lines
    assert "  Remove MCP registration for Claude Code (project): FAILED - boom" in lines
    assert lines[-1] == "one or more uninstall steps failed"
