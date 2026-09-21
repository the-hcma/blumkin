"""Unit coverage for ``blumkin.output`` - the OSC 8 hyperlink helper (#233)."""

from __future__ import annotations

import io
import json

import pytest

from blumkin.output import emit_error, hyperlink

_URL = "https://accounts.google.com/o/oauth2/auth?response_type=code&scope=a+b+c"
_OSC8 = "\x1b]8;;"


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _NotTty(io.StringIO):
    def isatty(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _neutral_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLUMKIN_HYPERLINKS", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")


def test_tty_stream_gets_an_osc8_sequence() -> None:
    out = hyperlink("Google authorization page", _URL, stream=_Tty())
    assert out == f"\x1b]8;;{_URL}\x1b\\Google authorization page\x1b]8;;\x1b\\"


def test_non_tty_stream_falls_back_to_label_and_url() -> None:
    assert hyperlink("Doc title", _URL, stream=_NotTty()) == f"Doc title ({_URL})"


def test_non_tty_stream_with_label_equal_to_url_is_the_bare_url() -> None:
    assert hyperlink(_URL, _URL, stream=_NotTty()) == _URL


def test_env_never_wins_over_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLUMKIN_HYPERLINKS", "never")
    assert hyperlink("label", _URL, stream=_Tty()) == f"label ({_URL})"


def test_env_always_wins_over_a_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLUMKIN_HYPERLINKS", "always")
    assert hyperlink("label", _URL, stream=_NotTty()).startswith(_OSC8)


def test_no_color_disables_hyperlinks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert hyperlink("label", _URL, stream=_Tty()) == f"label ({_URL})"


def test_dumb_terminal_disables_hyperlinks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "dumb")
    assert hyperlink("label", _URL, stream=_Tty()) == f"label ({_URL})"


def test_a_url_with_a_control_char_neither_emits_nor_passes_through_an_escape() -> None:
    poisoned = f"{_URL}\x1b]0;pwned\x07"
    out = hyperlink("label", poisoned, stream=_Tty())
    # Falls back to plain text, and the control bytes are stripped from it too -
    # the fallback is printed straight to the terminal.
    assert out == f"label ({_URL}]0;pwned)"
    assert "\x1b" not in out and "\x07" not in out


def test_a_hostile_label_cannot_break_out_of_the_escape() -> None:
    # A remote Drive/doc name carrying the OSC terminator + a new hyperlink.
    hostile = "report\x1b\\\x1b]8;;https://evil.example\x1b\\click"
    out = hyperlink(hostile, _URL, stream=_Tty())
    assert out.startswith(f"\x1b]8;;{_URL}\x1b\\")
    assert out.endswith("\x1b]8;;\x1b\\")
    assert out.count("\x1b") == 4  # only the helper's own escapes; none from the label
    assert "evil.example" in out  # survives as literal, harmless text
    # Plain fallback is clean too.
    assert "\x1b" not in hyperlink(hostile, _URL, stream=_NotTty())


def test_a_placeholder_url_round_trips_through_str_format() -> None:
    template = hyperlink("Google authorization page", "{url}", stream=_Tty())
    assert template.format(url=_URL) == (
        f"\x1b]8;;{_URL}\x1b\\Google authorization page\x1b]8;;\x1b\\"
    )


def test_emit_error_json_carries_agent_instructions_and_retry_after_seconds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit_error(
        error="too_soon",
        message="mail.send-draft was composed 3s ago",
        as_json=True,
        hint="wait for the cooldown",
        agent_instructions="Do not retry automatically. Confirm with the user first.",
        retry_after_seconds=17.0,
    )
    err = capsys.readouterr().err
    payload = json.loads(err)
    assert payload["error"] == "too_soon"
    assert payload["ok"] is False
    assert payload["hint"] == "wait for the cooldown"
    assert payload["agent_instructions"] == (
        "Do not retry automatically. Confirm with the user first."
    )
    assert payload["retry_after_seconds"] == 17.0


def test_emit_error_human_readable_prints_agent_instructions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit_error(
        error="too_soon",
        message="mail.send-draft was composed 3s ago",
        as_json=False,
        agent_instructions="Do not retry automatically. Confirm with the user first.",
        retry_after_seconds=17.0,
    )
    err = capsys.readouterr().err
    assert "mail.send-draft was composed 3s ago" in err
    assert "Do not retry automatically" in err


def test_emit_error_omits_optional_fields_when_absent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit_error(error="not_found", message="gone", as_json=True)
    payload = json.loads(capsys.readouterr().err)
    assert "agent_instructions" not in payload
    assert "retry_after_seconds" not in payload
    assert "hint" not in payload
