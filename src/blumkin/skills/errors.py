"""One shared exception -> error-slug classifier for the CLI and the MCP server.

Every non-zero exit blumkin can produce comes from :func:`classify_exception`. The
CLI turns the :class:`ErrorInfo` into ``emit_error`` + ``SystemExit``; the MCP
server turns it into an ``isError`` tool result. Keeping the ladder in one place
is what stops the two surfaces from drifting.
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfoNotFoundError

import httpx

from blumkin.auth import (
    AuthRequiredError,
    AuthTransientError,
    MissingScopeError,
    SecretWriteError,
)
from blumkin.exit_codes import (
    EXIT_AUTH,
    EXIT_MISSING_SCOPE,
    EXIT_NOT_FOUND,
    EXIT_OTHER,
    EXIT_USAGE,
)
from blumkin.providers.kind import ProviderConfigError
from blumkin.skills.calendar import (
    CalendarAmbiguousError,
    CalendarEventNotFoundError,
    CalendarListTooLargeError,
    CalendarNotFoundError,
)
from blumkin.skills.chat import (
    ChatAttachmentNotFoundError,
    ChatAttachmentScopeError,
    ChatAttachmentSkippedError,
    ChatDraftNotFoundError,
    ChatMessageNotFoundError,
)
from blumkin.skills.docs_read import DocsReadFileNotFoundError
from blumkin.skills.mail import (
    MailAttachError,
    MailAttachmentNotFoundError,
    MailAttachmentSkippedError,
    MailBodyFileError,
    MailDraftNotFoundError,
    MailFolderNotFoundError,
    MailMessageNotFoundError,
)
from blumkin.tasks import TaskAmbiguousError, TaskConflictError

# Overrides the Microsoft/Graph-only ``missing_scope`` default hint: a plain
# MissingScopeError is provider-neutral (issue #133).
_MISSING_SCOPE_HINT = (
    "Run `blumkin auth login` on a TTY and tick every scope box (or click "
    '"Select all") on the consent screen - the message above lists exactly '
    "which scopes are missing."
)
_CALENDAR_AMBIGUOUS_HINT = (
    "Pass the calendar id (from `blumkin calendar list --json`), not the name."
)
_TZ_HINT = "Use an IANA name like America/New_York or UTC (not an abbreviation)."


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    slug: str
    exit_code: int
    message: str
    hint: str | None = None
    # Only set on a `too_soon` cooldown rejection (issue #365).
    agent_instructions: str | None = None
    retry_after_seconds: float | None = None


class ConsentRequiredError(ValueError):
    """A notify/mutate skill was called without ``--yes`` / ``confirm``."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class EmitCooldownError(ValueError):
    """An `emit` skill was called before its composed artifact's cooldown elapsed.

    See ``blumkin.compose_state`` and issue #365: this is the code-enforced half
    of "always confirm with the user before a notifying send" - a bare
    ``--yes`` / ``confirm: true`` proves nothing about whether the agent
    actually showed the human what is about to go out.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float,
        artifact_summary: str = "recipients/subject/body or message text",
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.agent_instructions = (
            f"Do not retry automatically. Show the user the exact composed content "
            f"({artifact_summary}) now, in this turn, and wait for their explicit "
            "go-ahead before calling emit again. Retrying immediately or silently "
            "without a real user confirmation is a policy violation."
        )


class FreshnessRequiredError(ValueError):
    """`calendar.accept` / `decline` / `tentative` / `cancel` was called on an
    event id that was not just freshly read with `calendar.get`.

    See ``blumkin.read_state`` and issue #365: acting on an id read (or never
    read) too long ago risks approving, declining, or cancelling based on
    stale attendees, a since-moved time, or a since-cancelled event - the
    RSVP/cancel equivalent of the emit cooldown, gated on a *read* instead of
    a *compose*.
    """

    def __init__(self, message: str, *, event_id: str) -> None:
        super().__init__(message)
        self.agent_instructions = (
            f"Do not retry automatically. Call `calendar.get --event-id {event_id}` "
            "now, show the user the event's current subject/time/attendees/status, "
            "and only then retry this action. Acting on event data that was not "
            "just freshly read is a policy violation."
        )


class ScopeAddonDisabledError(ValueError):
    """A skill needs the WO1162425 add-on scopes but ``wo1162425_scopes`` is off."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


def graph_http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status from a kiota/msgraph or googleapiclient exception."""
    for attr in ("response_status_code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            value = getattr(response, attr, None)
            if isinstance(value, int):
                return value
    return None


def classify_exception(exc: BaseException) -> ErrorInfo:  # noqa: PLR0911 - a flat ladder
    """Map any blumkin exception to its ``(slug, exit_code, message, hint)``."""
    # 0. typed gate exceptions from the dispatch layer
    if isinstance(exc, EmitCooldownError):
        return ErrorInfo(
            "too_soon",
            EXIT_USAGE,
            str(exc),
            agent_instructions=exc.agent_instructions,
            retry_after_seconds=exc.retry_after_seconds,
        )
    if isinstance(exc, FreshnessRequiredError):
        return ErrorInfo(
            "stale_or_unread",
            EXIT_USAGE,
            str(exc),
            agent_instructions=exc.agent_instructions,
        )
    if isinstance(exc, ConsentRequiredError | ScopeAddonDisabledError):
        return ErrorInfo("usage_error", EXIT_USAGE, str(exc), exc.hint)

    # Task-template errors carry operator text (`--name`, template names, file
    # paths) - classify by type, before the ValueError message heuristics below
    # could re-read that text as `auth_required`.
    if isinstance(exc, TaskAmbiguousError | TaskConflictError):
        return ErrorInfo("usage_error", EXIT_USAGE, str(exc))

    # ZoneInfoNotFoundError subclasses KeyError/LookupError, so it must be
    # classified before the generic LookupError branch below.
    if isinstance(exc, ZoneInfoNotFoundError):
        return ErrorInfo("usage_error", EXIT_USAGE, f"invalid timezone: {exc}", _TZ_HINT)
    if isinstance(exc, DocsReadFileNotFoundError):
        return ErrorInfo("not_found", EXIT_NOT_FOUND, str(exc))

    # 1. per-verb "not found" / bad-input classes that the CLI catches ahead of the ladder
    if isinstance(
        exc,
        CalendarEventNotFoundError
        | MailFolderNotFoundError
        | MailMessageNotFoundError
        | MailDraftNotFoundError
        | MailAttachmentNotFoundError,
    ):
        # MailFolderNotFoundError / MailMessageNotFoundError carry an optional
        # `hint` for the specific-wrong-call patterns (issue #314); the other
        # `not_found` classes here have none, so fall back to the CLI's generic
        # not_found hint via getattr.
        return ErrorInfo("not_found", EXIT_NOT_FOUND, str(exc), getattr(exc, "hint", None))
    if isinstance(exc, MailAttachError | MailBodyFileError | MailAttachmentSkippedError):
        return ErrorInfo("usage_error", EXIT_USAGE, str(exc))

    # 2. --calendar name resolution
    if isinstance(exc, CalendarAmbiguousError):
        return ErrorInfo("usage_error", EXIT_USAGE, str(exc), _CALENDAR_AMBIGUOUS_HINT)
    if isinstance(exc, CalendarNotFoundError):
        return ErrorInfo("not_found", EXIT_NOT_FOUND, str(exc))
    if isinstance(exc, CalendarListTooLargeError):
        return ErrorInfo("graph_error", EXIT_OTHER, str(exc))

    # 3. chat attachments
    if isinstance(exc, ChatAttachmentScopeError):
        return ErrorInfo("missing_scope", EXIT_MISSING_SCOPE, str(exc))
    if isinstance(
        exc,
        ChatAttachmentNotFoundError
        | ChatDraftNotFoundError
        | ChatMessageNotFoundError
        | LookupError,
    ):
        return ErrorInfo("not_found", EXIT_NOT_FOUND, str(exc))
    if isinstance(exc, ChatAttachmentSkippedError):
        return ErrorInfo("usage_error", EXIT_USAGE, str(exc))

    # 4. secret write / HTTP timeout
    if isinstance(exc, SecretWriteError):
        return ErrorInfo("secret_write_failed", EXIT_OTHER, str(exc))
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return ErrorInfo("timeout", EXIT_OTHER, str(exc) or "Graph or token HTTP call timed out")

    # 5. auth-layer ValueError classification
    if isinstance(exc, ProviderConfigError):
        return ErrorInfo("usage_error", EXIT_USAGE, str(exc))
    if isinstance(exc, MissingScopeError):
        return ErrorInfo("missing_scope", EXIT_MISSING_SCOPE, str(exc), _MISSING_SCOPE_HINT)
    if isinstance(exc, AuthTransientError):
        return ErrorInfo("transient_error", EXIT_OTHER, str(exc))
    if isinstance(exc, AuthRequiredError):
        return ErrorInfo("auth_required", EXIT_AUTH, str(exc))
    if isinstance(exc, ValueError):
        msg = str(exc)
        if msg.startswith("freebusy lookup failed"):
            return ErrorInfo("graph_error", EXIT_OTHER, msg)
        if (
            "client_id" in msg
            or "Missing" in msg
            or msg.startswith("Authentication required")
            or msg.startswith("Silent token refresh failed")
        ):
            return ErrorInfo("auth_required", EXIT_AUTH, msg)
        return ErrorInfo("usage_error", EXIT_USAGE, msg)

    # 6. Graph HTTP status ladder
    status = graph_http_status(exc)
    if status == 401:
        return ErrorInfo("auth_required", EXIT_AUTH, str(exc))
    if status == 403:
        return ErrorInfo("missing_scope", EXIT_MISSING_SCOPE, str(exc))
    if status == 404:
        return ErrorInfo("not_found", EXIT_NOT_FOUND, str(exc))
    return ErrorInfo("graph_error", EXIT_OTHER, str(exc))
