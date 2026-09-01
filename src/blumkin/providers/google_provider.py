"""Google Workspace provider (calendar + mail reads MVP)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from blumkin.config import BlumkinConfig
from blumkin.providers import google_auth
from blumkin.providers.google import calendar as google_calendar
from blumkin.providers.google import mail as google_mail
from blumkin.providers.google import mail_writes as google_mail_writes
from blumkin.providers.kind import ProviderKind


class GoogleWorkspaceProvider:
    """Delegates supported Google skills; unsupported ops fail closed."""

    def __init__(self, config: BlumkinConfig) -> None:
        self._config = config

    def auth_login(self) -> None:
        google_auth.login(self._config)

    def auth_logout(self) -> None:
        google_auth.logout(self._config)

    def auth_refresh(self) -> dict[str, Any]:
        return google_auth.refresh_silent(self._config)

    def auth_status(self) -> dict[str, Any]:
        return google_auth.status_dict(self._config)

    async def calendar_accept(
        self,
        *,
        event_id: str | None = None,
        today_pending: bool = False,
        tz_name: str | None = None,
    ) -> dict[str, Any]:
        return self._unsupported("calendar accept")

    async def calendar_cancel(self, *, event_id: str) -> dict[str, Any]:
        return self._unsupported("calendar cancel")

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
    ) -> dict[str, Any]:
        return await google_calendar.calendar_create(
            subject=subject,
            with_emails=with_emails,
            start_raw=start_raw,
            duration=duration,
            remind_email=remind_email,
            tz_name=tz_name,
            config=self._config,
        )

    async def calendar_freebusy(
        self,
        *,
        with_emails: list[str],
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        return await google_calendar.calendar_freebusy(
            with_emails=with_emails,
            start=start,
            end=end,
            config=self._config,
        )

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
    ) -> dict[str, Any]:
        return await google_calendar.calendar_suggest(
            with_emails=with_emails,
            start=start,
            end=end,
            duration=duration,
            window=window,
            treat_tentative=treat_tentative,
            step=step,
            limit=limit,
            config=self._config,
        )

    async def calendar_today(
        self,
        *,
        day: date | None = None,
        tz_name: str | None = None,
    ) -> dict[str, Any]:
        return await google_calendar.calendar_today(day=day, tz_name=tz_name, config=self._config)

    async def calendar_update(
        self,
        *,
        event_id: str,
        teams: bool = True,
        tz_name: str | None = None,
    ) -> dict[str, Any]:
        return self._unsupported("calendar update")

    async def calendar_view(
        self,
        *,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        return await google_calendar.calendar_view(start=start, end=end, config=self._config)

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
    ) -> dict[str, Any]:
        return self._unsupported("chat attachments download")

    async def chat_attachments_list(
        self,
        *,
        chat_id: str | None = None,
        latest: bool = False,
        message_id: str | None = None,
        with_name: str | None = None,
    ) -> dict[str, Any]:
        return self._unsupported("chat attachments list")

    async def chat_delete(self, *, chat_id: str, message_id: str) -> dict[str, Any]:
        return self._unsupported("chat delete")

    async def chat_edit(self, *, chat_id: str, message_id: str, text: str) -> dict[str, Any]:
        return self._unsupported("chat edit")

    async def chat_find(self, *, with_name: str) -> dict[str, Any]:
        return self._unsupported("chat find")

    async def chat_last(self, *, with_name: str, n: int = 3) -> dict[str, Any]:
        return self._unsupported("chat last")

    async def chat_send(
        self,
        *,
        text: str,
        with_name: str | None = None,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        return self._unsupported("chat send")

    @property
    def kind(self) -> ProviderKind:
        return ProviderKind.GOOGLE

    async def mail_attachments_download(
        self,
        *,
        message_id: str,
        out: str,
        attachment_id: str | None = None,
        download_all: bool = False,
    ) -> dict[str, Any]:
        return self._unsupported("mail attachments download")

    async def mail_attachments_list(self, *, message_id: str) -> dict[str, Any]:
        return self._unsupported("mail attachments list")

    async def mail_delete_draft(self, *, draft_id: str) -> dict[str, Any]:
        return await google_mail_writes.mail_delete_draft(draft_id=draft_id, config=self._config)

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
    ) -> dict[str, Any]:
        return await google_mail_writes.mail_draft(
            to=to,
            subject=subject,
            attach=attach,
            bcc=bcc,
            body=body,
            body_file=body_file,
            body_type=body_type,
            cc=cc,
            no_signature=no_signature,
            config=self._config,
        )

    async def mail_folders(self) -> dict[str, Any]:
        return self._unsupported("mail folders")

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
    ) -> dict[str, Any]:
        return await google_mail_writes.mail_forward(
            message_id=message_id,
            to=to,
            body=body,
            body_file=body_file,
            body_type=body_type,
            bcc=bcc,
            cc=cc,
            no_signature=no_signature,
            config=self._config,
        )

    async def mail_get(self, *, message_id: str, body_type: str = "text") -> dict[str, Any]:
        return await google_mail.mail_get(
            message_id=message_id, body_type=body_type, config=self._config
        )

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
    ) -> dict[str, Any]:
        if has_attachments or importance is not None:
            return self._unsupported("mail --has-attachments/--importance filters")
        return await google_mail.mail_inbox(
            top=top,
            search=search,
            sender=sender,
            since=since,
            subject=subject,
            unread=unread,
            until=until,
            config=self._config,
        )

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
    ) -> dict[str, Any]:
        if has_attachments or importance is not None:
            return self._unsupported("mail --has-attachments/--importance filters")
        return await google_mail.mail_list(
            top=top,
            folder=folder,
            orderby=orderby,
            search=search,
            sender=sender,
            since=since,
            subject=subject,
            unread=unread,
            until=until,
            config=self._config,
        )

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
    ) -> dict[str, Any]:
        return await google_mail_writes.mail_reply(
            message_id=message_id,
            body=body,
            body_file=body_file,
            body_type=body_type,
            bcc=bcc,
            cc=cc,
            reply_all=reply_all,
            no_signature=no_signature,
            config=self._config,
        )

    async def mail_send_draft(self, *, draft_id: str) -> dict[str, Any]:
        return await google_mail_writes.mail_send_draft(draft_id=draft_id, config=self._config)

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
    ) -> dict[str, Any]:
        return await google_mail_writes.mail_update_draft(
            draft_id=draft_id,
            attach=attach,
            bcc=bcc,
            subject=subject,
            body=body,
            body_file=body_file,
            body_type=body_type,
            cc=cc,
            to=to,
            config=self._config,
        )

    async def meeting_get(self, *, event_id: str) -> dict[str, Any]:
        return self._unsupported("meeting get")

    async def meeting_transcription(self, *, event_id: str, enable: bool = False) -> dict[str, Any]:
        return self._unsupported("meeting transcription")

    async def people_resolve(
        self,
        *,
        name: str | None = None,
        email: str | None = None,
        top: int = 10,
    ) -> dict[str, Any]:
        return self._unsupported("people resolve")

    def _unsupported(self, op: str) -> dict[str, Any]:
        raise ValueError(f"{op} not supported for provider=google yet")
