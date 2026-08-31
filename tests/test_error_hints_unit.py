"""Every non-zero exit carries actionable next-step guidance (issue #97)."""

from __future__ import annotations

import json

import httpx
import pytest
from click.testing import CliRunner

from blumkin.auth import SecretWriteError
from blumkin.cli import _DEFAULT_HINTS, main
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_USAGE


def _run(args: list[str]) -> tuple[int, str]:
    result = CliRunner().invoke(main, args)
    return result.exit_code, (result.output or "") + (result.stderr or "")


def _json_err(args: list[str]) -> dict:
    """The compact one-line ``{...}`` object emitted by ``emit_error`` (stderr).

    ``emit_json`` payloads are pretty-printed multi-line, so a start+end brace on
    one line uniquely identifies the error object.
    """
    _, combined = _run(args)
    for line in combined.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            payload = json.loads(line)
            if "error" in payload:
                return payload
    raise AssertionError(f"no JSON error line in output:\n{combined}")


def test_default_hints_cover_every_documented_error_slug() -> None:
    # The error slugs the CLI can emit (see tests/test_skills_schema.py).
    documented = {
        "auth_required",
        "graph_error",
        "missing_scope",
        "not_found",
        "secret_write_failed",
        "timeout",
        "usage_error",
    }
    assert documented <= set(_DEFAULT_HINTS)
    for hint in _DEFAULT_HINTS.values():
        assert hint and hint[0].isupper() and hint.rstrip().endswith((".", ")"))
        assert "—" not in hint and "–" not in hint  # ASCII hyphens only


def test_missing_yes_hint_tells_you_to_add_it() -> None:
    code, out = _run(["calendar", "cancel", "--event-id", "x"])
    assert code == EXIT_USAGE
    assert "--yes" in out
    assert "notifies other people" in out
    assert "Re-run the command with --yes" in out


def test_missing_yes_hint_reason_is_per_command(tmp_path, monkeypatch) -> None:
    """meeting transcription --enable is a write gate, not a notify gate."""
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'client_id = "00000000-0000-0000-0000-000000000001"\n'
        'tenant_id = "example.onmicrosoft.com"\ndefault_tz = "UTC"\n'
        "wo1162425_scopes = true\n"
    )
    payload = _json_err(["meeting", "transcription", "--event-id", "x", "--enable", "--json"])
    assert payload["error"] == "usage_error"
    assert "meeting setting" in payload["hint"]
    assert "notifies other people" not in payload["hint"]


def test_missing_yes_json_carries_hint() -> None:
    payload = _json_err(["calendar", "cancel", "--event-id", "x", "--json"])
    assert payload["ok"] is False
    assert payload["error"] == "usage_error"
    assert "--yes" in payload["hint"]


def test_invalid_timezone_hint_points_at_iana_names() -> None:
    code, out = _run(["--tz", "Not/ARealZone", "calendar", "today"])
    assert code == EXIT_USAGE
    assert "IANA" in out and "America/New_York" in out


def test_wo1162425_gate_hint_explains_how_to_enable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'client_id = "00000000-0000-0000-0000-000000000001"\n'
        'tenant_id = "example.onmicrosoft.com"\ndefault_tz = "UTC"\n'
    )
    payload = _json_err(["people", "resolve", "--name", "Ada", "--json"])
    assert payload["error"] == "usage_error"
    assert "wo1162425_scopes = true" in payload["hint"]
    assert "auth login" in payload["hint"]
    assert "—" not in json.dumps(payload)  # no em dashes in error text


def test_mail_attachments_missing_id_hint(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'client_id = "00000000-0000-0000-0000-000000000001"\n'
        'tenant_id = "example.onmicrosoft.com"\ndefault_tz = "UTC"\n'
    )
    payload = _json_err(["mail", "attachments", "--json"])
    assert payload["error"] == "usage_error"
    assert "mail list" in payload["hint"]


def test_not_found_hint_suggests_listing(monkeypatch) -> None:
    from blumkin.skills.mail import MailMessageNotFoundError

    async def _boom(**_kwargs):
        raise MailMessageNotFoundError("no message with id 'nope'")

    monkeypatch.setattr("blumkin.providers.microsoft.mail_get", _boom)
    code, out = _run(["mail", "get", "--id", "nope"])
    assert code == EXIT_NOT_FOUND
    assert "Re-check the id" in out


@pytest.mark.parametrize(
    "args",
    [
        ["calendar", "cancel", "--event-id", "x", "--json"],
        ["chat", "send", "--with", "A", "--text", "hi", "--json"],
        ["mail", "attachments", "--json"],
    ],
)
def test_json_errors_are_single_line_objects_with_ok_false(args: list[str]) -> None:
    payload = _json_err(args)
    assert payload["ok"] is False
    assert set(payload) <= {"ok", "error", "message", "hint"}
    assert payload["hint"]


class _GraphStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _provider_raises(exc: BaseException):
    async def _boom(**_kwargs):
        raise exc

    return _boom


@pytest.mark.parametrize(
    ("slug", "exc"),
    [
        ("auth_required", ValueError("Authentication required. Run 'blumkin auth login'.")),
        ("auth_required", _GraphStatusError(401)),
        ("missing_scope", _GraphStatusError(403)),
        ("not_found", _GraphStatusError(404)),
        ("graph_error", RuntimeError("Graph 500 boom")),
        ("timeout", httpx.ReadTimeout("slow")),
        ("secret_write_failed", SecretWriteError("cannot write token cache")),
    ],
    ids=lambda v: v if isinstance(v, str) else type(v).__name__,
)
def test_fallback_hint_reaches_the_cli_for_each_error_class(slug, exc, monkeypatch) -> None:
    monkeypatch.setattr("blumkin.providers.microsoft.calendar_today", _provider_raises(exc))

    code, human = _run(["calendar", "today", "--tz", "UTC"])
    assert code != 0
    assert _DEFAULT_HINTS[slug].split(".")[0] in human

    payload = _json_err(["calendar", "today", "--tz", "UTC", "--json"])
    assert payload["error"] == slug
    assert payload["ok"] is False
    assert payload["hint"] == _DEFAULT_HINTS[slug]
