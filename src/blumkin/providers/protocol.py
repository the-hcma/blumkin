"""Abstract workspace provider surface (skill-shaped payloads)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from blumkin.providers.kind import ProviderKind


class WorkspaceProvider(Protocol):
    """Provider-agnostic ops used by the CLI.

    Implementations return the same ``dict`` payloads as today's skill modules so
    ``--json`` schemas stay stable when a Google adapter is added later.
    """

    @property
    def kind(self) -> ProviderKind: ...

    def account_email(self) -> str: ...

    def auth_login(self) -> None: ...

    def auth_logout(self) -> None: ...

    def auth_refresh(self) -> dict[str, Any]: ...

    def auth_status(self) -> dict[str, Any]: ...

    async def calendar_accept(
        self,
        *,
        event_id: str | None = None,
        today_pending: bool = False,
        tz_name: str | None = None,
    ) -> dict[str, Any]: ...

    async def calendar_cancel(self, *, event_id: str) -> dict[str, Any]: ...

    async def calendar_create(
        self,
        *,
        subject: str,
        with_emails: list[str],
        start_raw: str,
        duration: str | None = None,
        remind_email: str | None = None,
        teams: bool = True,
        tz_name: str | None = None,
    ) -> dict[str, Any]: ...

    async def calendar_freebusy(
        self,
        *,
        with_emails: list[str],
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]: ...

    async def calendar_suggest(
        self,
        *,
        with_emails: list[str],
        start: datetime,
        end: datetime,
        duration: timedelta,
        window: str | None = None,
        treat_tentative: str = "busy",
        step: timedelta | None = None,
        limit: int = 10,
    ) -> dict[str, Any]: ...

    async def calendar_today(
        self,
        *,
        day: date | None = None,
        tz_name: str | None = None,
    ) -> dict[str, Any]: ...

    async def calendar_update(
        self,
        *,
        event_id: str,
        teams: bool = True,
        tz_name: str | None = None,
    ) -> dict[str, Any]: ...

    async def calendar_view(
        self,
        *,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]: ...

    async def chat_attachments_download(
        self,
        *,
        out: str,
        attachment_id: str | None = None,
        chat_id: str | None = None,
        download_all: bool = False,
        latest: bool = False,
        message_id: str | None = None,
        with_name: str | None = None,
    ) -> dict[str, Any]: ...

    async def chat_attachments_list(
        self,
        *,
        chat_id: str | None = None,
        latest: bool = False,
        message_id: str | None = None,
        with_name: str | None = None,
    ) -> dict[str, Any]: ...

    async def chat_delete(self, *, chat_id: str, message_id: str) -> dict[str, Any]: ...

    async def chat_edit(self, *, chat_id: str, message_id: str, text: str) -> dict[str, Any]: ...

    async def chat_find(self, *, with_name: str) -> dict[str, Any]: ...

    async def chat_last(
        self,
        *,
        with_name: str | None = None,
        chat_id: str | None = None,
        contains: str | None = None,
        n: int = 3,
    ) -> dict[str, Any]: ...

    async def chat_send(
        self,
        *,
        text: str,
        with_name: str | None = None,
        chat_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def mail_attachments_download(
        self,
        *,
        message_id: str,
        out: str,
        attachment_id: str | None = None,
        download_all: bool = False,
    ) -> dict[str, Any]: ...

    async def mail_attachments_list(self, *, message_id: str) -> dict[str, Any]: ...

    async def mail_delete_draft(self, *, draft_id: str) -> dict[str, Any]: ...

    async def mail_draft(
        self,
        *,
        to: str | Sequence[str],
        subject: str,
        attach: Sequence[str] = (),
        bcc: str | Sequence[str] = (),
        body: str | None = None,
        body_file: str | None = None,
        body_type: str = "text",
        cc: str | Sequence[str] = (),
        no_signature: bool = False,
    ) -> dict[str, Any]: ...

    async def mail_folders(self) -> dict[str, Any]: ...

    async def mail_forward(
        self,
        *,
        message_id: str,
        to: str,
        body: str | None = None,
        body_file: str | None = None,
        body_type: str = "text",
        bcc: str | Sequence[str] | None = None,
        cc: str | Sequence[str] | None = None,
        no_signature: bool = False,
    ) -> dict[str, Any]: ...

    async def mail_get(self, *, message_id: str, body_type: str = "text") -> dict[str, Any]: ...

    async def mail_inbox(
        self,
        *,
        top: int = 10,
        has_attachments: bool = False,
        importance: str | None = None,
        search: str | None = None,
        sender: str | None = None,
        since: datetime | None = None,
        subject: str | None = None,
        unread: bool = False,
        until: datetime | None = None,
    ) -> dict[str, Any]: ...

    async def mail_list(
        self,
        *,
        top: int = 10,
        folder: str | None = None,
        has_attachments: bool = False,
        importance: str | None = None,
        orderby: str | None = None,
        search: str | None = None,
        sender: str | None = None,
        since: datetime | None = None,
        subject: str | None = None,
        unread: bool = False,
        until: datetime | None = None,
    ) -> dict[str, Any]: ...

    async def mail_reply(
        self,
        *,
        message_id: str,
        body: str | None = None,
        body_file: str | None = None,
        body_type: str = "text",
        bcc: str | Sequence[str] | None = None,
        cc: str | Sequence[str] | None = None,
        reply_all: bool = False,
        no_signature: bool = False,
    ) -> dict[str, Any]: ...

    async def mail_send_draft(self, *, draft_id: str) -> dict[str, Any]: ...

    async def mail_update_draft(
        self,
        *,
        draft_id: str,
        attach: Sequence[str] = (),
        bcc: str | Sequence[str] | None = None,
        subject: str | None = None,
        body: str | None = None,
        body_file: str | None = None,
        body_type: str = "text",
        cc: str | Sequence[str] | None = None,
        to: str | Sequence[str] | None = None,
    ) -> dict[str, Any]: ...

    async def meeting_get(self, *, event_id: str) -> dict[str, Any]: ...

    async def meeting_transcription(
        self, *, event_id: str, enable: bool = False
    ) -> dict[str, Any]: ...

    async def people_resolve(
        self,
        *,
        name: str | None = None,
        email: str | None = None,
        top: int = 10,
    ) -> dict[str, Any]: ...
