"""User-facing texts name the mail folders Google accepts (issue #461)."""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest

from blumkin import help_text
from blumkin.cli import mail as mail_group
from blumkin.providers.google.mail import _FOLDER_LABELS
from blumkin.skills import SKILLS

_GOOGLE_FOLDERS = sorted(name for name, label in _FOLDER_LABELS.items() if label is not None)
_MICROSOFT_ONLY = ("archive", "outbox")
_README = Path(__file__).resolve().parents[1] / "README.md"


def _folder_option_help() -> str:
    list_command = mail_group.commands["list"]
    option = next(
        p for p in list_command.params if isinstance(p, click.Option) and p.name == "folder"
    )
    return option.help or ""


def _skill_summary() -> str:
    return next(s.summary for s in SKILLS if s.id == "mail.list")


def _readme_provider_differences() -> str:
    return _README.read_text(encoding="utf-8")


_TEXTS = {
    "cli --folder help": _folder_option_help,
    "help_text MAIL_LIST_EPILOG": lambda: help_text.MAIL_LIST_EPILOG,
    "mail.list skill summary": _skill_summary,
    "README": _readme_provider_differences,
}


def _google_sentence(text: str) -> str:
    flat = " ".join(text.split())
    match = re.search(r"Google accepts only ([^.;]*)[.;]", flat)
    assert match, f"Expected a 'Google accepts only ...' sentence, got none in {flat[:200]!r}"
    return match.group(1)


def test_google_folder_set_is_what_the_provider_maps_to_labels() -> None:
    assert _GOOGLE_FOLDERS == ["deleteditems", "drafts", "inbox", "junkemail", "sentitems"]


@pytest.mark.parametrize("where", sorted(_TEXTS))
def test_text_lists_exactly_the_folders_google_accepts(where: str) -> None:
    sentence = _google_sentence(_TEXTS[where]())
    named = set(re.findall(r"[a-z]+", sentence)) & set(_FOLDER_LABELS)
    assert named == set(_GOOGLE_FOLDERS)
    for name in _MICROSOFT_ONLY:
        assert name not in named


@pytest.mark.parametrize("where", sorted(_TEXTS))
def test_text_says_archive_and_outbox_are_microsoft_only(where: str) -> None:
    flat = " ".join(_TEXTS[where]().split())
    assert re.search(r"Microsoft[^.]*archive|archive[^.]*Microsoft", flat), flat[:300]
