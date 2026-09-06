"""Skill catalog and skill metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from blumkin.version import get_build_info


@dataclass(frozen=True, slots=True)
class SkillSpec:
    args: list[dict[str, Any]]
    cli: list[str]
    id: str
    mutates: bool
    notifies_others: bool
    scopes: list[str]
    summary: str


# Dotted skill id -> provider method, where `id.replace(".", "_").replace("-", "_")`
# does not land on the right name.
SKILL_METHOD_OVERRIDES: dict[str, str] = {
    "chat.attachments": "chat_attachments_list",
    "mail.attachments": "mail_attachments_list",
}

# Skills gated on `wo1162425_scopes` (Microsoft add-on grant). `mail.auto-reply` is
# gated only when it is actually changing a setting - handled in the dispatch layer.
WO1162425_SKILLS: frozenset[str] = frozenset(
    {
        "chat.delete",
        "chat.edit",
        "chat.send",
        "meeting.get",
        "meeting.transcription",
        "people.resolve",
    }
)

# Skills with no `WorkspaceProvider` method - the CLI keeps a bespoke callback and
# the MCP server does not expose them.
BESPOKE_SKILLS: frozenset[str] = frozenset(
    {
        "auth.login",
        "auth.logout",
        "auth.refresh",
        "auth.status",
        "doctor",
        "mail.signature",
        "mcp.serve",
        "skills.describe",
        "skills.list",
    }
)

# (skill id, catalog arg name) -> provider kwarg. Only listed where it differs from
# the default `name.lstrip("-").replace("-", "_")`. `None` means the value is consumed
# by the consent gate or a dispatch preprocessor and never passed as a direct kwarg.
_ARG_PARAM: dict[tuple[str, str], str | None] = {
    # --tz: `tz_name` where the provider method takes it; consumed (folded into a
    # tz-aware datetime) on the range/search verbs.
    ("calendar.accept", "--tz"): "tz_name",
    ("calendar.create", "--tz"): "tz_name",
    ("calendar.decline", "--tz"): "tz_name",
    ("calendar.get", "--tz"): "tz_name",
    ("calendar.tentative", "--tz"): "tz_name",
    ("calendar.today", "--tz"): "tz_name",
    ("calendar.update", "--tz"): "tz_name",
    ("calendar.freebusy", "--tz"): None,
    ("calendar.suggest", "--tz"): None,
    ("calendar.view", "--tz"): None,
    ("mail.inbox", "--tz"): None,
    ("mail.list", "--tz"): None,
    ("mail.search", "--tz"): None,
    # --with: attendee emails vs a display name to resolve.
    ("calendar.create", "--with"): "with_emails",
    ("calendar.update", "--with"): "with_emails",
    ("calendar.freebusy", "--with"): "with_emails",
    ("calendar.suggest", "--with"): "with_emails",
    ("chat.attachments", "--with"): "with_name",
    ("chat.attachments.download", "--with"): "with_name",
    ("chat.find", "--with"): "with_name",
    ("chat.last", "--with"): "with_name",
    ("chat.send", "--with"): "with_name",
    # calendar create/update take the start/end strings raw (the skill parses them);
    # calendar view folds --from/--to into a [start, end) datetime pair.
    ("calendar.create", "--start"): "start_raw",
    ("calendar.create", "--optional"): "optional_emails",
    ("calendar.update", "--start"): "start_raw",
    ("calendar.update", "--end"): "end_raw",
    ("calendar.view", "--from"): "start",
    ("calendar.view", "--to"): "end",
    ("calendar.today", "--date"): "day",
    ("calendar.decline", "--propose-time"): "propose_start",
    ("calendar.tentative", "--propose-time"): "propose_start",
    # --no-teams is the negated pole of Click's `--teams/--no-teams`; it binds to the
    # `teams` kwarg (negated by the `negate_flag` coerce below).
    ("calendar.create", "--no-teams"): "teams",
    ("calendar.update", "--no-teams"): "teams",
    # recurrence flags are folded into a single `recurrence` value object.
    ("calendar.create", "--repeat"): None,
    ("calendar.create", "--interval"): None,
    ("calendar.create", "--until"): None,
    ("calendar.create", "--count"): None,
    ("calendar.create", "--days"): None,
    # mail: --from is a sender substring, not a range bound.
    ("mail.inbox", "--from"): "sender",
    ("mail.list", "--from"): "sender",
    # --id spans three id namespaces.
    ("mail.attachments", "--id"): "message_id",
    ("mail.get", "--id"): "message_id",
    ("mail.thread", "--id"): "message_id",
    ("mail.forward", "--id"): "message_id",
    ("mail.reply", "--id"): "message_id",
    ("mail.delete", "--id"): "message_ids",
    ("mail.mark", "--id"): "message_ids",
    ("mail.move", "--id"): "message_ids",
    ("mail.delete-draft", "--id"): "draft_id",
    ("mail.send-draft", "--id"): "draft_id",
    ("mail.update-draft", "--id"): "draft_id",
    # --all: download every attachment vs reply to everyone.
    ("chat.attachments.download", "--all"): "download_all",
    ("mail.attachments.download", "--all"): "download_all",
    ("mail.reply", "--all"): "reply_all",
    ("mail.mark", "--flag"): "flagged",
    ("mail.auto-reply", "--external"): "external_audience",
    ("mail.auto-reply", "--on"): None,
    ("mail.auto-reply", "--off"): None,
}

# (skill id, catalog arg name) -> a coercion the dispatch layer applies on top of the
# `type` vocabulary before calling the provider.
_ARG_COERCE: dict[tuple[str, str], str] = {
    ("calendar.today", "--date"): "date",
    ("calendar.view", "--from"): "local_midnight",
    ("calendar.view", "--to"): "local_midnight",
    ("calendar.create", "--start"): "raw",
    ("calendar.create", "--no-teams"): "negate_flag",
    ("calendar.update", "--start"): "raw",
    ("calendar.update", "--end"): "raw",
    ("calendar.update", "--no-teams"): "negate_flag",
    ("calendar.freebusy", "--start"): "local_datetime",
    ("calendar.freebusy", "--end"): "local_datetime",
    ("calendar.suggest", "--start"): "local_datetime",
    ("calendar.suggest", "--end"): "local_datetime",
    ("calendar.suggest", "--duration"): "duration",
    # decline/tentative parse the proposed slot themselves (like create/update start/end).
    ("calendar.decline", "--propose-time"): "raw",
    ("calendar.tentative", "--propose-time"): "raw",
    ("mail.inbox", "--since"): "local_datetime",
    ("mail.inbox", "--until"): "local_datetime",
    ("mail.list", "--since"): "local_datetime",
    ("mail.list", "--until"): "local_datetime",
    ("mail.search", "--since"): "local_datetime",
    ("mail.search", "--until"): "local_datetime",
    ("mail.auto-reply", "--start"): "date",
    ("mail.auto-reply", "--until"): "date",
    ("calendar.create", "--with"): "list",
    ("calendar.create", "--optional"): "list",
    ("calendar.update", "--with"): "list",
    ("calendar.freebusy", "--with"): "list",
    ("calendar.suggest", "--with"): "list",
    ("mail.delete", "--id"): "list",
    ("mail.mark", "--id"): "list",
    ("mail.move", "--id"): "list",
}


def _default_param(arg_name: str) -> str:
    return arg_name.lstrip("-").replace("-", "_")


def resolve_arg_param(skill_id: str, arg: dict[str, Any]) -> str | None:
    """The provider kwarg an arg maps to.

    ``None`` means the value is never handed to a provider method as a direct
    kwarg: the skill is bespoke (no ``WorkspaceProvider`` method), or a consent
    gate / dispatch preprocessor consumes the value.
    """
    if skill_id in BESPOKE_SKILLS:
        return None
    name = arg["name"]
    if name == "--yes":
        return None
    if (skill_id, name) in _ARG_PARAM:
        return _ARG_PARAM[(skill_id, name)]
    return _default_param(name)


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
        id="auth.refresh",
        cli=["blumkin", "auth", "refresh"],
        summary="Silent token refresh (never opens a browser); persist updated cache",
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
            {"name": "--comment", "required": False, "type": "string"},
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
            {"name": "--calendar", "required": False, "type": "string"},
            {"name": "--yes", "required": True, "type": "flag"},
        ],
    ),
    SkillSpec(
        id="calendar.create",
        cli=["blumkin", "calendar", "create"],
        summary=(
            "Create a calendar event (Teams online meeting by default; "
            "--no-teams for an offline hold)"
        ),
        mutates=True,
        notifies_others=True,
        scopes=["Calendars.ReadWrite"],
        args=[
            {"name": "--subject", "required": True, "type": "string"},
            {"name": "--with", "required": False, "type": "email", "multiple": True},
            {"name": "--start", "required": True, "type": "datetime"},
            {"name": "--duration", "required": False, "type": "duration"},
            {
                "name": "--all-day",
                "required": False,
                "type": "flag",
                "note": "--start is a date, --duration is whole days",
            },
            {"name": "--location", "required": False, "type": "string"},
            {
                "name": "--calendar",
                "required": False,
                "type": "string",
                "note": "name or id; default primary",
            },
            {"name": "--optional", "required": False, "type": "email", "multiple": True},
            {"name": "--body", "required": False, "type": "string"},
            {"name": "--body-file", "required": False, "type": "path"},
            {
                "name": "--body-type",
                "required": False,
                "type": "enum",
                "values": ["html", "text"],
                "note": "Microsoft only",
            },
            {
                "name": "--remind-email",
                "required": False,
                "type": "duration",
                "note": "reminder lead time; email on Google, Outlook popup on Microsoft",
            },
            {
                "name": "--no-teams",
                "required": False,
                "type": "flag",
                "note": "--teams / --no-teams; a Teams online meeting is attached by default",
            },
            {
                "name": "--repeat",
                "required": False,
                "type": "enum",
                "values": ["daily", "weekly", "monthly"],
                "note": "make this a recurring series",
            },
            {
                "name": "--interval",
                "required": False,
                "type": "int",
                "note": "repeat every N days/weeks/months (with --repeat)",
            },
            {
                "name": "--until",
                "required": False,
                "type": "date",
                "note": "end the series on this date; exclusive with --count",
            },
            {
                "name": "--count",
                "required": False,
                "type": "int",
                "note": "stop after N occurrences; exclusive with --until",
            },
            {
                "name": "--days",
                "required": False,
                "type": "string",
                "note": "weekly only: comma list like mon,tue,wed,thu,fri",
            },
            {"name": "--tz", "required": False, "type": "iana_tz"},
            {"name": "--yes", "required": True, "type": "flag"},
        ],
    ),
    SkillSpec(
        id="calendar.decline",
        cli=["blumkin", "calendar", "decline"],
        summary="Decline calendar invitation(s); optional --comment / --propose-time",
        mutates=True,
        notifies_others=True,
        scopes=["Calendars.ReadWrite"],
        args=[
            {"name": "--event-id", "required": False, "type": "string"},
            {"name": "--today-pending", "required": False, "type": "flag"},
            {"name": "--comment", "required": False, "type": "string"},
            {
                "name": "--propose-time",
                "required": False,
                "type": "datetime",
                "note": "Microsoft only; fails closed on Google",
            },
            {"name": "--propose-duration", "required": False, "type": "duration"},
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
        id="calendar.get",
        cli=["blumkin", "calendar", "get"],
        summary="Read one calendar event in full (body, attendees + responses, recurrence)",
        mutates=False,
        notifies_others=False,
        scopes=["Calendars.ReadWrite"],
        args=[
            {"name": "--event-id", "required": True, "type": "string"},
            {"name": "--body-type", "required": False, "type": "enum", "values": ["html", "text"]},
            {"name": "--calendar", "required": False, "type": "string"},
            {"name": "--tz", "required": False, "type": "iana_tz"},
        ],
    ),
    SkillSpec(
        id="calendar.list",
        cli=["blumkin", "calendar", "list"],
        summary="List the calendars this account can see (id, name, default, editability)",
        mutates=False,
        notifies_others=False,
        scopes=["Calendars.ReadWrite"],
        args=[],
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
        id="calendar.tentative",
        cli=["blumkin", "calendar", "tentative"],
        summary='Respond "tentative" to calendar invitation(s); optional --comment/--propose-time',
        mutates=True,
        notifies_others=True,
        scopes=["Calendars.ReadWrite"],
        args=[
            {"name": "--event-id", "required": False, "type": "string"},
            {"name": "--today-pending", "required": False, "type": "flag"},
            {"name": "--comment", "required": False, "type": "string"},
            {
                "name": "--propose-time",
                "required": False,
                "type": "datetime",
                "note": "Microsoft only; fails closed on Google",
            },
            {"name": "--propose-duration", "required": False, "type": "duration"},
            {"name": "--tz", "required": False, "type": "iana_tz"},
            {"name": "--yes", "required": True, "type": "flag"},
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
            {"name": "--calendar", "required": False, "type": "string"},
            {"name": "--tz", "required": False, "type": "iana_tz"},
        ],
    ),
    SkillSpec(
        id="calendar.update",
        cli=["blumkin", "calendar", "update"],
        summary="Edit an existing event's fields (only the flags you pass are changed)",
        mutates=True,
        notifies_others=True,
        scopes=["Calendars.ReadWrite"],
        args=[
            {"name": "--event-id", "required": True, "type": "string"},
            {"name": "--subject", "required": False, "type": "string"},
            {"name": "--start", "required": False, "type": "datetime"},
            {"name": "--end", "required": False, "type": "datetime"},
            {"name": "--duration", "required": False, "type": "duration"},
            {
                "name": "--all-day",
                "required": False,
                "type": "flag",
                "note": "--no-all-day converts back to a timed event",
            },
            {"name": "--location", "required": False, "type": "string"},
            {"name": "--calendar", "required": False, "type": "string"},
            {"name": "--body", "required": False, "type": "string"},
            {"name": "--body-file", "required": False, "type": "path"},
            {
                "name": "--body-type",
                "required": False,
                "type": "enum",
                "values": ["html", "text"],
                "note": "Microsoft only",
            },
            {
                "name": "--with",
                "required": False,
                "type": "email",
                "multiple": True,
                "note": "replaces the attendee list",
            },
            {
                "name": "--no-teams",
                "required": False,
                "type": "flag",
                "note": "--teams attaches, --no-teams removes the online meeting; omit to leave it",
            },
            {"name": "--tz", "required": False, "type": "iana_tz"},
            {"name": "--yes", "required": True, "type": "flag"},
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
            {"name": "--calendar", "required": False, "type": "string"},
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
        summary=("Show the last N messages from one chat (exactly one of --with or --chat-id)"),
        mutates=False,
        notifies_others=False,
        scopes=["Chat.Read"],
        args=[
            {
                "name": "--chat-id",
                "required": False,
                "type": "string",
                "note": "exactly one of --with or --chat-id",
            },
            {
                "name": "--contains",
                "required": False,
                "type": "string",
                "note": "case-insensitive body filter over a local newest-first scan (max 500)",
            },
            {"name": "--n", "required": False, "type": "int"},
            {
                "name": "--with",
                "required": False,
                "type": "string",
                "note": "exactly one of --with or --chat-id; refuses if multiple matches",
            },
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
        id="mail.auto-reply",
        cli=["blumkin", "mail", "auto-reply"],
        summary=(
            "Read, set, or clear the automatic-reply / vacation responder "
            "(Microsoft needs wo1162425_scopes + MailboxSettings.ReadWrite; "
            "Google needs gmail.settings.basic)"
        ),
        mutates=True,
        notifies_others=False,
        scopes=["MailboxSettings.ReadWrite"],
        args=[
            {
                "name": "--on",
                "required": False,
                "type": "flag",
                "note": "--on/--off; omit both to read. --on needs --message or --message-file",
            },
            {"name": "--off", "required": False, "type": "flag"},
            {"name": "--message", "required": False, "type": "string"},
            {"name": "--message-file", "required": False, "type": "path"},
            {
                "name": "--external-message",
                "required": False,
                "type": "string",
                "note": "Microsoft only; Google has one response body",
            },
            {
                "name": "--external",
                "required": False,
                "type": "enum",
                "values": ["none", "contacts", "all"],
            },
            {"name": "--start", "required": False, "type": "date"},
            {"name": "--until", "required": False, "type": "date"},
            {
                "name": "--yes",
                "required": False,
                "type": "flag",
                "note": "required with --on/--off",
            },
        ],
    ),
    SkillSpec(
        id="mail.delete",
        cli=["blumkin", "mail", "delete"],
        summary="Move message(s) to Deleted Items / Trash (recoverable)",
        mutates=True,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[
            {"name": "--id", "required": True, "type": "string", "multiple": True},
            {"name": "--yes", "required": True, "type": "flag"},
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
            {
                "name": "--importance",
                "required": False,
                "type": "enum",
                "values": ["high", "normal", "low"],
                "note": "server-side; cannot be combined with --search",
            },
            {
                "name": "--has-attachments",
                "required": False,
                "type": "flag",
                "note": "server-side; cannot be combined with --search",
            },
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
            {
                "name": "--importance",
                "required": False,
                "type": "enum",
                "values": ["high", "normal", "low"],
                "note": "server-side; cannot be combined with --search",
            },
            {
                "name": "--has-attachments",
                "required": False,
                "type": "flag",
                "note": "server-side; cannot be combined with --search",
            },
            {"name": "--top", "required": False, "type": "int"},
            {"name": "--tz", "required": False, "type": "iana_tz"},
        ],
    ),
    SkillSpec(
        id="mail.mark",
        cli=["blumkin", "mail", "mark"],
        summary="Set read/unread, the follow-up flag, and/or importance on message(s)",
        mutates=True,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[
            {"name": "--id", "required": True, "type": "string", "multiple": True},
            {
                "name": "--read",
                "required": False,
                "type": "flag",
                "note": "--read / --unread; Gmail UNREAD label",
            },
            {
                "name": "--flag",
                "required": False,
                "type": "flag",
                "note": "--flag / --unflag; Gmail STARRED label",
            },
            {
                "name": "--importance",
                "required": False,
                "type": "enum",
                "values": ["high", "normal", "low"],
                "note": "Gmail IMPORTANT label",
            },
            {"name": "--yes", "required": True, "type": "flag"},
        ],
    ),
    SkillSpec(
        id="mail.move",
        cli=["blumkin", "mail", "move"],
        summary="Move message(s) to a folder / label (or archive = remove Inbox)",
        mutates=True,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[
            {"name": "--id", "required": True, "type": "string", "multiple": True},
            {
                "name": "--to",
                "required": True,
                "type": "string",
                "note": "well-known name, folder id, or Gmail label",
            },
            {"name": "--yes", "required": True, "type": "flag"},
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
        id="mail.search",
        cli=["blumkin", "mail", "search"],
        summary="Search the whole mailbox (every folder); tags each hit with its folder",
        mutates=False,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[
            {"name": "--query", "required": True, "type": "string"},
            {"name": "--since", "required": False, "type": "datetime"},
            {"name": "--until", "required": False, "type": "datetime"},
            {"name": "--top", "required": False, "type": "int", "note": "default 25"},
            {"name": "--tz", "required": False, "type": "iana_tz"},
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
        id="mail.signature",
        cli=["blumkin", "mail", "signature"],
        summary="Print the rendered [mail.signature] for the active profile (read-only)",
        mutates=False,
        notifies_others=False,
        scopes=[],
        args=[
            {
                "name": "--body-type",
                "required": False,
                "type": "enum",
                "values": ["html", "text"],
                "note": "default html",
            },
        ],
    ),
    SkillSpec(
        id="mail.thread",
        cli=["blumkin", "mail", "thread"],
        summary="List every message in the conversation a message belongs to, oldest first",
        mutates=False,
        notifies_others=False,
        scopes=["Mail.ReadWrite"],
        args=[
            {"name": "--id", "required": True, "type": "string"},
            {"name": "--full", "required": False, "type": "flag", "note": "include each body"},
            {
                "name": "--body-type",
                "required": False,
                "type": "enum",
                "values": ["html", "text"],
                "note": "with --full; defaults to text",
            },
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
            {
                "name": "--keep-quoted",
                "required": False,
                "type": "flag",
                "note": "re-append the quoted original from the existing draft",
            },
            {
                "name": "--no-signature",
                "required": False,
                "type": "flag",
                "note": "skip reapplying [mail.signature] when replacing the body",
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
        id="mcp.serve",
        cli=["blumkin", "mcp", "serve"],
        summary="Run blumkin as a Model Context Protocol stdio server for MCP-aware clients",
        mutates=False,
        notifies_others=False,
        scopes=[],
        args=[
            {"name": "--profile", "required": False, "type": "string"},
            {"name": "--read-only", "required": False, "type": "flag"},
            {"name": "--only", "required": False, "type": "string", "multiple": True},
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
            "(fail-closed on zero or multiple matches; requires wo1162425_scopes + "
            "People.Read)"
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


def _enrich_args() -> None:
    """Attach the resolved provider kwarg (``param``) and coercion hint to every arg.

    Runs once at import so ``describe_skill`` and ``skills_catalog`` agree. ``param``
    is the single source the dispatch layer and the MCP server read, and
    ``tests/test_skills_schema.py`` pins it against the live provider signatures.
    """
    for skill in SKILLS:
        bespoke = skill.id in BESPOKE_SKILLS
        for arg in skill.args:
            arg["param"] = resolve_arg_param(skill.id, arg)
            if bespoke:
                continue  # no provider method - a coerce hint would be meaningless
            coerce = _ARG_COERCE.get((skill.id, arg["name"]))
            if coerce is not None:
                arg["coerce"] = coerce


_enrich_args()


def describe_skill(skill_id: str) -> SkillSpec | None:
    for skill in SKILLS:
        if skill.id == skill_id:
            return skill
    return None


def skills_catalog() -> dict[str, Any]:
    package, commit = get_build_info()
    return {
        "build": {"commit": commit, "version": package},
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
