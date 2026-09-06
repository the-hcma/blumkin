"""Unit coverage for the shared exception classifier (``blumkin.skills.errors``)."""

from __future__ import annotations

from zoneinfo import ZoneInfoNotFoundError

import httpx

from blumkin.auth import AuthRequiredError, AuthTransientError, MissingScopeError, SecretWriteError
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
from blumkin.skills.chat import ChatAttachmentScopeError, ChatAttachmentSkippedError
from blumkin.skills.errors import (
    ConsentRequiredError,
    ScopeAddonDisabledError,
    classify_exception,
)
from blumkin.skills.mail import MailAttachError, MailMessageNotFoundError


class _FakeApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.response_status_code = status


def test_gate_exceptions_are_usage_errors_with_their_hint() -> None:
    info = classify_exception(ConsentRequiredError("--yes is required", hint="do it with --yes"))
    assert (info.slug, info.exit_code, info.hint) == ("usage_error", EXIT_USAGE, "do it with --yes")
    info = classify_exception(ScopeAddonDisabledError("off", hint="turn it on"))
    assert (info.slug, info.exit_code) == ("usage_error", EXIT_USAGE)


def test_zoneinfo_not_found_is_a_usage_error_despite_subclassing_lookuperror() -> None:
    info = classify_exception(ZoneInfoNotFoundError("No/Zone"))
    assert info.slug == "usage_error"
    assert info.exit_code == EXIT_USAGE
    assert info.message.startswith("invalid timezone:")
    assert "IANA" in (info.hint or "")


def test_not_found_and_bad_input_classes() -> None:
    assert classify_exception(CalendarEventNotFoundError("gone")).exit_code == EXIT_NOT_FOUND
    assert classify_exception(MailMessageNotFoundError("gone")).exit_code == EXIT_NOT_FOUND
    assert classify_exception(MailAttachError("too big")).slug == "usage_error"
    assert classify_exception(LookupError("no match")).exit_code == EXIT_NOT_FOUND


def test_calendar_resolution_classes() -> None:
    amb = classify_exception(CalendarAmbiguousError("two matched"))
    assert amb.slug == "usage_error" and "calendar list" in (amb.hint or "")
    assert classify_exception(CalendarNotFoundError("nope")).exit_code == EXIT_NOT_FOUND
    assert classify_exception(CalendarListTooLargeError("google")).slug == "graph_error"


def test_chat_and_scope_classes() -> None:
    assert classify_exception(ChatAttachmentScopeError("needs Files.Read")).slug == "missing_scope"
    assert classify_exception(ChatAttachmentSkippedError("v1")).slug == "usage_error"


def test_auth_layer_value_errors() -> None:
    assert classify_exception(ProviderConfigError("no client_id")).slug == "usage_error"
    missing = classify_exception(MissingScopeError("gap", current=frozenset(), missing=frozenset()))
    assert missing.slug == "missing_scope" and missing.hint
    assert classify_exception(AuthTransientError("blip")).slug == "transient_error"
    assert classify_exception(AuthRequiredError("login")).exit_code == EXIT_AUTH
    assert classify_exception(ValueError("Silent token refresh failed: x")).slug == "auth_required"
    assert classify_exception(ValueError("freebusy lookup failed: 500")).slug == "graph_error"
    assert classify_exception(ValueError("bad --flag")).slug == "usage_error"


def test_transport_and_http_status_ladder() -> None:
    assert classify_exception(SecretWriteError("cache")).slug == "secret_write_failed"
    assert classify_exception(httpx.TimeoutException("slow")).slug == "timeout"
    assert classify_exception(_FakeApiError(401)).exit_code == EXIT_AUTH
    assert classify_exception(_FakeApiError(403)).exit_code == EXIT_MISSING_SCOPE
    assert classify_exception(_FakeApiError(404)).exit_code == EXIT_NOT_FOUND
    fallback = classify_exception(_FakeApiError(500))
    assert (fallback.slug, fallback.exit_code) == ("graph_error", EXIT_OTHER)
