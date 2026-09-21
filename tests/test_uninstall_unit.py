"""Unit coverage for blumkin.uninstall."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from blumkin import secret_store, uninstall
from blumkin.agent.client import AgentUnavailableError, AgentUnreachableError
from blumkin.config import load_config
from blumkin.install_method import METHOD_PIPX, METHOD_UNMANAGED, METHOD_UV_TOOL, Install
from blumkin.mcp_install import McpInstallError
from blumkin.providers.kind import ProviderConfigError
from blumkin.secret_store import SecretWriteError


class _PasswordDeleteError(Exception):
    pass


class _FakeKeyring:
    class errors:
        PasswordDeleteError = _PasswordDeleteError

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise _PasswordDeleteError("not found")
        del self.store[(service, username)]

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password


def _config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    token_storage: str = "keyring",
) -> Any:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                "[profiles.default]",
                'client_id = "test-client"',
                f'token_storage = "{token_storage}"',
            ]
        )
        + "\n"
    )
    return load_config()


def _runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "runtime"
    monkeypatch.setenv("BLUMKIN_AGENT_RUNTIME_DIR", str(base))
    runtime = base / f"blumkin-agent-{os.getuid()}"
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime


def test_build_plan_reports_present_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    (runtime / "agent.sock").write_text("")
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(cfg_dir))
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text('[profiles.default]\nclient_id = "test-client"\n')
    monkeypatch.setattr(uninstall, "is_supported_platform", lambda: True)
    monkeypatch.setattr(
        uninstall.agent_client,
        "call",
        lambda *args, **kwargs: (_ for _ in ()).throw(AgentUnavailableError("down")),
    )
    monkeypatch.setattr(
        uninstall,
        "current_entry",
        lambda client, scope, cwd: (
            {"command": "blumkin"} if (client, scope) == ("cursor", "user") else None
        ),
    )
    monkeypatch.setattr(
        uninstall,
        "detect_install",
        lambda: Install(checkout=None, managed_path=Path("/x"), method=METHOD_UV_TOOL),
    )
    monkeypatch.setattr(
        uninstall,
        "_profile_keyring_state",
        lambda cfg: uninstall._KeyringState(
            backend_unavailable=False,
            present=True,
            unknown=False,
        ),
    )

    plan = uninstall.build_plan(cwd=tmp_path)

    assert plan.agent.present is True
    assert any(
        target.present
        for target in plan.mcp
        if target.client == "cursor" and target.scope == "user"
    )
    assert plan.package.present is True
    assert plan.config.present is True
    assert plan.keyring.present is True


def test_build_plan_degrades_keyring_probe_errors_to_the_keyring_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        uninstall,
        "list_profiles",
        lambda: (_ for _ in ()).throw(ProviderConfigError("bad profiles")),
    )
    monkeypatch.setattr(
        uninstall,
        "detect_install",
        lambda: Install(checkout=None, managed_path=Path("/x"), method=METHOD_UNMANAGED),
    )

    plan = uninstall.build_plan(cwd=tmp_path)

    assert plan.package.present is False
    assert plan.keyring.present is True
    assert plan._keyring_probe_error == "bad profiles"


def test_build_plan_reports_toml_decode_errors_as_keyring_probe_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.toml").write_text("[profiles.default\n")
    monkeypatch.setattr(
        uninstall,
        "detect_install",
        lambda: Install(checkout=None, managed_path=Path("/x"), method=METHOD_UNMANAGED),
    )

    plan = uninstall.build_plan(cwd=tmp_path)

    assert plan.keyring.present is True
    assert plan._keyring_probe_error is not None
    assert plan._keyring_probe_error.startswith("config.toml is not valid TOML")


def test_remove_agent_succeeds_and_deletes_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    (runtime / "agent.sock").write_text("")
    monkeypatch.setattr(uninstall, "is_supported_platform", lambda: True)
    monkeypatch.setattr(uninstall.agent_client, "call", lambda *args, **kwargs: {"ok": True})

    outcome = uninstall.remove_agent()

    assert outcome.outcome == "removed"
    assert not runtime.exists()


def test_remove_agent_removes_runtime_for_a_wedged_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(uninstall, "is_supported_platform", lambda: True)

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise AgentUnreachableError("wedged")

    monkeypatch.setattr(uninstall.agent_client, "call", _raise)

    outcome = uninstall.remove_agent()

    assert outcome.outcome == "removed"
    assert "wedged" in outcome.detail
    assert not runtime.exists()


def test_remove_agent_reports_runtime_dir_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(uninstall, "is_supported_platform", lambda: True)
    monkeypatch.setattr(
        uninstall,
        "runtime_dir",
        lambda: (_ for _ in ()).throw(RuntimeError("bad")),
    )

    outcome = uninstall.remove_agent()

    assert outcome.outcome == "failed"


@pytest.mark.parametrize(
    ("result", "expected"),
    [("removed", "removed"), ("absent", "not_present")],
)
def test_remove_mcp_wraps_removed_and_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: str,
    expected: str,
) -> None:
    monkeypatch.setattr(uninstall, "remove_entry", lambda *args, **kwargs: result)

    assert uninstall.remove_mcp("cursor", "user", cwd=tmp_path).outcome == expected


def test_remove_mcp_wraps_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise McpInstallError("nope")

    monkeypatch.setattr(uninstall, "remove_entry", _boom)

    assert uninstall.remove_mcp("cursor", "user", cwd=tmp_path).outcome == "failed"


def test_remove_package_covers_success_failure_and_not_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install = Install(checkout=None, managed_path=Path("/x"), method=METHOD_PIPX)
    monkeypatch.setattr(
        uninstall.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="done", stderr=""),
    )
    assert uninstall.remove_package(install).outcome == "removed"

    monkeypatch.setattr(
        uninstall.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr="boom"),
    )
    assert uninstall.remove_package(install).outcome == "failed"

    unmanaged = Install(checkout=None, managed_path=Path("/x"), method=METHOD_UNMANAGED)
    assert uninstall.remove_package(unmanaged).outcome == "not_present"


def test_remove_package_reports_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    install = Install(checkout=None, managed_path=Path("/x"), method=METHOD_PIPX)

    def _oserror(*args: Any, **kwargs: Any) -> Any:
        raise OSError("missing")

    monkeypatch.setattr(uninstall.subprocess, "run", _oserror)

    assert uninstall.remove_package(install).outcome == "failed"


def test_remove_package_reports_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    install = Install(checkout=None, managed_path=Path("/x"), method=METHOD_PIPX)

    def _timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=60)

    monkeypatch.setattr(uninstall.subprocess, "run", _timeout)

    assert uninstall.remove_package(install).outcome == "failed"


def test_remove_config_covers_success_failure_and_not_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "cfg"
    config.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config))
    assert uninstall.remove_config().outcome == "removed"
    assert uninstall.remove_config().outcome == "not_present"

    target = tmp_path / "real-cfg"
    target.mkdir()
    link = tmp_path / "symlink-cfg"
    link.symlink_to(target)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(link))
    assert uninstall.remove_config().outcome == "failed"


def test_remove_keyring_removes_keyring_but_keeps_plaintext_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(tmp_path, monkeypatch)
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    key = (secret_store._KEYRING_SERVICE, secret_store._keyring_account(cfg, "google_token"))
    fake.store[key] = "keyring-value"
    cfg.google_token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.google_token_path.write_text("file-value")

    outcome = uninstall.remove_keyring(((cfg.profile, cfg),))

    assert outcome.outcome == "removed"
    assert cfg.google_token_path.read_text() == "file-value"
    assert key not in fake.store


def test_remove_keyring_reports_backend_unavailable_for_keyring_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)

    outcome = uninstall.remove_keyring(((cfg.profile, cfg),))

    assert outcome.outcome == "failed"


def test_remove_keyring_reports_probe_errors_directly() -> None:
    outcome = uninstall.remove_keyring((), probe_error="boom")

    assert outcome.outcome == "failed"
    assert outcome.detail == "boom"


def test_remove_keyring_reports_not_present_for_file_backed_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(tmp_path, monkeypatch, token_storage="file")
    cfg.google_token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.google_token_path.write_text("file-value")

    outcome = uninstall.remove_keyring(((cfg.profile, cfg),))

    assert outcome.outcome == "not_present"


def test_remove_keyring_still_attempts_delete_when_probe_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(tmp_path, monkeypatch)
    fake = _FakeKeyring()
    key = (secret_store._KEYRING_SERVICE, secret_store._keyring_account(cfg, "google_token"))
    fake.store[key] = "keyring-value"

    def _probe_fails(service: str, username: str) -> str | None:
        raise RuntimeError("locked")

    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    monkeypatch.setattr(fake, "get_password", _probe_fails)

    outcome = uninstall.remove_keyring(((cfg.profile, cfg),))

    assert outcome.outcome == "removed"
    assert key not in fake.store


def test_remove_keyring_converts_unexpected_delete_errors_to_failed_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(tmp_path, monkeypatch)
    monkeypatch.setattr(
        uninstall,
        "_profile_keyring_state",
        lambda cfg: uninstall._KeyringState(
            backend_unavailable=False,
            present=False,
            unknown=True,
        ),
    )
    monkeypatch.setattr(
        uninstall,
        "delete_keyring_entry",
        lambda cfg, kind: (_ for _ in ()).throw(TimeoutError("timed out")),
    )

    outcome = uninstall.remove_keyring(((cfg.profile, cfg),))

    assert outcome.outcome == "failed"
    assert "timed out" in outcome.detail


def test_remove_keyring_reports_not_present_and_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _FakeKeyring())
    assert uninstall.remove_keyring(((cfg.profile, cfg),)).outcome == "not_present"
    monkeypatch.setattr(
        uninstall,
        "_profile_keyring_state",
        lambda cfg: uninstall._KeyringState(
            backend_unavailable=False,
            present=True,
            unknown=False,
        ),
    )

    def _boom(cfg: Any, kind: Any) -> None:
        if kind == "google_token":
            raise SecretWriteError("locked")

    monkeypatch.setattr(uninstall, "delete_keyring_entry", _boom)
    outcome = uninstall.remove_keyring(((cfg.profile, cfg),))

    assert outcome.outcome == "failed"
