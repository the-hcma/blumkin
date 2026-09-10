"""Unit coverage for ``blumkin.contacts`` - ``email-context.md`` (#207)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from blumkin.config import load_config
from blumkin.contacts import (
    format_people_context_human,
    load_context,
    locate_operator_files,
    people_context,
)

_TABLE = """\
Anything above the header is ignored.

| Name  | Aliases   | Email             | Notes                   |
|-------|-----------|-------------------|-------------------------|
| Sam   | sammy, S  | sam@example.com   | Colleague on Foo.       |
| Alex  |           | alex@example.com  | Friend.                 |
| Bad   |           | not-an-email      | dropped                 |
"""

_BULLET = """\
- Dana (dee) <dana@example.com> - manager
- Nope <also-bad> - dropped
- Robin <robin@example.com>
"""


def _cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, profile: str | None = None):
    (tmp_path / "config.toml").write_text(
        '[profiles.work]\nprovider = "microsoft"\ntenant_id = "x"\nclient_id = "y"\n'
        '[profiles.personal]\nprovider = "google"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    if profile:
        monkeypatch.setenv("BLUMKIN_PROFILE", profile)
    return load_config(profile=profile)


def test_table_rows_parse_and_bad_emails_are_dropped(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(_TABLE, encoding="utf-8")
    names = {c.name: c for c in load_context(cfg)}
    assert set(names) == {"Sam", "Alex"}
    assert names["Sam"].aliases == ("sammy", "S")
    assert names["Sam"].email == "sam@example.com"
    assert names["Alex"].notes == "Friend."


def test_bullet_rows_parse(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(_BULLET, encoding="utf-8")
    names = {c.name: c for c in load_context(cfg)}
    assert set(names) == {"Dana", "Robin"}
    assert names["Dana"].aliases == ("dee",)
    assert names["Dana"].notes == "manager"
    assert names["Robin"].notes == ""


def test_table_and_bullet_mix_in_one_file(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(_TABLE + "\n" + _BULLET, encoding="utf-8")
    assert {c.name for c in load_context(cfg)} == {"Sam", "Alex", "Dana", "Robin"}


def test_profile_file_merges_over_config_dir_and_flags_conflicts(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(_TABLE, encoding="utf-8")
    prof = tmp_path / "profiles" / "work"
    prof.mkdir(parents=True)
    (prof / "email-context.md").write_text(
        "- Sam <sam@work.example.com> - a different Sam\n- Kai <kai@example.com> - new\n",
        encoding="utf-8",
    )
    by_name = {c.name: c for c in load_context(cfg)}
    assert set(by_name) == {"Sam", "Alex", "Kai"}
    assert by_name["Sam"].conflict is True
    assert len(by_name["Sam"].sources) == 2
    assert by_name["Kai"].conflict is False


def test_locate_operator_files_dedupes_a_legacy_flat_config(tmp_path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text('client_id = "x"\ndefault_tz = "UTC"\n', encoding="utf-8")
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    cfg = load_config()
    (tmp_path / "email-context.md").write_text("- Sam <sam@example.com>\n", encoding="utf-8")
    assert locate_operator_files(cfg, "email-context.md") == [tmp_path / "email-context.md"]


def test_people_context_handler_filters_by_name_or_alias(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(_TABLE, encoding="utf-8")
    payload = asyncio.run(people_context(config=cfg, name="sammy"))
    assert payload["ok"] is True
    assert [c["name"] for c in payload["contacts"]] == ["Sam"]
    assert asyncio.run(people_context(config=cfg, name="nobody"))["contacts"] == []


def test_missing_file_is_an_empty_list_not_an_error(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    payload = asyncio.run(people_context(config=cfg))
    assert payload == {"ok": True, "contacts": []}


def test_human_formatter_shows_aliases_notes_and_conflicts() -> None:
    lines = format_people_context_human(
        {
            "contacts": [
                {
                    "name": "Sam",
                    "aliases": ["sammy"],
                    "email": "sam@example.com",
                    "notes": "Colleague.",
                    "conflict": True,
                    "sources": ["/a/email-context.md", "/b/email-context.md"],
                }
            ]
        }
    )
    assert lines[0] == "Sam (sammy) <sam@example.com>"
    assert lines[1] == "  Colleague."
    assert "conflicting entries" in lines[2]
    assert format_people_context_human({"contacts": []})[0].startswith("(no contacts")
