"""Unit tests for skill catalog."""

from __future__ import annotations

from blumkin.skills import describe_skill, skills_catalog


def test_skills_catalog_has_calendar_today() -> None:
    catalog = skills_catalog()
    ids = [s["id"] for s in catalog["skills"]]
    assert catalog["version"] == 1
    assert "calendar.today" in ids
    assert ids == sorted(ids)


def test_describe_calendar_today() -> None:
    skill = describe_skill("calendar.today")
    assert skill is not None
    assert skill.cli == ["blumkin", "calendar", "today"]
    assert skill.mutates is False
    assert skill.scopes == ["Calendars.Read"]
