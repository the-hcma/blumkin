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
        summary=(
            "Create a calendar event; --teams needs wo1162425_scopes + OnlineMeetings.ReadWrite"
        ),
        mutates=True,
        notifies_others=True,
        scopes=["Calendars.ReadWrite", "OnlineMeetings.ReadWrite"],
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
        summary=(
            "Get free/busy for one or more email addresses (includes attendee "
            "timezone / working hours when Graph returns them)"
        ),
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
        id="calendar.suggest",
        cli=["blumkin", "calendar", "suggest"],
        summary=(
            "Suggest mutual free slots for a duration over a range "
            "(from freebusy; does not create an event)"
        ),
        mutates=False,
        notifies_others=False,
        scopes=["Calendars.ReadWrite"],
        args=[
            {"name": "--with", "required": True, "type": "email", "multiple": True},
            {"name": "--start", "required": True, "type": "datetime"},
            {"name": "--end", "required": True, "type": "datetime"},
            {"name": "--duration", "required": False, "type": "duration", "note": "default 30m"},
            {
                "name": "--window",
                "required": False,
                "type": "string",
                "note": "optional HH:MM-HH:MM local day clip",
            },
            {
                "name": "--treat-tentative",
                "required": False,
                "type": "enum",
                "values": ["busy", "free"],
                "note": "default busy",
            },
            {"name": "--limit", "required": False, "type": "int", "note": "default 10"},
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
        id="chat.attachments",
        cli=["blumkin", "chat", "attachments"],
        summary=(
            "List attachments on a chat message (exactly one of --chat-id or --with, "
            "and exactly one of --message-id or --latest)"
        ),
        mutates=False,
        notifies_others=False,
        scopes=["Chat.Read"],
        args=[
            {
                "name": "--chat-id",
                "required": False,
                "type": "string",
                "note": "exactly one of --chat-id or --with",
            },
            {
                "name": "--latest",
                "required": False,
                "type": "flag",
                "note": "exactly one of --message-id or --latest; picks newest message "
                "carrying attachments",
            },
            {
                "name": "--message-id",
                "required": False,
                "type": "string",
                "note": "exactly one of --message-id or --latest",
            },
            {
                "name": "--with",
                "required": False,
                "type": "string",
                "note": "exactly one of --chat-id or --with; refuses if multiple matches",
            },
        ],
    ),
    SkillSpec(
        id="chat.attachments.download",
        cli=["blumkin", "chat", "attachments", "download"],
        summary=(
            "Download Teams chat files to disk (exactly one of --attachment-id or --all; "
            "requires files_scopes because chat files live in SharePoint/OneDrive)"
        ),
        mutates=False,
        notifies_others=False,
        scopes=["Chat.Read", "Files.Read"],
        args=[
            {
                "name": "--all",
                "required": False,
                "type": "flag",
                "note": "exactly one of --attachment-id or --all; --out must be a directory",
            },
            {
                "name": "--attachment-id",
                "required": False,
                "type": "string",
                "note": "exactly one of --attachment-id or --all",
            },
            {
                "name": "--chat-id",
                "required": False,
                "type": "string",
                "note": "exactly one of --chat-id or --with",
            },
            {
                "name": "--latest",
                "required": False,
                "type": "flag",
                "note": "exactly one of --message-id or --latest",
            },
            {
                "name": "--message-id",
                "required": False,
                "type": "string",
                "note": "exactly one of --message-id or --latest",
            },
            {
                "name": "--out",
                "required": True,
                "type": "path",
                "note": "file or directory; must be a directory with --all",
            },
            {
                "name": "--with",
                "required": False,
                "type": "string",
                "note": "exactly one of --chat-id or --with",
            },
        ],
    ),
    SkillSpec(
        id="chat.delete",
        cli=["blumkin", "chat", "delete"],
        summary="Soft-delete a chat message (requires wo1162425_scopes)",
        mutates=True,
        notifies_others=True,
        scopes=["Chat.ReadWrite"],
        args=[
            {"name": "--chat-id", "required": True, "type": "string"},
            {"name": "--message-id", "required": True, "type": "string"},
            {"name": "--yes", "required": True, "type": "flag"},
        ],
    ),
    SkillSpec(
        id="chat.edit",
        cli=["blumkin", "chat", "edit"],
        summary="Edit a chat message body in place (requires wo1162425_scopes)",
        mutates=True,
        notifies_others=True,
        scopes=["Chat.ReadWrite"],
        args=[
            {"name": "--chat-id", "required": True, "type": "string"},
            {"name": "--message-id", "required": True, "type": "string"},
            {"name": "--text", "required": True, "type": "string"},
            {"name": "--yes", "required": True, "type": "flag"},
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
        id="chat.send",
        cli=["blumkin", "chat", "send"],
        summary=(
            "Send a text message to a chat (requires wo1162425_scopes; "
            "exactly one of --with or --chat-id)"
        ),
        mutates=True,
        notifies_others=True,
        scopes=["Chat.ReadWrite"],
        args=[
            {
                "name": "--chat-id",
                "required": False,
                "type": "string",
                "note": "exactly one of --with or --chat-id",
            },
            {"name": "--text", "required": True, "type": "string"},
            {
                "name": "--with",
                "required": False,
                "type": "string",
                "note": "exactly one of --with or --chat-id; refuses if multiple matches",
            },
            {"name": "--yes", "required": True, "type": "flag"},
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
        id="mail.attachments",
        cli=["blumkin", "mail", "attachments"],
        summary="List attachments on a message",
        mutates=False,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[{"name": "--id", "required": True, "type": "string"}],
    ),
    SkillSpec(
        id="mail.attachments.download",
        cli=["blumkin", "mail", "attachments", "download"],
        summary=(
            "Download one or all file attachments from a message "
            "(exactly one of --attachment-id or --all; --out is a file or directory)"
        ),
        mutates=False,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[
            {"name": "--message-id", "required": True, "type": "string"},
            {
                "name": "--attachment-id",
                "required": False,
                "type": "string",
                "note": "exactly one of --attachment-id or --all",
            },
            {
                "name": "--all",
                "required": False,
                "type": "flag",
                "note": "exactly one of --attachment-id or --all; --out must be a directory",
            },
            {"name": "--out", "required": True, "type": "path"},
        ],
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
            {
                "name": "--to",
                "required": True,
                "type": "email",
                "note": "repeatable or comma-separated",
            },
            {
                "name": "--cc",
                "required": False,
                "type": "email",
                "note": "repeatable or comma-separated",
            },
            {
                "name": "--bcc",
                "required": False,
                "type": "email",
                "note": "repeatable or comma-separated",
            },
            {"name": "--subject", "required": True, "type": "string"},
            {
                "name": "--attach",
                "required": False,
                "type": "path",
                "note": "repeatable; each file must be under 2 MB",
            },
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
            {
                "name": "--no-signature",
                "required": False,
                "type": "flag",
                "note": "skip [mail.signature] when enabled in config",
            },
        ],
    ),
    SkillSpec(
        id="mail.folders",
        cli=["blumkin", "mail", "folders"],
        summary=(
            "List mail folders with their ids and message counts "
            "(counts come from Graph and may lag; not proof of emptiness)"
        ),
        mutates=False,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[],
    ),
    SkillSpec(
        id="mail.forward",
        cli=["blumkin", "mail", "forward"],
        summary="Create a forward draft carrying the original message (does not send)",
        mutates=True,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[
            {"name": "--id", "required": True, "type": "string"},
            {"name": "--to", "required": True, "type": "email"},
            {
                "name": "--cc",
                "required": False,
                "type": "email",
                "note": "adds to Graph-inherited CC; repeatable or comma-separated",
            },
            {
                "name": "--bcc",
                "required": False,
                "type": "email",
                "note": "adds to Graph-inherited BCC; repeatable or comma-separated",
            },
            {
                "name": "--body",
                "required": False,
                "type": "string",
                "note": "at most one of --body or --body-file; omit for an empty draft",
            },
            {"name": "--body-file", "required": False, "type": "path"},
            {"name": "--body-type", "required": False, "type": "enum", "values": ["html", "text"]},
            {
                "name": "--no-signature",
                "required": False,
                "type": "flag",
                "note": "skip [mail.signature] when enabled in config",
            },
        ],
    ),
    SkillSpec(
        id="mail.get",
        cli=["blumkin", "mail", "get"],
        summary="Read one message in full, including its body and attachments",
        mutates=False,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[
            {"name": "--id", "required": True, "type": "string"},
            {
                "name": "--body-type",
                "required": False,
                "type": "enum",
                "values": ["html", "text"],
                "note": "body format requested from Graph; defaults to text",
            },
        ],
    ),
    SkillSpec(
        id="mail.inbox",
        cli=["blumkin", "mail", "inbox"],
        summary="List recent inbox messages, optionally filtered or searched",
        mutates=False,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[
            {"name": "--from", "required": False, "type": "string"},
            {"name": "--subject", "required": False, "type": "string"},
            {
                "name": "--search",
                "required": False,
                "type": "string",
                "note": "Graph $search; cannot be combined with the other filters or --orderby",
            },
            {"name": "--since", "required": False, "type": "datetime"},
            {"name": "--until", "required": False, "type": "datetime"},
            {"name": "--unread", "required": False, "type": "flag"},
            {"name": "--top", "required": False, "type": "int"},
            {"name": "--tz", "required": False, "type": "iana_tz"},
        ],
    ),
    SkillSpec(
        id="mail.list",
        cli=["blumkin", "mail", "list"],
        summary=(
            "List recent messages from a mail folder "
            "(well-known name such as sentitems/archive, or a folder id)"
        ),
        mutates=False,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[
            {
                "name": "--folder",
                "required": False,
                "type": "string",
                "note": "well-known name or folder id; omit for the whole mailbox",
            },
            {
                "name": "--orderby",
                "required": False,
                "type": "enum",
                "values": ["created", "received", "sent"],
            },
            {"name": "--from", "required": False, "type": "string"},
            {"name": "--subject", "required": False, "type": "string"},
            {
                "name": "--search",
                "required": False,
                "type": "string",
                "note": "Graph $search; cannot be combined with the other filters or --orderby",
            },
            {"name": "--since", "required": False, "type": "datetime"},
            {"name": "--until", "required": False, "type": "datetime"},
            {"name": "--unread", "required": False, "type": "flag"},
            {"name": "--top", "required": False, "type": "int"},
            {"name": "--tz", "required": False, "type": "iana_tz"},
        ],
    ),
    SkillSpec(
        id="mail.reply",
        cli=["blumkin", "mail", "reply"],
        summary=(
            "Create a reply draft through Graph so it threads: recipients, subject, "
            "and conversation carry over from the original (does not send)"
        ),
        mutates=True,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[
            {"name": "--id", "required": True, "type": "string"},
            {"name": "--all", "required": False, "type": "flag", "note": "reply to everyone"},
            {
                "name": "--cc",
                "required": False,
                "type": "email",
                "note": "adds to Graph-inherited CC; repeatable or comma-separated",
            },
            {
                "name": "--bcc",
                "required": False,
                "type": "email",
                "note": "adds to Graph-inherited BCC; repeatable or comma-separated",
            },
            {
                "name": "--body",
                "required": False,
                "type": "string",
                "note": "at most one of --body or --body-file; omit for an empty draft",
            },
            {"name": "--body-file", "required": False, "type": "path"},
            {"name": "--body-type", "required": False, "type": "enum", "values": ["html", "text"]},
            {
                "name": "--no-signature",
                "required": False,
                "type": "flag",
                "note": "skip [mail.signature] when enabled in config",
            },
        ],
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
        id="mail.update-draft",
        cli=["blumkin", "mail", "update-draft"],
        summary="Patch an existing draft in place (does not send)",
        mutates=True,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[
            {"name": "--id", "required": True, "type": "string"},
            {
                "name": "--attach",
                "required": False,
                "type": "path",
                "note": "repeatable; adds to the draft's existing attachments",
            },
            {"name": "--subject", "required": False, "type": "string"},
            {
                "name": "--to",
                "required": False,
                "type": "email",
                "note": "replaces entire To list; repeatable or comma-separated",
            },
            {
                "name": "--cc",
                "required": False,
                "type": "email",
                "note": "replaces entire CC list; repeatable or comma-separated",
            },
            {
                "name": "--bcc",
                "required": False,
                "type": "email",
                "note": "replaces entire BCC list; repeatable or comma-separated",
            },
            {"name": "--body", "required": False, "type": "string"},
            {"name": "--body-file", "required": False, "type": "path"},
            {"name": "--body-type", "required": False, "type": "enum", "values": ["text", "html"]},
        ],
    ),
    SkillSpec(
        id="meeting.get",
        cli=["blumkin", "meeting", "get"],
        summary=(
            "Show online-meeting details for a calendar event you organize "
            "(requires wo1162425_scopes; attendee-only meetings are not in /me/onlineMeetings)"
        ),
        mutates=False,
        notifies_others=False,
        scopes=["Calendars.ReadWrite", "OnlineMeetings.ReadWrite"],
        args=[{"name": "--event-id", "required": True, "type": "string"}],
    ),
    SkillSpec(
        id="meeting.transcription",
        cli=["blumkin", "meeting", "transcription"],
        summary=(
            "Show or enable allowTranscription on an online meeting (requires wo1162425_scopes)"
        ),
        mutates=True,
        notifies_others=False,
        scopes=["Calendars.ReadWrite", "OnlineMeetings.ReadWrite"],
        args=[
            {"name": "--event-id", "required": True, "type": "string"},
            {"name": "--enable", "required": False, "type": "flag"},
            {
                "name": "--yes",
                "required": False,
                "type": "flag",
                "note": "required with --enable",
            },
        ],
    ),
    SkillSpec(
        id="people.resolve",
        cli=["blumkin", "people", "resolve"],
        summary=(
            "Resolve a display name or email via Graph people search "
            "(fail-closed on zero or multiple matches; requires People.Read)"
        ),
        mutates=False,
        notifies_others=False,
        scopes=["People.Read"],
        args=[
            {
                "name": "--name",
                "required": False,
                "type": "string",
                "note": "display name search; provide --name and/or --email",
            },
            {
                "name": "--email",
                "required": False,
                "type": "email",
                "note": "exact email filter / reverse lookup; provide --name and/or --email",
            },
            {"name": "--top", "required": False, "type": "int", "note": "default 10, max 50"},
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
