"""Skill catalog and skill metadata."""

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
        id="calendar.accept",
        cli=["blumkin", "calendar", "accept"],
        summary="Accept calendar invitation(s) by event id or today's pending",
        mutates=True,
        notifies_others=True,
        scopes=["Calendars.ReadWrite"],
        args=[
            {"name": "--event-id", "required": False, "type": "string"},
            {"name": "--today-pending", "required": False, "type": "flag"},
            {"name": "--tz", "required": False, "type": "iana_tz"},
            {"name": "--yes", "required": True, "type": "flag"},
        ],
    ),
    SkillSpec(
        id="calendar.cancel",
        cli=["blumkin", "calendar", "cancel"],
        summary="Cancel a calendar event and notify attendees",
        mutates=True,
        notifies_others=True,
        scopes=["Calendars.ReadWrite"],
        args=[
            {"name": "--event-id", "required": True, "type": "string"},
            {"name": "--yes", "required": True, "type": "flag"},
        ],
    ),
    SkillSpec(
        id="calendar.create",
        cli=["blumkin", "calendar", "create"],
        summary="Create a calendar event (optional Teams online meeting)",
        mutates=True,
        notifies_others=True,
        scopes=["Calendars.ReadWrite"],
        args=[
            {"name": "--subject", "required": True, "type": "string"},
            {"name": "--with", "required": True, "type": "email", "multiple": True},
            {"name": "--start", "required": True, "type": "datetime"},
            {"name": "--duration", "required": False, "type": "duration"},
            {"name": "--teams", "required": False, "type": "flag"},
            {"name": "--tz", "required": False, "type": "iana_tz"},
            {"name": "--yes", "required": True, "type": "flag"},
        ],
    ),
    SkillSpec(
        id="calendar.freebusy",
        cli=["blumkin", "calendar", "freebusy"],
        summary="Get free/busy for one or more email addresses",
        mutates=False,
        notifies_others=False,
        scopes=["Calendars.ReadWrite"],
        args=[
            {"name": "--with", "required": True, "type": "email", "multiple": True},
            {"name": "--start", "required": True, "type": "datetime"},
            {"name": "--end", "required": True, "type": "datetime"},
            {"name": "--tz", "required": False, "type": "iana_tz"},
        ],
    ),
    SkillSpec(
        id="calendar.today",
        cli=["blumkin", "calendar", "today"],
        summary="List the signed-in user's events for today",
        mutates=False,
        notifies_others=False,
        scopes=["Calendars.ReadWrite"],
        args=[
            {"name": "--date", "required": False, "type": "date"},
            {"name": "--tz", "required": False, "type": "iana_tz"},
        ],
    ),
    SkillSpec(
        id="calendar.view",
        cli=["blumkin", "calendar", "view"],
        summary="List events in a half-open local date range [--from, --to)",
        mutates=False,
        notifies_others=False,
        scopes=["Calendars.ReadWrite"],
        args=[
            {"name": "--from", "required": True, "type": "date"},
            {"name": "--to", "required": True, "type": "date"},
            {"name": "--tz", "required": False, "type": "iana_tz"},
        ],
    ),
    SkillSpec(
        id="chat.find",
        cli=["blumkin", "chat", "find"],
        summary="Find Teams chats whose members match a display name",
        mutates=False,
        notifies_others=False,
        scopes=["Chat.Read"],
        args=[{"name": "--with", "required": True, "type": "string"}],
    ),
    SkillSpec(
        id="chat.last",
        cli=["blumkin", "chat", "last"],
        summary="Show the last N messages from a chat matched by display name",
        mutates=False,
        notifies_others=False,
        scopes=["Chat.Read"],
        args=[
            {"name": "--with", "required": True, "type": "string"},
            {"name": "--n", "required": False, "type": "int"},
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
        id="mail.delete-draft",
        cli=["blumkin", "mail", "delete-draft"],
        summary="Delete a draft message (does not notify recipients)",
        mutates=True,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[{"name": "--id", "required": True, "type": "string"}],
    ),
    SkillSpec(
        id="mail.draft",
        cli=["blumkin", "mail", "draft"],
        summary=(
            "Create a mail draft (exactly one of --body or --body-file required; does not send)"
        ),
        mutates=True,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[
            {"name": "--to", "required": True, "type": "email"},
            {"name": "--subject", "required": True, "type": "string"},
            {
                "name": "--body",
                "required": False,
                "type": "string",
                "note": "exactly one of --body or --body-file",
            },
            {
                "name": "--body-file",
                "required": False,
                "type": "path",
                "note": "exactly one of --body or --body-file",
            },
            {"name": "--body-type", "required": False, "type": "enum", "values": ["text", "html"]},
        ],
    ),
    SkillSpec(
        id="mail.inbox",
        cli=["blumkin", "mail", "inbox"],
        summary="List recent inbox messages",
        mutates=False,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[{"name": "--top", "required": False, "type": "int"}],
    ),
    SkillSpec(
        id="mail.send-draft",
        cli=["blumkin", "mail", "send-draft"],
        summary="Send an existing draft message",
        mutates=True,
        notifies_others=True,
        scopes=["Mail.Send"],
        args=[
            {"name": "--id", "required": True, "type": "string"},
            {"name": "--yes", "required": True, "type": "flag"},
        ],
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
