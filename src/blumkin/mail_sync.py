"""Shared types for incremental mail sync (issue #525).

A provider's `mail_new` returns the messages that arrived since an opaque sync token and the
token to use next time. It raises `MailSyncResetError` when the token is no longer valid (an
expired Graph delta state, a stale Gmail history id), and the caller then starts over with one
bounded scan and a fresh token.
"""

from __future__ import annotations


class MailSyncResetError(RuntimeError):
    """The sync token is expired or unusable; take a fresh one and rescan."""


class MailSyncUnsupportedError(RuntimeError):
    """The provider has no incremental mail sync."""
