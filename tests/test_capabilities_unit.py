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
    """meeting also needs Calendars.ReadWrite: meeting.get/transcription resolve
    the calendar event first (skills/__init__.py declares both scopes)."""
    granted = ["Calendars.ReadWrite", "OnlineMeetings.ReadWrite", "People.Read"]
    summary = capability_summary(provider=ProviderKind.MICROSOFT, granted_scopes=granted)
    assert summary["meeting"] is True
    assert summary["people"] is True


def test_capability_summary_meeting_needs_calendar_scope_too() -> None:
    granted = ["OnlineMeetings.ReadWrite"]
    summary = capability_summary(provider=ProviderKind.MICROSOFT, granted_scopes=granted)
    assert summary["meeting"] is False


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


def test_capability_summary_google_readonly_grant_still_unlocks_mail_and_calendar() -> None:
    """gmail.readonly-only / calendar.readonly-only is a supported admin-restricted
    Workspace state (google_auth.py) where `mail list`/`calendar today` still work -
    baselining on the write-capable constants would wrongly report unavailable."""
    granted = [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]
    summary = capability_summary(provider=ProviderKind.GOOGLE, granted_scopes=granted)
    assert summary["mail"] is True
    assert summary["calendar"] is True


def test_capability_summary_google_drive_scope_alone_unlocks_only_drive() -> None:
    summary = capability_summary(
        provider=ProviderKind.GOOGLE,
        granted_scopes=["https://www.googleapis.com/auth/drive"],
    )
    assert summary["drive"] is True
    assert summary["docs"] is False


def test_capability_summary_google_docs_scopes_alone_unlock_only_docs() -> None:
    summary = capability_summary(
        provider=ProviderKind.GOOGLE,
        granted_scopes=[
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/drive.file",
        ],
    )
    assert summary["docs"] is True
    assert summary["drive"] is False


def test_capability_summary_google_people_scope_unlocks_people() -> None:
    summary = capability_summary(
        provider=ProviderKind.GOOGLE,
        granted_scopes=["https://www.googleapis.com/auth/contacts.readonly"],
    )
    assert summary["people"] is True


def test_capability_summary_preserves_family_display_order() -> None:
    summary = capability_summary(provider=ProviderKind.MICROSOFT, granted_scopes=[])
    assert tuple(summary) == CAPABILITY_FAMILIES


def test_microsoft_family_scopes_stay_within_the_declared_scope_sets() -> None:
    """Anti-drift guard: every scope `_MICROSOFT_FAMILY_SCOPES` hardcodes must
    come from auth.py's own requested-scope constants, so a rename there (or a
    typo in the table) fails loudly instead of silently reporting `False`."""
    from blumkin.auth import BASE_SCOPES, DOCS_SCOPES, WO1162425_SCOPES
    from blumkin.capabilities import _MICROSOFT_FAMILY_SCOPES

    base = set(BASE_SCOPES)
    docs = set(DOCS_SCOPES)
    wo1162425 = set(WO1162425_SCOPES)
    assert _MICROSOFT_FAMILY_SCOPES["mail"] <= base
    assert _MICROSOFT_FAMILY_SCOPES["calendar"] <= base
    assert _MICROSOFT_FAMILY_SCOPES["chat"] <= base
    assert _MICROSOFT_FAMILY_SCOPES["docs"] <= docs
    assert _MICROSOFT_FAMILY_SCOPES["drive"] <= docs
    assert _MICROSOFT_FAMILY_SCOPES["people"] <= wo1162425
    assert _MICROSOFT_FAMILY_SCOPES["meeting"] <= base | wo1162425


def test_microsoft_meeting_family_matches_the_skills_catalog() -> None:
    """meeting.get / meeting.transcription both declare the same scopes in the
    skills catalog - the family table must not fall behind that declaration."""
    from blumkin.capabilities import _MICROSOFT_FAMILY_SCOPES
    from blumkin.skills import SKILLS

    meeting_get = next(s for s in SKILLS if s.id == "meeting.get")
    meeting_transcription = next(s for s in SKILLS if s.id == "meeting.transcription")
    assert _MICROSOFT_FAMILY_SCOPES["meeting"] == frozenset(meeting_get.scopes)
    assert _MICROSOFT_FAMILY_SCOPES["meeting"] == frozenset(meeting_transcription.scopes)
