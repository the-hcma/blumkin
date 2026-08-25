"""Skill catalog and calendar skills."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SkillSpec:
    args: list[dict[str, Any]]
    cli: list[str]
    id: str
    mutates: bool
    notifies_others: bool
    scopes: list[str]
    summary: str


SKILLS: list[SkillSpec] = [
    SkillSpec(
        id="auth.login",
        cli=["blumkin", "auth", "login"],
        summary="Interactive browser sign-in; write token cache + auth record",
        mutates=True,
        notifies_others=False,
        scopes=[],
        args=[],
    ),
    SkillSpec(
        id="auth.logout",
        cli=["blumkin", "auth", "logout"],
        summary="Delete local token cache and auth record",
        mutates=True,
        notifies_others=False,
        scopes=[],
        args=[],
    ),
    SkillSpec(
        id="auth.status",
        cli=["blumkin", "auth", "status"],
        summary="Show whether client id, cache, and auth record are present",
        mutates=False,
        notifies_others=False,
        scopes=[],
        args=[],
    ),
    SkillSpec(
        id="calendar.today",
        cli=["blumkin", "calendar", "today"],
        summary="List the signed-in user's events for today",
        mutates=False,
        notifies_others=False,
        scopes=["Calendars.Read"],
        args=[
            {"name": "--date", "required": False, "type": "date"},
            {"name": "--tz", "required": False, "type": "iana_tz"},
        ],
    ),
    SkillSpec(
        id="doctor",
        cli=["blumkin", "doctor"],
        summary="Check config, cache, and skill scope requirements",
        mutates=False,
        notifies_others=False,
        scopes=[],
        args=[],
    ),
    SkillSpec(
        id="skills.describe",
        cli=["blumkin", "skills", "describe"],
        summary="Describe one skill by id",
        mutates=False,
        notifies_others=False,
        scopes=[],
        args=[{"name": "skill-id", "required": True, "type": "string"}],
    ),
    SkillSpec(
        id="skills.list",
        cli=["blumkin", "skills", "list"],
        summary="List Blumkin skills for agent discovery",
        mutates=False,
        notifies_others=False,
        scopes=[],
        args=[],
    ),
]


def describe_skill(skill_id: str) -> SkillSpec | None:
    for skill in SKILLS:
        if skill.id == skill_id:
            return skill
    return None


def skills_catalog() -> dict[str, Any]:
    return {
        "cli": "blumkin",
        "skills": [
            {
                "args": list(skill.args),
                "cli": list(skill.cli),
                "id": skill.id,
                "mutates": skill.mutates,
                "notifies_others": skill.notifies_others,
                "scopes": list(skill.scopes),
                "summary": skill.summary,
            }
            for skill in SKILLS
        ],
        "version": 1,
    }
