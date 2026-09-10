"""Unit coverage for ``blumkin.contacts`` - ``email-context.md`` (#207)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from blumkin.cli import main
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


def test_table_rows_parse_and_bad_emails_are_dropped(tmp_path, monkeypatch, capsys) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(_TABLE, encoding="utf-8")
    names = {c.name: c for c in load_context(cfg)}
    assert set(names) == {"Sam", "Alex"}
    assert names["Sam"].aliases == ("sammy", "S")
    assert names["Sam"].email == "sam@example.com"
    assert names["Alex"].notes == "Friend."
    # the operator is told which row was dropped and why
    err = capsys.readouterr().err
    assert "warning:" in err and "'Bad'" in err


def test_bullet_rows_parse(tmp_path, monkeypatch, capsys) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(_BULLET, encoding="utf-8")
    names = {c.name: c for c in load_context(cfg)}
    assert set(names) == {"Dana", "Robin"}
    assert names["Dana"].aliases == ("dee",)
    assert names["Dana"].notes == "manager"
    assert names["Robin"].notes == ""
    assert "warning:" in capsys.readouterr().err  # the `Nope <also-bad>` line


def test_table_and_bullet_mix_in_one_file(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(_TABLE + "\n" + _BULLET, encoding="utf-8")
    assert {c.name for c in load_context(cfg)} == {"Sam", "Alex", "Dana", "Robin"}


def test_profile_file_combines_and_a_matching_entry_merges(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(
        "| Name | Email | Notes |\n|---|---|---|\n| Sam | sam@example.com | |\n", encoding="utf-8"
    )
    prof = tmp_path / "profiles" / "work"
    prof.mkdir(parents=True)
    (prof / "email-context.md").write_text(
        "- Sam (sammy) <sam@example.com> - keep it formal\n- Kai <kai@example.com> - new\n",
        encoding="utf-8",
    )
    by_name = {c.name: c for c in load_context(cfg)}
    assert set(by_name) == {"Sam", "Kai"}
    # same address, base note blank -> merged, profile note fills, no conflict
    assert by_name["Sam"].email == "sam@example.com"
    assert by_name["Sam"].notes == "keep it formal"
    assert by_name["Sam"].aliases == ("sammy",)
    assert by_name["Sam"].conflict is False
    assert len(by_name["Sam"].sources) == 2


def test_different_address_keeps_both_variants_flagged(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(
        "- Sam <sam@personal.example.com> - my brother\n", encoding="utf-8"
    )
    prof = tmp_path / "profiles" / "work"
    prof.mkdir(parents=True)
    (prof / "email-context.md").write_text(
        "- Sam <sam@work.example.com> - teammate\n", encoding="utf-8"
    )
    sams = [c for c in load_context(cfg) if c.name == "Sam"]
    assert {c.email for c in sams} == {"sam@personal.example.com", "sam@work.example.com"}
    assert all(c.conflict for c in sams)
    assert [c.sources for c in sams] == [
        (str(tmp_path / "email-context.md"),),
        (str(prof / "email-context.md"),),
    ]


def test_same_address_different_notes_keeps_both_variants(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text("- Sam <s@example.com> - note A\n", encoding="utf-8")
    prof = tmp_path / "profiles" / "work"
    prof.mkdir(parents=True)
    (prof / "email-context.md").write_text("- Sam <s@example.com> - note B\n", encoding="utf-8")
    sams = [c for c in load_context(cfg) if c.name == "Sam"]
    assert {c.notes for c in sams} == {"note A", "note B"}
    assert all(c.conflict for c in sams)


def test_a_bullet_with_no_name_is_skipped(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(
        "-  <nobody@example.com> - ghost\n- Sam <sam@example.com>\n", encoding="utf-8"
    )
    assert [c.name for c in load_context(cfg)] == ["Sam"]


def test_a_malformed_bullet_warns_instead_of_vanishing(tmp_path, monkeypatch, capsys) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(
        "- Sam - colleague (address forgotten)\n- Alex <alex@example.com>\n", encoding="utf-8"
    )
    assert [c.name for c in load_context(cfg)] == ["Alex"]
    assert "could not parse bullet entry" in capsys.readouterr().err


def test_identical_notes_across_files_merge_without_a_conflict(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(
        "- Sam <sam@example.com> - my manager\n", encoding="utf-8"
    )
    prof = tmp_path / "profiles" / "work"
    prof.mkdir(parents=True)
    (prof / "email-context.md").write_text(
        "- Sam <sam@example.com> - my manager\n", encoding="utf-8"
    )
    (sam,) = load_context(cfg)
    assert sam.conflict is False
    assert sam.notes == "my manager"
    assert len(sam.sources) == 2


def test_a_second_table_header_is_re_detected(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(
        "| Name | Aliases | Email |\n|---|---|---|\n| Sam | s | sam@example.com |\n\n"
        "| Name | Email |\n|---|---|\n| Ana | ana@example.com |\n",
        encoding="utf-8",
    )
    assert {c.name for c in load_context(cfg)} == {"Sam", "Ana"}


def test_a_non_utf8_file_degrades_instead_of_crashing(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_bytes(
        "- Jos\xe9 <jose@example.com> - amigo\n".encode("cp1252")
    )
    (jose,) = load_context(cfg)
    assert jose.email == "jose@example.com"


def test_locate_operator_files_dedupes_a_legacy_flat_config(tmp_path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text('client_id = "x"\ndefault_tz = "UTC"\n', encoding="utf-8")
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    cfg = load_config()
    (tmp_path / "email-context.md").write_text("- Sam <sam@example.com>\n", encoding="utf-8")
    assert locate_operator_files(cfg, "email-context.md") == [tmp_path / "email-context.md"]


def test_people_context_handler_filters_by_name_or_alias(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(_TABLE, encoding="utf-8")
    for needle in ("sammy", "Sam", "SAM"):
        payload = asyncio.run(people_context(config=cfg, name=needle))
        assert [c["name"] for c in payload["contacts"]] == ["Sam"], needle
    assert asyncio.run(people_context(config=cfg, name="nobody"))["contacts"] == []
    # pin the full contact dict - agents read every one of these keys (SKILL.md)
    assert asyncio.run(people_context(config=cfg, name="Sam"))["contacts"][0] == {
        "aliases": ["sammy", "S"],
        "conflict": False,
        "email": "sam@example.com",
        "name": "Sam",
        "notes": "Colleague on Foo.",
        "sources": [str(tmp_path / "email-context.md")],
    }


def test_a_utf8_bom_header_still_parses(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_bytes(
        b"\xef\xbb\xbf| Name | Email |\n|---|---|\n| Sam | sam@example.com |\n"
    )
    assert [c.name for c in load_context(cfg)] == ["Sam"]


def test_missing_file_is_an_empty_list_not_an_error(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, profile="work")
    payload = asyncio.run(people_context(config=cfg))
    assert payload == {"ok": True, "contacts": []}


def test_cli_people_context_runs_with_no_auth_or_provider(tmp_path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch, profile="work")
    (tmp_path / "email-context.md").write_text(_TABLE, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(main, ["people", "context", "--json"], obj={})
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert {c["name"] for c in payload["contacts"]} == {"Sam", "Alex"}
    filtered = runner.invoke(main, ["people", "context", "--name", "sammy", "--json"], obj={})
    assert [c["name"] for c in json.loads(filtered.stdout)["contacts"]] == ["Sam"]


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
