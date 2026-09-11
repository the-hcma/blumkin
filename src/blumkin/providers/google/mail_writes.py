"""Google Gmail draft write skills (create / update / delete / send / reply / forward).

Payloads mirror ``blumkin.skills.mail`` so ``--json`` schemas stay identical
across providers. Gmail has no upload-session step: a draft is one raw RFC 822
message, so attachments ride inside the MIME body rather than as a separate
collection with per-item ids (``attachments[].id`` is therefore ``null``).

``id`` in the returned ``draft`` object is the Gmail **draft** id, which is what
``mail send-draft`` / ``mail delete-draft`` expect back (Gmail keys those on the
draft id, not the underlying message id).

``reply`` sets the draft's ``threadId`` plus ``In-Reply-To`` / ``References`` so
Gmail threads it with the original; ``forward`` starts a new thread and carries
the original's attachments into the new MIME body.
"""

from __future__ import annotations

import base64
import html as html_lib
import mimetypes
import pathlib
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import SMTP as _smtp_policy
from email.policy import default as _default_policy
from email.utils import getaddresses
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from googleapiclient.errors import HttpError

from blumkin.config import BlumkinConfig, load_config
from blumkin.providers.google.mail import (
    _FOLDER_LABELS,
    _header_map,
    _html_to_text,
    _message_detail,
)
from blumkin.providers.google_auth import (
    MAIL_MODIFY_SCOPES,
    MAIL_SETTINGS_SCOPES,
    MAIL_WRITE_SCOPES,
    get_credentials,
)
from blumkin.providers.google_http import build_api_service, execute
from blumkin.skills.mail import (
    MailDraftNotFoundError,
    MailFolderNotFoundError,
    MailMessageNotFoundError,
    _clean_message_ids,
    _compose_wire_label,
    _merge_addresses,
    _parse_addresses,
    _read_attachment,
    _validate_importance,
    append_mail_signature,
    resolve_mail_body,
    split_quoted_original,
)

_IMPORTANCE_LABEL_OPS = {
    "high": (["IMPORTANT"], []),
    "normal": ([], ["IMPORTANT"]),
    "low": ([], ["IMPORTANT"]),
}


async def mail_auto_reply(
    *,
    enable: bool | None = None,
    message: str | None = None,
    message_file: str | None = None,
    external_message: str | None = None,
    external_audience: str | None = None,
    start: date | None = None,
    until: date | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Read / set / clear the Gmail vacation responder (needs gmail.settings.basic).

    Gmail has one response body (no internal/external split), so
    ``--external-message`` is not supported here. ``--external`` maps to Gmail's
    vacation restrictions: ``contacts`` -> ``restrictToContacts``, ``none`` ->
    ``restrictToDomain`` (org only), ``all`` -> neither.
    """
    if external_message is not None:
        raise ValueError("--external-message is Microsoft-only (Gmail has one response body)")
    cfg = config or load_config()
    service = _gmail_settings_service(cfg)
    current = execute(service.users().settings().getVacation(userId="me"))
    if enable is None:
        return {"auto_reply": _vacation_to_dict(current)}
    if enable is False:
        # updateVacation replaces the whole resource, so carry the existing body
        # and restrictions forward - only the enable flag changes.
        body: dict[str, Any] = {**_vacation_keep(current), "enableAutoReply": False}
    else:
        text = _read_body_text(message, message_file)
        if not text:
            raise ValueError("turning auto-reply on needs --message or --message-file")
        if external_audience is not None and external_audience not in {"none", "contacts", "all"}:
            raise ValueError("--external must be none, contacts, or all")
        # A full-resource write: startTime/endTime are set only when scheduled, so
        # re-enabling always-on drops any window left over from an earlier schedule.
        body = {
            "enableAutoReply": True,
            "responseBodyPlainText": text,
            "restrictToContacts": external_audience == "contacts",
            "restrictToDomain": external_audience == "none",
        }
        if start is not None or until is not None:
            tz = _oof_zone(cfg.default_tz)
            if start is not None:
                body["startTime"] = _day_epoch_ms(start, tz)
            if until is not None:
                body["endTime"] = _day_epoch_ms(until, tz, end=True)
    updated = execute(service.users().settings().updateVacation(userId="me", body=body))
    return {"auto_reply": _vacation_to_dict(updated)}


_VACATION_KEEP_KEYS = (
    "responseSubject",
    "responseBodyPlainText",
    "responseBodyHtml",
    "restrictToContacts",
    "restrictToDomain",
    "startTime",
    "endTime",
)


def _vacation_keep(current: dict[str, Any]) -> dict[str, Any]:
    return {k: current[k] for k in _VACATION_KEEP_KEYS if k in current}


def _day_epoch_ms(day: date, tz: ZoneInfo, *, end: bool = False) -> int:
    """A calendar date -> local-midnight epoch ms in ``tz``. ``end`` makes ``--until`` inclusive."""
    boundary = day + timedelta(days=1) if end else day
    return int(datetime(boundary.year, boundary.month, boundary.day, tzinfo=tz).timestamp() * 1000)


def _oof_zone(tz_name: str) -> ZoneInfo:
    """Resolve the profile timezone for an OOF window, falling back to UTC.

    ``default_tz`` is a free-form, unvalidated config string (``""`` when a profile
    never set it), so ``ZoneInfo`` can raise - a scheduled auto-reply must not
    crash on that.
    """
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


def _gmail_settings_service(cfg: BlumkinConfig) -> Any:
    creds = get_credentials(cfg, allow_interactive=False, required_scopes=MAIL_SETTINGS_SCOPES)
    return build_api_service("gmail", "v1", creds=creds, config=cfg)


def _read_body_text(message: str | None, message_file: str | None) -> str | None:
    if message is not None and message_file is not None:
        raise ValueError("pass only one of --message or --message-file")
    if message_file is not None:
        try:
            return pathlib.Path(message_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"cannot read --message-file {message_file}: {exc}") from exc
    return message


def _vacation_to_dict(vacation: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(vacation.get("enableAutoReply"))
    scheduled = bool(vacation.get("startTime") or vacation.get("endTime"))
    body = vacation.get("responseBodyPlainText") or vacation.get("responseBodyHtml")
    return {
        "enabled": enabled,
        "scope": ("scheduled" if scheduled else "alwaysEnabled") if enabled else "disabled",
        "start": _epoch_ms_iso(vacation.get("startTime")),
        "end": _epoch_ms_iso(vacation.get("endTime")),
        "internal_message": body,
        "external_message": body,
        "external_audience": (
            "contacts"
            if vacation.get("restrictToContacts")
            else "none"
            if vacation.get("restrictToDomain")
            else "all"
        ),
    }


def _epoch_ms_iso(raw: Any) -> str | None:
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC).isoformat()
    except TypeError, ValueError:
        return None


async def mail_delete(
    *,
    message_ids: Sequence[str],
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Move one or more messages to the Gmail Trash (recoverable)."""
    ids = _clean_message_ids(message_ids)
    service = _gmail_modify_service(config or load_config())

    def _apply(mid: str) -> None:
        execute(service.users().messages().trash(userId="me", id=mid), num_retries=0)

    return _triage_batch(ids, _apply, key="deleted")


async def mail_mark(
    *,
    message_ids: Sequence[str],
    read: bool | None = None,
    flagged: bool | None = None,
    importance: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Set read/unread (UNREAD), the star (STARRED), and/or importance (IMPORTANT)."""
    ids = _clean_message_ids(message_ids)
    if read is None and flagged is None and importance is None:
        raise ValueError("pass at least one of --read/--unread, --flag/--unflag, --importance")
    add: list[str] = []
    remove: list[str] = []
    if read is not None:
        (remove if read else add).append("UNREAD")
    if flagged is not None:
        (add if flagged else remove).append("STARRED")
    if importance is not None:
        imp_add, imp_remove = _IMPORTANCE_LABEL_OPS[_validate_importance(importance)]
        add += imp_add
        remove += imp_remove
    body = {"addLabelIds": add, "removeLabelIds": remove}
    service = _gmail_modify_service(config or load_config())

    def _apply(mid: str) -> None:
        execute(service.users().messages().modify(userId="me", id=mid, body=body), num_retries=0)

    return _triage_batch(ids, _apply, key="marked")


async def mail_move(
    *,
    message_ids: Sequence[str],
    to: str,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Move one or more messages: to a folder label, or `archive` (remove INBOX)."""
    ids = _clean_message_ids(message_ids)
    label = to.strip()
    if not label:
        raise ValueError("--to is required")
    service = _gmail_modify_service(config or load_config())
    add, dest = _gmail_move_labels(service, label)
    # Gmail "move" = drop INBOX and add the destination label. Keep INBOX only
    # when the destination *is* INBOX (un-archive), so the modify never adds and
    # removes the same label in one call.
    remove = [] if "INBOX" in add else ["INBOX"]
    body = {"addLabelIds": add, "removeLabelIds": remove}

    def _apply(mid: str) -> None:
        execute(service.users().messages().modify(userId="me", id=mid, body=body), num_retries=0)

    result = _triage_batch(ids, _apply, key="moved")
    result["to"] = dest
    return result


def _gmail_move_labels(service: Any, label: str) -> tuple[list[str], str]:
    """`--to` -> (labels to add, destination name). `archive` just removes INBOX."""
    key = label.casefold()
    if key in {"archive", "all mail", "allmail"}:
        return [], "archive"
    if key in _FOLDER_LABELS:
        mapped = _FOLDER_LABELS[key]
        if mapped is None:
            raise MailFolderNotFoundError(
                f"mail folder {label!r} is not a move target for provider=google"
            )
        return [mapped], mapped
    if label.startswith("Label_") or label in _GMAIL_SYSTEM_LABELS:
        return [label], label
    # A user label name: Gmail modify only takes label ids, so resolve it via
    # labels.list (case-insensitive), the same ids `mail folders` reports.
    listing = execute(service.users().labels().list(userId="me"))
    for entry in listing.get("labels") or []:
        if str(entry.get("name") or "").casefold() == key:
            return [str(entry["id"])], str(entry.get("name") or label)
    raise MailFolderNotFoundError(
        f"no Gmail label named {label!r}; pass a Label_ id from `blumkin mail folders`"
    )


_GMAIL_SYSTEM_LABELS = frozenset(
    {"INBOX", "SENT", "TRASH", "DRAFT", "SPAM", "STARRED", "IMPORTANT", "UNREAD"}
)


def _gmail_modify_service(cfg: BlumkinConfig) -> Any:
    creds = get_credentials(cfg, allow_interactive=False, required_scopes=MAIL_MODIFY_SCOPES)
    return build_api_service("gmail", "v1", creds=creds, config=cfg)


def _triage_batch(
    ids: Sequence[str],
    apply: Any,
    *,
    key: str,
) -> dict[str, Any]:
    done: list[str] = []
    skipped: list[dict[str, str]] = []
    first_error: Exception | None = None
    for mid in ids:
        try:
            apply(mid)
        except HttpError as exc:
            missing = _http_status(exc) in {404, 410}
            reason = f"message not found: {mid}" if missing else str(exc)
            if len(ids) == 1:
                if missing:
                    raise MailMessageNotFoundError(reason) from exc
                raise
            first_error = first_error or exc
            skipped.append({"id": mid, "reason": reason})
            continue
        done.append(mid)
    if len(ids) > 1 and not done and first_error is not None:
        raise first_error
    return {key: done, "count": len(done), "skipped": skipped}


async def mail_delete_draft(
    *,
    draft_id: str,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    mid = draft_id.strip()
    if not mid:
        raise ValueError("--id is required")
    cfg = config or load_config()
    service = _gmail_service(cfg)
    _get_draft(service, mid)
    execute(service.users().drafts().delete(userId="me", id=mid), num_retries=0)
    return {"deleted": mid}


async def mail_draft(
    *,
    to: str | Sequence[str],
    subject: str,
    attach: Sequence[str] = (),
    bcc: str | Sequence[str] = (),
    body: str | None = None,
    body_file: str | None = None,
    body_type: str | None = None,
    cc: str | Sequence[str] = (),
    config: BlumkinConfig | None = None,
    no_signature: bool = False,
) -> dict[str, Any]:
    to_addrs = _parse_addresses(to, flag="--to", required=True)
    if to_addrs is None:
        raise ValueError("--to is required")
    cc_addrs = _parse_addresses(cc, flag="--cc", required=False) or []
    bcc_addrs = _parse_addresses(bcc, flag="--bcc", required=False) or []
    if not subject.strip():
        raise ValueError("--subject is required")
    # Read the files before touching Gmail: a bad path should not leave a draft behind.
    pending = [_read_attachment(path) for path in attach]
    cfg = config or load_config()
    content, body_type_label, _ = resolve_mail_body(
        body=body, body_file=body_file, body_type=body_type, config=cfg
    )
    content = append_mail_signature(
        content, body_type=body_type_label, config=cfg, no_signature=no_signature
    )
    message = _build_message(
        subject=subject.strip(),
        to=to_addrs,
        cc=cc_addrs,
        bcc=bcc_addrs,
        content=content,
        body_type=body_type_label,
        attachments=pending,
    )
    service = _gmail_service(cfg)
    created = execute(
        service.users().drafts().create(userId="me", body={"message": {"raw": _raw(message)}}),
        # drafts.create is a non-idempotent POST; a blind retry would double the draft.
        num_retries=0,
    )
    return {
        "draft": {
            "attachments": [{"id": None, "name": name, "size": len(raw)} for name, raw in pending],
            "bcc": ", ".join(bcc_addrs) or None,
            "body_type": body_type_label,
            "cc": ", ".join(cc_addrs) or None,
            "id": created.get("id"),
            "subject": subject.strip(),
            "to": ", ".join(to_addrs),
        }
    }


async def mail_forward(
    *,
    message_id: str,
    to: str,
    body: str | None = None,
    body_file: str | None = None,
    body_type: str | None = None,
    bcc: str | Sequence[str] | None = None,
    cc: str | Sequence[str] | None = None,
    config: BlumkinConfig | None = None,
    no_signature: bool = False,
) -> dict[str, Any]:
    mid = message_id.strip()
    if not mid:
        raise ValueError("--id is required")
    if not to.strip():
        raise ValueError("--to is required")
    to_addrs = _parse_addresses(to, flag="--to", required=True) or []
    cc_addrs = _parse_addresses(cc, flag="--cc", required=False) or []
    bcc_addrs = _parse_addresses(bcc, flag="--bcc", required=False) or []
    cfg = config or load_config()
    label = _body_label(body_type, config=cfg)
    service = _gmail_service(cfg)
    original = _get_message(service, mid)
    detail = _message_detail(original, wanted="text")
    comment = _comment_text(
        body=body, body_file=body_file, body_type=body_type, config=cfg, no_signature=no_signature
    )
    content = _join_sections(comment, _quote_for_forward(detail, label), label)
    message = _build_message(
        subject=_prefixed_subject(detail.get("subject"), "Fwd:"),
        to=to_addrs,
        cc=cc_addrs,
        bcc=bcc_addrs,
        content=content,
        body_type=label,
        attachments=_original_attachments(service, mid, original.get("payload") or {}),
    )
    created = execute(
        service.users().drafts().create(userId="me", body={"message": {"raw": _raw(message)}}),
        num_retries=0,
    )
    return {
        "draft": _reply_forward_summary(
            created=created,
            # drafts.create returns threadId on the nested message, not the draft.
            thread_id=(created.get("message") or {}).get("threadId"),
            kind="forward",
            source=mid,
            subject=_prefixed_subject(detail.get("subject"), "Fwd:"),
            to=to_addrs,
            cc=cc_addrs,
            bcc=bcc_addrs,
            label=label,
        )
    }


async def mail_reply(
    *,
    message_id: str,
    body: str | None = None,
    body_file: str | None = None,
    body_type: str | None = None,
    bcc: str | Sequence[str] | None = None,
    cc: str | Sequence[str] | None = None,
    reply_all: bool = False,
    config: BlumkinConfig | None = None,
    no_signature: bool = False,
) -> dict[str, Any]:
    mid = message_id.strip()
    if not mid:
        raise ValueError("--id is required")
    cc_flag = _parse_addresses(cc, flag="--cc", required=False)
    bcc_flag = _parse_addresses(bcc, flag="--bcc", required=False)
    cfg = config or load_config()
    label = _body_label(body_type, config=cfg)
    service = _gmail_service(cfg)
    original = _get_message(service, mid)
    detail = _message_detail(original, wanted="text")
    headers = _header_map(original)

    me = _me_email(service)
    original_recipients = [
        person["email"]
        for person in (*detail.get("to", []), *detail.get("cc", []))
        if person.get("email")
    ]
    reply_to = _reply_to_addresses(headers, detail)
    from_email = detail.get("from_email") or ""
    if me and from_email and from_email.casefold() == me.casefold():
        # The signed-in user sent the original: address whoever it went to, not
        # the From/Reply-To (which is us). Keyed on From, not the sender-controlled
        # Reply-To header, so a spoofed Reply-To can't redirect a plain reply.
        reply_to = [
            addr for addr in original_recipients if addr.casefold() != me.casefold()
        ] or reply_to
    cc_out: list[str] = []
    if reply_all:
        exclude = {addr.casefold() for addr in (*reply_to, me) if addr}
        cc_out = [addr for addr in original_recipients if addr.casefold() not in exclude]
    if cc_flag is not None:
        cc_out = _merge_addresses(cc_out, cc_flag)
    bcc_out = bcc_flag or []

    subject = _prefixed_subject(detail.get("subject"), "Re:")
    comment = _comment_text(
        body=body, body_file=body_file, body_type=body_type, config=cfg, no_signature=no_signature
    )
    content = _join_sections(comment, _quote_for_reply(detail, label), label)
    message = _build_message(
        subject=subject,
        to=reply_to,
        cc=cc_out,
        bcc=bcc_out,
        content=content,
        body_type=label,
        attachments=[],
        extra_headers=_thread_headers(headers),
    )
    created = execute(
        service.users()
        .drafts()
        .create(
            userId="me",
            body={"message": {"raw": _raw(message), "threadId": detail.get("conversation_id")}},
        ),
        num_retries=0,
    )
    return {
        "draft": _reply_forward_summary(
            created=created,
            thread_id=detail.get("conversation_id"),
            kind="reply-all" if reply_all else "reply",
            source=mid,
            subject=subject,
            to=reply_to,
            cc=cc_out,
            bcc=bcc_out,
            label=label,
        )
    }


async def mail_send_draft(
    *,
    draft_id: str,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    mid = draft_id.strip()
    if not mid:
        raise ValueError("--id is required")
    cfg = config or load_config()
    service = _gmail_service(cfg)
    # Let an HttpError propagate: the CLI maps 404 -> not_found, 403 -> missing_scope.
    execute(
        service.users().drafts().send(userId="me", body={"id": mid}),
        # send is non-idempotent — a retry could deliver the mail twice.
        num_retries=0,
    )
    return {"sent": mid}


async def mail_update_draft(
    *,
    draft_id: str,
    attach: Sequence[str] = (),
    bcc: str | Sequence[str] | None = None,
    subject: str | None = None,
    body: str | None = None,
    body_file: str | None = None,
    body_type: str | None = None,
    cc: str | Sequence[str] | None = None,
    keep_quoted: bool = False,
    no_signature: bool = False,
    to: str | Sequence[str] | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    mid = draft_id.strip()
    if not mid:
        raise ValueError("--id is required")
    has_body = body is not None or body_file is not None
    to_addrs = _parse_addresses(to, flag="--to", required=False)
    cc_addrs = _parse_addresses(cc, flag="--cc", required=False)
    bcc_addrs = _parse_addresses(bcc, flag="--bcc", required=False)
    if (
        subject is None
        and not has_body
        and to_addrs is None
        and cc_addrs is None
        and bcc_addrs is None
        and not attach
    ):
        raise ValueError(
            "provide at least one of --subject, --body/--body-file, --to, --cc, --bcc, or --attach"
        )
    pending = [_read_attachment(path) for path in attach]
    new_content: str | None = None
    new_body_type: str | None = None
    cfg = config or load_config()
    if has_body:
        new_content, new_body_type, _ = resolve_mail_body(
            body=body, body_file=body_file, body_type=body_type, config=cfg
        )
        if not new_content.strip():
            raise ValueError("--body/--body-file must be non-empty when provided")
    if new_content is not None:
        new_content = append_mail_signature(
            new_content,
            body_type=new_body_type or "text",
            config=cfg,
            no_signature=no_signature,
        )
    service = _gmail_service(cfg)
    stored = _get_draft(service, mid)
    thread_id = (stored.get("message") or {}).get("threadId")
    # Mutate the stored message in place rather than rebuilding it from a parsed
    # model: this keeps Message-ID / In-Reply-To / References headers, inline
    # (cid) parts, and any structure the tool does not model.
    message = _load_raw_message(stored)

    if new_content is not None and _has_inline_parts(message):
        # A body rewrite can't safely reflow multipart/related inline images; the
        # user should recreate the draft rather than get one with broken cid refs.
        raise ValueError(
            "--body cannot rewrite a draft that has inline images; recreate the draft instead"
        )
    if subject is not None:
        if not subject.strip():
            raise ValueError("--subject must be non-empty when provided")
        _set_header(message, "Subject", subject.strip())
    if to_addrs is not None:
        _set_header(message, "To", ", ".join(to_addrs))
    if cc_addrs is not None:
        _set_header(message, "Cc", ", ".join(cc_addrs))
    if bcc_addrs is not None:
        _set_header(message, "Bcc", ", ".join(bcc_addrs))
    if new_content is not None:
        if keep_quoted:
            existing_part = message.get_body(preferencelist=("html", "plain"))
            existing_body = existing_part.get_content() if existing_part is not None else ""
            _, quoted = split_quoted_original(str(existing_body))
            if quoted:
                # The quoted tail is markup, so the joined body must be html.
                head = new_content
                if (new_body_type or "text") != "html":
                    head = html_lib.escape(head).replace("\n", "<br>")
                    new_body_type = "html"
                new_content = f"{head}{quoted}"
        _replace_body(message, new_content, new_body_type or "text")
    for name, data in pending:
        maintype, _, subtype = (
            mimetypes.guess_type(name)[0] or "application/octet-stream"
        ).partition("/")
        message.add_attachment(
            data, maintype=maintype, subtype=subtype or "octet-stream", filename=name
        )

    update_message: dict[str, Any] = {"raw": _raw(message)}
    if isinstance(thread_id, str) and thread_id:
        # Re-assert threadId (and the preserved In-Reply-To / References headers)
        # so updating a reply draft keeps it in its conversation.
        update_message["threadId"] = thread_id
    execute(
        service.users().drafts().update(userId="me", id=mid, body={"message": update_message}),
        num_retries=0,
    )
    body_part = message.get_body(preferencelist=("html", "plain"))
    body_type_out = (
        "html" if body_part is not None and body_part.get_content_subtype() == "html" else "text"
    )
    return {
        "draft": {
            "attachments": [
                {"id": None, "name": name, "size": len(data)} for name, data in pending
            ],
            "bcc": ", ".join(_addresses_from(message, "Bcc")) or None,
            "body_type": body_type_out,
            "cc": ", ".join(_addresses_from(message, "Cc")) or None,
            "id": mid,
            "subject": str(message.get("Subject") or "").strip(),
            "to": ", ".join(_addresses_from(message, "To")),
        }
    }


def _addresses_from(message: EmailMessage, header: str) -> list[str]:
    """Bare addresses on ``header``, RFC-parsed so quoted display names with commas survive."""
    return [addr for _, addr in getaddresses(message.get_all(header, [])) if addr]


def _attachment_refs(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(filename, attachmentId) for every part with fetchable bytes, filename or not."""
    refs: list[tuple[str, str]] = []
    attachment_id = (payload.get("body") or {}).get("attachmentId")
    if isinstance(attachment_id, str) and attachment_id:
        filename = str(payload.get("filename") or "").strip() or attachment_id
        refs.append((filename, attachment_id))
    for part in payload.get("parts") or []:
        if isinstance(part, Mapping):
            refs.extend(_attachment_refs(part))
    return refs


def _body_label(raw: str | None, config: BlumkinConfig | None = None) -> str:
    """Effective type for quoting / joining the reply: markdown and html both
    produce an HTML lead, so the quoted original has to be HTML too."""
    return _compose_wire_label(raw, config=config)


def _build_message(
    *,
    subject: str,
    to: Sequence[str],
    cc: Sequence[str],
    bcc: Sequence[str],
    content: str,
    body_type: str,
    attachments: Sequence[tuple[str, bytes]],
    extra_headers: Mapping[str, str] | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    if to:
        message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    for name, value in (extra_headers or {}).items():
        if value:
            message[name] = value
    message.set_content(_html_to_text(content) if body_type == "html" else content)
    if body_type == "html":
        message.add_alternative(content, subtype="html")
    for name, raw in attachments:
        maintype, _, subtype = (
            mimetypes.guess_type(name)[0] or "application/octet-stream"
        ).partition("/")
        message.add_attachment(
            raw, maintype=maintype, subtype=subtype or "octet-stream", filename=name
        )
    return message


def _comment_text(
    *,
    body: str | None,
    body_file: str | None,
    body_type: str | None,
    config: BlumkinConfig,
    no_signature: bool,
) -> str:
    """Resolve the optional reply/forward lead text, with the signature appended.

    ``body_type`` is the raw ``--body-type`` (``markdown`` / ``html`` / ``text``, or
    ``None`` to fall back to ``config.preferences.html_email``); ``resolve_mail_body``
    renders a Markdown lead to HTML before it is joined to the quoted original.
    """
    if body is None and body_file is None:
        # Nothing to render - markdown/html both give an HTML signature to match
        # the HTML quoted original (see _body_label).
        label = _body_label(body_type, config=config)
        return append_mail_signature("", body_type=label, config=config, no_signature=no_signature)
    content, resolved_label, _ = resolve_mail_body(
        body=body, body_file=body_file, body_type=body_type, config=config
    )
    return append_mail_signature(
        content, body_type=resolved_label, config=config, no_signature=no_signature
    )


def _get_draft(service: Any, draft_id: str) -> dict[str, Any]:
    try:
        return execute(service.users().drafts().get(userId="me", id=draft_id, format="raw"))
    except HttpError as exc:
        if _http_status(exc) == 404:
            raise MailDraftNotFoundError(f"message not found: {draft_id}") from exc
        raise


def _get_message(service: Any, message_id: str) -> dict[str, Any]:
    try:
        return execute(service.users().messages().get(userId="me", id=message_id, format="full"))
    except HttpError as exc:
        if _http_status(exc) == 404:
            raise MailMessageNotFoundError(f"message not found: {message_id}") from exc
        raise


def _gmail_service(cfg: BlumkinConfig) -> Any:
    creds = get_credentials(cfg, allow_interactive=False, required_scopes=MAIL_WRITE_SCOPES)
    return build_api_service("gmail", "v1", creds=creds, config=cfg)


def _has_inline_parts(message: Any) -> bool:
    """True when any leaf part *in the draft's own body* is an inline (cid) attachment.

    Stops at a ``message/rfc822`` boundary: a forwarded/attached message's inline
    images are preserved wholesale by ``_replace_body`` (the whole nested message is
    re-attached), so they are not at risk from a body rewrite and must not trigger
    the reject-and-recreate guard.
    """
    for part in message.iter_parts():
        if part.get_content_maintype() == "message":
            continue
        if part.is_multipart():
            if _has_inline_parts(part):
                return True
            continue
        if part.get("Content-ID"):
            return True
        disposition = str(part.get("Content-Disposition") or "").split(";", 1)[0].strip().lower()
        if disposition == "inline" and part.get_filename():
            return True
    return False


def _http_status(exc: HttpError) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    resp = getattr(exc, "resp", None)
    value = getattr(resp, "status", None) if resp is not None else None
    try:
        return int(value) if value is not None else None
    except TypeError, ValueError:
        return None


def _join_sections(lead: str, quoted: str, label: str) -> str:
    if not lead.strip():
        return quoted
    if not quoted:
        return lead
    separator = "<br><br>" if label == "html" else "\n\n"
    return f"{lead.rstrip()}{separator}{quoted}"


def _me_email(service: Any) -> str:
    profile = execute(service.users().getProfile(userId="me"))
    return str(profile.get("emailAddress") or "")


def _original_attachments(
    service: Any, message_id: str, payload: Mapping[str, Any]
) -> list[tuple[str, bytes]]:
    """Fetch every file attachment on the original so a forward carries them along.

    A part with an attachmentId whose fetch returns no ``data`` is a hard error,
    not a silent drop: the forward promise is that the attachments come along.
    """
    out: list[tuple[str, bytes]] = []
    for name, attachment_id in _attachment_refs(payload):
        fetched = execute(
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
        )
        data = fetched.get("data")
        if not isinstance(data, str):
            raise RuntimeError(
                f"could not fetch attachment {attachment_id} on message {message_id} to forward"
            )
        out.append((name, base64.urlsafe_b64decode(data.encode() + b"=" * (-len(data) % 4))))
    return out


def _load_raw_message(draft: Mapping[str, Any]) -> EmailMessage:
    raw = ((draft.get("message") or {}).get("raw")) or ""
    data = base64.urlsafe_b64decode(raw.encode() + b"=" * (-len(raw) % 4)) if raw else b""
    parsed = BytesParser(policy=_default_policy).parsebytes(data)
    if not isinstance(
        parsed, EmailMessage
    ):  # pragma: no cover - default policy yields EmailMessage
        raise TypeError("expected EmailMessage from the default email policy")
    return parsed


def _prefixed_subject(subject: Any, prefix: str) -> str:
    text = str(subject or "").strip()
    if text.lower().startswith(prefix.lower()):
        return text
    return f"{prefix} {text}".strip()


def _quote_for_forward(detail: Mapping[str, Any], label: str) -> str:
    who = detail.get("from_name") or detail.get("from_email") or "(unknown sender)"
    lines = [
        "---------- Forwarded message ----------",
        f"From: {who}",
        f"Date: {detail.get('sent') or detail.get('received') or ''}",
        f"Subject: {detail.get('subject') or ''}",
        f"To: {', '.join(p['email'] for p in detail.get('to', []) if p.get('email'))}",
    ]
    header = "\n".join(line for line in lines if line.strip().rstrip(":"))
    original = detail.get("body") or ""
    if label == "html":
        body = html_lib.escape(f"{header}\n\n{original}").replace("\n", "<br>")
        return f"<blockquote>{body}</blockquote>"
    return f"{header}\n\n{original}"


def _quote_for_reply(detail: Mapping[str, Any], label: str) -> str:
    who = detail.get("from_name") or detail.get("from_email") or "someone"
    when = detail.get("sent") or detail.get("received") or ""
    attribution = f"On {when}, {who} wrote:" if when else f"{who} wrote:"
    original = detail.get("body") or ""
    if label == "html":
        quoted = html_lib.escape(original).replace("\n", "<br>")
        return f"{html_lib.escape(attribution)}<blockquote>{quoted}</blockquote>"
    quoted = "\n".join(f"> {line}" for line in original.splitlines())
    return f"{attribution}\n{quoted}" if quoted else attribution


def _raw(message: EmailMessage) -> str:
    # Gmail parses this `raw` field as an RFC 5322 message, whose line ending is CRLF.
    # EmailMessage is built under email.policy.default (linesep "\n"); serialized with
    # that policy the body goes out LF-only, which Gmail accepts but then renders with
    # every line break collapsed. policy.SMTP is policy.default with linesep "\r\n"
    # (the content managers already normalize any stray CR the caller passed in).
    return base64.urlsafe_b64encode(message.as_bytes(policy=_smtp_policy)).decode()


def _replace_body(message: EmailMessage, content: str, body_type: str) -> None:
    """Swap the draft body, keeping headers and re-attaching regular file attachments.

    Callers reject drafts with inline (cid) parts first (see ``_has_inline_parts``),
    so this only has to carry ``Content-Disposition: attachment`` files across.
    """
    messages: list[EmailMessage] = []
    files: list[tuple[bytes, str, str, str]] = []
    for part in message.iter_attachments():
        if part.get_content_maintype() == "message":
            # message/rfc822: payload is a nested Message; carry the whole part.
            nested = part.get_payload()
            inner = nested[0] if isinstance(nested, list) and nested else nested
            if isinstance(inner, EmailMessage):
                messages.append(inner)
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes | bytearray):
            continue
        files.append(
            (
                bytes(payload),
                part.get_content_maintype(),
                part.get_content_subtype(),
                part.get_filename() or "attachment",
            )
        )
    message.clear_content()
    if body_type == "html":
        message.set_content(_html_to_text(content))
        message.add_alternative(content, subtype="html")
    else:
        message.set_content(content)
    for data, maintype, subtype, filename in files:
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    for nested_message in messages:
        message.add_attachment(nested_message)


def _reply_forward_summary(
    *,
    created: Mapping[str, Any],
    thread_id: Any,
    kind: str,
    source: str,
    subject: str,
    to: Sequence[str],
    cc: Sequence[str],
    bcc: Sequence[str],
    label: str,
) -> dict[str, Any]:
    return {
        "bcc": ", ".join(bcc) or None,
        "body_type": label,
        "cc": ", ".join(cc) or None,
        "conversation_id": thread_id,
        "id": created.get("id"),
        "kind": kind,
        "source_message_id": source,
        "subject": subject,
        "to": ", ".join(to),
    }


def _reply_to_addresses(headers: Mapping[str, str], detail: Mapping[str, Any]) -> list[str]:
    reply_to = headers.get("reply-to")
    if reply_to:
        parsed = [addr for _, addr in getaddresses([reply_to]) if addr]
        if parsed:
            return parsed
    sender = detail.get("from_email")
    return [sender] if sender else []


def _set_header(message: EmailMessage, name: str, value: str) -> None:
    del message[name]
    message[name] = value


def _thread_headers(headers: Mapping[str, str]) -> dict[str, str]:
    message_id = headers.get("message-id", "")
    references = " ".join(part for part in (headers.get("references", ""), message_id) if part)
    out: dict[str, str] = {}
    if message_id:
        out["In-Reply-To"] = message_id
    if references:
        out["References"] = references
    return out
