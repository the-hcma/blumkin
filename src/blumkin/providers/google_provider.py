"""Google Workspace provider (calendar + mail reads MVP)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from blumkin.config import BlumkinConfig
from blumkin.providers import google_auth
from blumkin.providers.google import calendar as google_calendar
from blumkin.providers.google import chat as google_chat
from blumkin.providers.google import docs as google_docs
from blumkin.providers.google import mail as google_mail
from blumkin.providers.google import mail_writes as google_mail_writes
from blumkin.providers.google import people as google_people
from blumkin.providers.google_http import build_api_service, execute
from blumkin.providers.kind import ProviderKind
from blumkin.skills.calendar_writes import Recurrence


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

    def account_email(self) -> str:
        """Signed-in address via Gmail users.getProfile (no extra scope).

        Google's saved credential JSON carries no email, so this is a live call.
        Best-effort: callers use it for a display label and a doctor drift check,
        neither of which should fail because the network or a scope is missing.
        """
        try:
            creds = google_auth.get_credentials(
                self._config,
                allow_interactive=False,
                required_scopes=google_auth.MAIL_READ_SCOPES,
            )
            service = build_api_service("gmail", "v1", creds=creds, config=self._config)
            profile = execute(service.users().getProfile(userId="me"))
        except Exception:
            return ""
        return str(profile.get("emailAddress") or "").strip()

    def auth_status(self) -> dict[str, Any]:
        return google_auth.status_dict(self._config)

    async def calendar_accept(
        self,
        *,
        event_id: str | None = None,
        today_pending: bool = False,
        comment: str | None = None,
        tz_name: str | None = None,
    ) -> dict[str, Any]:
        return await google_calendar.calendar_accept(
            event_id=event_id,
            today_pending=today_pending,
            comment=comment,
            tz_name=tz_name,
            config=self._config,
        )

    async def calendar_cancel(
        self, *, event_id: str, calendar: str | None = None
    ) -> dict[str, Any]:
        return await google_calendar.calendar_cancel(
            event_id=event_id, calendar=calendar, config=self._config
        )

    async def calendar_decline(
        self,
        *,
        event_id: str | None = None,
        today_pending: bool = False,
        comment: str | None = None,
        propose_start: str | None = None,
        propose_duration: str | None = None,
        tz_name: str | None = None,
    ) -> dict[str, Any]:
        return await google_calendar.calendar_decline(
            event_id=event_id,
            today_pending=today_pending,
            comment=comment,
            propose_start=propose_start,
            propose_duration=propose_duration,
            tz_name=tz_name,
            config=self._config,
        )

    async def calendar_tentative(
        self,
        *,
        event_id: str | None = None,
        today_pending: bool = False,
        comment: str | None = None,
        propose_start: str | None = None,
        propose_duration: str | None = None,
        tz_name: str | None = None,
    ) -> dict[str, Any]:
        return await google_calendar.calendar_tentative(
            event_id=event_id,
            today_pending=today_pending,
            comment=comment,
            propose_start=propose_start,
            propose_duration=propose_duration,
            tz_name=tz_name,
            config=self._config,
        )

    async def calendar_create(
        self,
        *,
        subject: str,
        with_emails: list[str],
        start_raw: str,
        all_day: bool = False,
        body: str | None = None,
        body_file: str | None = None,
        body_type: str = "text",
        calendar: str | None = None,
        duration: str | None = None,
        location: str | None = None,
        optional_emails: list[str] | None = None,
        recurrence: Recurrence | None = None,
        remind_email: str | None = None,
        teams: bool = True,
        tz_name: str | None = None,
    ) -> dict[str, Any]:
        return await google_calendar.calendar_create(
            subject=subject,
            with_emails=with_emails,
            start_raw=start_raw,
            all_day=all_day,
            body=body,
            body_file=body_file,
            body_type=body_type,
            calendar=calendar,
            duration=duration,
            location=location,
            optional_emails=optional_emails,
            recurrence=recurrence,
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

    async def calendar_get(
        self,
        *,
        event_id: str,
        body_type: str = "text",  # Microsoft-only; ignored on Google
        calendar: str | None = None,
        tz_name: str | None = None,
    ) -> dict[str, Any]:
        return await google_calendar.calendar_get(
            event_id=event_id,
            calendar=calendar,
            tz_name=tz_name,
            config=self._config,
        )

    async def calendar_list(self) -> dict[str, Any]:
        return await google_calendar.calendar_list(config=self._config)

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
        calendar: str | None = None,
        tz_name: str | None = None,
    ) -> dict[str, Any]:
        return await google_calendar.calendar_today(
            day=day, calendar=calendar, tz_name=tz_name, config=self._config
        )

    async def calendar_update(
        self,
        *,
        event_id: str,
        all_day: bool | None = None,
        body: str | None = None,
        body_file: str | None = None,
        body_type: str = "text",
        calendar: str | None = None,
        duration: str | None = None,
        end_raw: str | None = None,
        location: str | None = None,
        start_raw: str | None = None,
        subject: str | None = None,
        teams: bool | None = None,
        with_emails: list[str] | None = None,
        tz_name: str | None = None,
    ) -> dict[str, Any]:
        return await google_calendar.calendar_update(
            event_id=event_id,
            all_day=all_day,
            body=body,
            body_file=body_file,
            body_type=body_type,
            calendar=calendar,
            duration=duration,
            end_raw=end_raw,
            location=location,
            start_raw=start_raw,
            subject=subject,
            teams=teams,
            with_emails=with_emails,
            tz_name=tz_name,
            config=self._config,
        )

    async def calendar_view(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar: str | None = None,
    ) -> dict[str, Any]:
        return await google_calendar.calendar_view(
            start=start, end=end, calendar=calendar, config=self._config
        )

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
        return await google_chat.chat_attachments_download(
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
        return await google_chat.chat_attachments_list(
            chat_id=chat_id,
            latest=latest,
            message_id=message_id,
            with_name=with_name,
            config=self._config,
        )

    async def chat_delete(self, *, chat_id: str, message_id: str) -> dict[str, Any]:
        return await google_chat.chat_delete(
            chat_id=chat_id, message_id=message_id, config=self._config
        )

    async def chat_edit(self, *, chat_id: str, message_id: str, text: str) -> dict[str, Any]:
        return await google_chat.chat_edit(
            chat_id=chat_id, message_id=message_id, text=text, config=self._config
        )

    async def chat_find(self, *, with_name: str) -> dict[str, Any]:
        return await google_chat.chat_find(with_name=with_name, config=self._config)

    async def chat_last(
        self,
        *,
        with_name: str | None = None,
        chat_id: str | None = None,
        contains: str | None = None,
        n: int = 3,
    ) -> dict[str, Any]:
        return await google_chat.chat_last(
            with_name=with_name, chat_id=chat_id, contains=contains, n=n, config=self._config
        )

    async def chat_send(
        self,
        *,
        text: str,
        with_name: str | None = None,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        return await google_chat.chat_send(
            text=text, with_name=with_name, chat_id=chat_id, config=self._config
        )

    async def docs_create(
        self,
        *,
        title: str,
        body: str | None = None,
        body_file: str | None = None,
        body_format: str = "markdown",
        folder: str | None = None,
    ) -> dict[str, Any]:
        return await google_docs.docs_create(
            title=title,
            body=body,
            body_file=body_file,
            body_format=body_format,
            folder=folder,
            config=self._config,
        )

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
        return await google_mail.mail_attachments_download(
            message_id=message_id,
            out=out,
            attachment_id=attachment_id,
            download_all=download_all,
            config=self._config,
        )

    async def mail_attachments_list(self, *, message_id: str) -> dict[str, Any]:
        return await google_mail.mail_attachments_list(message_id=message_id, config=self._config)

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
        body_type: str = "markdown",
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
        return await google_mail.mail_folders(config=self._config)

    async def mail_forward(
        self,
        *,
        message_id: str,
        to: str,
        body: str | None = None,
        body_file: str | None = None,
        body_type: str = "markdown",
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
        body_type: str = "markdown",
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

    async def mail_auto_reply(
        self,
        *,
        enable: bool | None = None,
        message: str | None = None,
        message_file: str | None = None,
        external_message: str | None = None,
        external_audience: str | None = None,
        start: date | None = None,
        until: date | None = None,
    ) -> dict[str, Any]:
        return await google_mail_writes.mail_auto_reply(
            enable=enable,
            message=message,
            message_file=message_file,
            external_message=external_message,
            external_audience=external_audience,
            start=start,
            until=until,
            config=self._config,
        )

    async def mail_delete(self, *, message_ids: Sequence[str]) -> dict[str, Any]:
        return await google_mail_writes.mail_delete(message_ids=message_ids, config=self._config)

    async def mail_mark(
        self,
        *,
        message_ids: Sequence[str],
        read: bool | None = None,
        flagged: bool | None = None,
        importance: str | None = None,
    ) -> dict[str, Any]:
        return await google_mail_writes.mail_mark(
            message_ids=message_ids,
            read=read,
            flagged=flagged,
            importance=importance,
            config=self._config,
        )

    async def mail_move(self, *, message_ids: Sequence[str], to: str) -> dict[str, Any]:
        return await google_mail_writes.mail_move(
            message_ids=message_ids, to=to, config=self._config
        )

    async def mail_search(
        self,
        *,
        query: str,
        top: int = 25,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        return await google_mail.mail_search(
            query=query, top=top, since=since, until=until, config=self._config
        )

    async def mail_send_draft(self, *, draft_id: str) -> dict[str, Any]:
        return await google_mail_writes.mail_send_draft(draft_id=draft_id, config=self._config)

    async def mail_thread(
        self, *, message_id: str, full: bool = False, body_type: str = "text"
    ) -> dict[str, Any]:
        return await google_mail.mail_thread(
            message_id=message_id, full=full, body_type=body_type, config=self._config
        )

    async def mail_update_draft(
        self,
        *,
        draft_id: str,
        attach: Sequence[str] = (),
        bcc: str | Sequence[str] | None = None,
        subject: str | None = None,
        body: str | None = None,
        body_file: str | None = None,
        body_type: str = "markdown",
        cc: str | Sequence[str] | None = None,
        keep_quoted: bool = False,
        no_signature: bool = False,
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
            keep_quoted=keep_quoted,
            no_signature=no_signature,
            to=to,
            config=self._config,
        )

    async def meeting_get(self, *, event_id: str) -> dict[str, Any]:
        return self._meeting_unsupported("meeting get")

    async def meeting_transcription(self, *, event_id: str, enable: bool = False) -> dict[str, Any]:
        return self._meeting_unsupported("meeting transcription")

    async def people_resolve(
        self,
        *,
        name: str | None = None,
        email: str | None = None,
        top: int = 10,
    ) -> dict[str, Any]:
        return await google_people.people_resolve(
            name=name, email=email, top=top, config=self._config
        )

    def _meeting_unsupported(self, op: str) -> dict[str, Any]:
        # Deliberate, not a TODO: Meet REST + transcript scopes are out of scope
        # for the parity milestone (docs/DECISIONS.md D8).
        raise ValueError(
            f"{op} not supported for provider=google "
            "(Meet get/transcription is intentionally not implemented; see docs/DECISIONS.md D8)"
        )

    def _unsupported(self, op: str) -> dict[str, Any]:
        raise ValueError(f"{op} not supported for provider=google yet")
