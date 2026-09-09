"""Unit coverage for ``blumkin.output`` - the OSC 8 hyperlink helper (#233)."""

from __future__ import annotations

import io

import pytest

from blumkin.output import hyperlink

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


def test_a_url_with_a_control_char_never_emits_an_escape() -> None:
    poisoned = f"{_URL}\x1b]0;pwned\x07"
    assert hyperlink("label", poisoned, stream=_Tty()) == f"label ({poisoned})"


def test_a_placeholder_url_round_trips_through_str_format() -> None:
    template = hyperlink("Google authorization page", "{url}", stream=_Tty())
    assert template.format(url=_URL) == (
        f"\x1b]8;;{_URL}\x1b\\Google authorization page\x1b]8;;\x1b\\"
    )
