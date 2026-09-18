"""`capability_summary` - the shared per-family usability helper (issue #313)."""

from __future__ import annotations

from blumkin.capabilities import CAPABILITY_FAMILIES, capability_summary
from blumkin.providers.kind import ProviderKind


def test_capability_summary_is_all_false_for_a_fresh_microsoft_profile() -> None:
    summary = capability_summary(provider=ProviderKind.MICROSOFT, granted_scopes=[])
    assert set(summary) == set(CAPABILITY_FAMILIES)
    assert summary["tasks"] is True
    for family in CAPABILITY_FAMILIES:
        if family != "tasks":
            assert summary[family] is False, family


def test_capability_summary_reports_the_base_microsoft_grant() -> None:
    granted = ["Calendars.ReadWrite", "Chat.Read", "Mail.ReadWrite", "Mail.Send", "User.Read"]
    summary = capability_summary(provider=ProviderKind.MICROSOFT, granted_scopes=granted)
    assert summary["mail"] is True
    assert summary["calendar"] is True
    assert summary["chat"] is True
    assert summary["tasks"] is True
    assert summary["docs"] is False
    assert summary["drive"] is False
    assert summary["people"] is False
    assert summary["meeting"] is False


def test_capability_summary_unlocks_docs_and_drive_together_on_files_readwrite() -> None:
    granted = ["Files.ReadWrite"]
    summary = capability_summary(provider=ProviderKind.MICROSOFT, granted_scopes=granted)
    assert summary["docs"] is True
    assert summary["drive"] is True


def test_capability_summary_unlocks_wo1162425_families() -> None:
    granted = ["OnlineMeetings.ReadWrite", "People.Read"]
    summary = capability_summary(provider=ProviderKind.MICROSOFT, granted_scopes=granted)
    assert summary["meeting"] is True
    assert summary["people"] is True


def test_capability_summary_google_meeting_is_always_false() -> None:
    """Google Meet get/transcription is intentionally unimplemented (D8)."""
    summary = capability_summary(provider=ProviderKind.GOOGLE, granted_scopes=[])
    assert summary["meeting"] is False


def test_capability_summary_google_reports_the_base_grant() -> None:
    granted = [
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/chat.memberships.readonly",
        "https://www.googleapis.com/auth/chat.messages.readonly",
        "https://www.googleapis.com/auth/chat.spaces.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]
    summary = capability_summary(provider=ProviderKind.GOOGLE, granted_scopes=granted)
    assert summary["calendar"] is True
    assert summary["chat"] is True
    assert summary["mail"] is True
    assert summary["tasks"] is True
    assert summary["docs"] is False
    assert summary["drive"] is False
    assert summary["people"] is False
    assert summary["meeting"] is False


def test_capability_summary_preserves_family_display_order() -> None:
    summary = capability_summary(provider=ProviderKind.MICROSOFT, granted_scopes=[])
    assert tuple(summary) == CAPABILITY_FAMILIES
