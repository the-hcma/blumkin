"""Microsoft 365 / Graph workspace provider (current concrete backend)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from blumkin.auth import create_credential, logout, refresh_silent, save_token_cache, status_dict
from blumkin.config import BlumkinConfig
from blumkin.providers.kind import ProviderKind
from blumkin.skills.calendar import (
    calendar_freebusy,
    calendar_suggest,
    calendar_today,
    calendar_view,
)
from blumkin.skills.calendar_writes import (
    calendar_accept,
    calendar_cancel,
    calendar_create,
    calendar_update,
)
from blumkin.skills.chat import (
    chat_attachments_download,
    chat_attachments_list,
    chat_delete,
    chat_edit,
    chat_find,
    chat_last,
    chat_send,
)
from blumkin.skills.mail import (
    mail_attachments_download,
    mail_attachments_list,
    mail_delete_draft,
    mail_draft,
    mail_folders,
    mail_forward,
    mail_get,
    mail_inbox,
    mail_list,
    mail_reply,
    mail_send_draft,
    mail_update_draft,
)
from blumkin.skills.meeting import meeting_get, meeting_transcription
from blumkin.skills.people import people_resolve


class MicrosoftWorkspaceProvider:
    """Delegates to existing Graph skill modules with a bound config."""

    def __init__(self, config: BlumkinConfig) -> None:
        self._config = config

    @property
    def kind(self) -> ProviderKind:
        return ProviderKind.MICROSOFT

    def auth_login(self) -> None:
        create_credential(self._config, allow_interactive=True)
        save_token_cache(self._config)

    def auth_logout(self) -> None:
        logout(self._config)

    def auth_refresh(self) -> dict[str, Any]:
        return refresh_silent(self._config)

    def auth_status(self) -> dict[str, Any]:
        return status_dict(self._config)

    async def calendar_accept(
        self,
        *,
        event_id: str | None = None,
        today_pending: bool = False,
        tz_name: str | None = None,
    ) -> dict[str, Any]:
        return await calendar_accept(
            event_id=event_id,
            today_pending=today_pending,
            tz_name=tz_name,
            config=self._config,
        )

    async def calendar_cancel(self, *, event_id: str) -> dict[str, Any]:
        return await calendar_cancel(event_id=event_id, config=self._config)

    async def calendar_create(
        self,
        *,
        subject: str,
        with_emails: list[str],
        start_raw: str,
        duration: str | None = None,
        teams: bool = True,
        tz_name: str | None = None,
    ) -> dict[str, Any]:
        return await calendar_create(
            subject=subject,
            with_emails=with_emails,
            start_raw=start_raw,
            duration=duration,
            teams=teams,
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
        return await calendar_freebusy(
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
        return await calendar_suggest(
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
        return await calendar_today(day=day, tz_name=tz_name, config=self._config)

    async def calendar_update(
        self,
        *,
        event_id: str,
        teams: bool = True,
        tz_name: str | None = None,
    ) -> dict[str, Any]:
        return await calendar_update(
            event_id=event_id,
            teams=teams,
            tz_name=tz_name,
            config=self._config,
        )

    async def calendar_view(
        self,
        *,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        return await calendar_view(start=start, end=end, config=self._config)

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
        return await chat_attachments_download(
            out=out,
            attachment_id=attachment_id,
            chat_id=chat_id,
            download_all=download_all,
            latest=latest,
            message_id=message_id,
            with_name=with_name,
            config=self._config,
        )

    async def chat_attachments_list(
        self,
        *,
        chat_id: str | None = None,
        latest: bool = False,
        message_id: str | None = None,
        with_name: str | None = None,
    ) -> dict[str, Any]:
        return await chat_attachments_list(
            chat_id=chat_id,
            latest=latest,
            message_id=message_id,
            with_name=with_name,
            config=self._config,
        )

    async def chat_delete(self, *, chat_id: str, message_id: str) -> dict[str, Any]:
        return await chat_delete(chat_id=chat_id, message_id=message_id, config=self._config)

    async def chat_edit(self, *, chat_id: str, message_id: str, text: str) -> dict[str, Any]:
        return await chat_edit(
            chat_id=chat_id, message_id=message_id, text=text, config=self._config
        )

    async def chat_find(self, *, with_name: str) -> dict[str, Any]:
        return await chat_find(with_name=with_name, config=self._config)

    async def chat_last(self, *, with_name: str, n: int = 3) -> dict[str, Any]:
        return await chat_last(with_name=with_name, n=n, config=self._config)

    async def chat_send(
        self,
        *,
        text: str,
        with_name: str | None = None,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        return await chat_send(text=text, with_name=with_name, chat_id=chat_id, config=self._config)

    async def mail_attachments_download(
        self,
        *,
        message_id: str,
        out: str,
        attachment_id: str | None = None,
        download_all: bool = False,
    ) -> dict[str, Any]:
        return await mail_attachments_download(
            message_id=message_id,
            out=out,
            attachment_id=attachment_id,
            download_all=download_all,
            config=self._config,
        )

    async def mail_attachments_list(self, *, message_id: str) -> dict[str, Any]:
        return await mail_attachments_list(message_id=message_id, config=self._config)

    async def mail_delete_draft(self, *, draft_id: str) -> dict[str, Any]:
        return await mail_delete_draft(draft_id=draft_id, config=self._config)

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
        return await mail_draft(
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
        return await mail_folders(config=self._config)

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
        return await mail_forward(
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
        return await mail_get(message_id=message_id, body_type=body_type, config=self._config)

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
        return await mail_inbox(
            top=top,
            has_attachments=has_attachments,
            importance=importance,
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
        return await mail_list(
            top=top,
            folder=folder,
            has_attachments=has_attachments,
            importance=importance,
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
        return await mail_reply(
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
        return await mail_send_draft(draft_id=draft_id, config=self._config)

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
        return await mail_update_draft(
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
        return await meeting_get(event_id=event_id, config=self._config)

    async def meeting_transcription(self, *, event_id: str, enable: bool = False) -> dict[str, Any]:
        return await meeting_transcription(event_id=event_id, enable=enable, config=self._config)

    async def people_resolve(
        self,
        *,
        name: str | None = None,
        email: str | None = None,
        top: int = 10,
    ) -> dict[str, Any]:
        return await people_resolve(name=name, email=email, top=top, config=self._config)
