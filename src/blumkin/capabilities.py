"""Per-profile capability summary: which service families are usable right now.

Derives boolean flags from the granted-scope data ``auth.status_dict`` /
``google_auth.status_dict`` already compute for ``doctor`` / ``auth status`` -
no new Graph/Google calls. This is the single helper shared by `doctor --json`
(issue #313), `blumkin capabilities --json` (issue #315), and
`auth status --json` (issue #316), so capability derivation lives in exactly
one place instead of three.
"""

from __future__ import annotations

from collections.abc import Iterable

from blumkin.providers.google_auth import (
    CALENDAR_SCOPES as _GOOGLE_CALENDAR_SCOPES,
)
from blumkin.providers.google_auth import (
    CHAT_READ_SCOPES as _GOOGLE_CHAT_SCOPES,
)
from blumkin.providers.google_auth import (
    DOCS_SCOPES as _GOOGLE_DOCS_SCOPES,
)
from blumkin.providers.google_auth import (
    DRIVE_SCOPES as _GOOGLE_DRIVE_SCOPES,
)
from blumkin.providers.google_auth import (
    MAIL_WRITE_SCOPES as _GOOGLE_MAIL_SCOPES,
)
from blumkin.providers.google_auth import (
    PEOPLE_SCOPES as _GOOGLE_PEOPLE_SCOPES,
)
from blumkin.providers.kind import ProviderKind

# Every service family `doctor --json` / `capabilities --json` report on, in
# display order. `tasks` is always usable (local markdown, no scope - see
# capability_summary below); `meeting` is Microsoft-only (Google Meet
# get/transcription is intentionally unimplemented, docs/DECISIONS.md D8).
CAPABILITY_FAMILIES: tuple[str, ...] = (
    "mail",
    "calendar",
    "chat",
    "tasks",
    "docs",
    "drive",
    "people",
    "meeting",
)

# Microsoft Graph scopes needed for a family's baseline commands to work,
# mirroring auth.py's BASE_SCOPES / DOCS_SCOPES / FILES_SCOPES / WO1162425_SCOPES
# and the per-skill `scopes` declared in skills/__init__.py's SKILLS catalog
# (drive.* and docs.* both declare Files.ReadWrite there - one Microsoft toggle
# covers both families, unlike Google's separate drive/docs scopes below).
_MICROSOFT_FAMILY_SCOPES: dict[str, frozenset[str]] = {
    "calendar": frozenset({"Calendars.ReadWrite"}),
    "chat": frozenset({"Chat.Read"}),
    "docs": frozenset({"Files.ReadWrite"}),
    "drive": frozenset({"Files.ReadWrite"}),
    "mail": frozenset({"Mail.ReadWrite", "Mail.Send"}),
    "meeting": frozenset({"OnlineMeetings.ReadWrite"}),
    "people": frozenset({"People.Read"}),
}

# Google scopes needed for a family's baseline commands - reusing the exact
# per-skill-area constants google_auth.py already defines for its own
# fail-fast gates, so this never drifts from what the provider actually checks.
_GOOGLE_FAMILY_SCOPES: dict[str, frozenset[str]] = {
    "calendar": frozenset(_GOOGLE_CALENDAR_SCOPES),
    "chat": frozenset(_GOOGLE_CHAT_SCOPES),
    "docs": frozenset(_GOOGLE_DOCS_SCOPES),
    "drive": frozenset(_GOOGLE_DRIVE_SCOPES),
    "mail": frozenset(_GOOGLE_MAIL_SCOPES),
    "people": frozenset(_GOOGLE_PEOPLE_SCOPES),
}


def capability_summary(*, provider: ProviderKind, granted_scopes: Iterable[str]) -> dict[str, bool]:
    """Per-family usability flags, in ``CAPABILITY_FAMILIES`` order.

    A family is ``True`` when every scope its baseline commands need is already
    in ``granted_scopes`` (empty on a never-logged-in profile, so every
    scope-gated family reports ``False`` until ``blumkin auth login``).
    """
    granted = frozenset(granted_scopes)
    family_scopes = (
        _MICROSOFT_FAMILY_SCOPES if provider == ProviderKind.MICROSOFT else _GOOGLE_FAMILY_SCOPES
    )
    summary = {
        family: family_scopes[family] <= granted
        for family in CAPABILITY_FAMILIES
        if family in family_scopes
    }
    summary["tasks"] = True
    if provider == ProviderKind.GOOGLE:
        summary["meeting"] = False
    return {family: summary[family] for family in CAPABILITY_FAMILIES}
