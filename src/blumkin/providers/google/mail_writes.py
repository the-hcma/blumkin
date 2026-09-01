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
from collections.abc import Mapping, Sequence
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as _default_policy
from email.utils import getaddresses
from typing import Any

from googleapiclient.errors import HttpError

from blumkin.config import BlumkinConfig, load_config
from blumkin.providers.google.mail import _header_map, _html_to_text, _message_detail
from blumkin.providers.google_auth import get_credentials
from blumkin.providers.google_http import build_api_service, execute
from blumkin.skills.mail import (
    MailDraftNotFoundError,
    MailMessageNotFoundError,
    _merge_addresses,
    _parse_addresses,
    _read_attachment,
    append_mail_signature,
    resolve_mail_body,
)


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
    body_type: str = "text",
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
    content, body_type_label, _ = resolve_mail_body(
        body=body, body_file=body_file, body_type=body_type
    )
    cfg = config or load_config()
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
    body_type: str = "text",
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
    label = _body_label(body_type)
    cfg = config or load_config()
    service = _gmail_service(cfg)
    original = _get_message(service, mid)
    detail = _message_detail(original, wanted="text")
    comment = _comment_text(
        body=body, body_file=body_file, label=label, config=cfg, no_signature=no_signature
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
    body_type: str = "text",
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
    label = _body_label(body_type)
    cfg = config or load_config()
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
        body=body, body_file=body_file, label=label, config=cfg, no_signature=no_signature
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
    body_type: str = "text",
    cc: str | Sequence[str] | None = None,
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
    if has_body:
        new_content, new_body_type, _ = resolve_mail_body(
            body=body, body_file=body_file, body_type=body_type
        )
        if not new_content.strip():
            raise ValueError("--body/--body-file must be non-empty when provided")
    cfg = config or load_config()
    service = _gmail_service(cfg)
    parsed = _parse_raw_message(_get_draft(service, mid))

    subject_out = parsed.subject if subject is None else subject.strip()
    if subject is not None and not subject_out:
        raise ValueError("--subject must be non-empty when provided")
    to_out = parsed.to if to_addrs is None else to_addrs
    cc_out = parsed.cc if cc_addrs is None else cc_addrs
    bcc_out = parsed.bcc if bcc_addrs is None else bcc_addrs
    if new_content is None:
        content_out, body_type_out = parsed.body, parsed.body_type
    else:
        content_out, body_type_out = new_content, new_body_type or "text"
    attachments_out = [*parsed.attachments, *pending]

    message = _build_message(
        subject=subject_out,
        to=to_out,
        cc=cc_out,
        bcc=bcc_out,
        content=content_out,
        body_type=body_type_out,
        attachments=attachments_out,
    )
    execute(
        service.users()
        .drafts()
        .update(userId="me", id=mid, body={"message": {"raw": _raw(message)}}),
        num_retries=0,
    )
    return {
        "draft": {
            "attachments": [
                {"id": None, "name": name, "size": len(raw)} for name, raw in attachments_out
            ],
            "bcc": ", ".join(bcc_out) or None,
            "body_type": body_type_out,
            "cc": ", ".join(cc_out) or None,
            "id": mid,
            "subject": subject_out,
            "to": ", ".join(to_out),
        }
    }


class _ParsedMessage:
    """The fields ``mail update-draft`` may keep or override, pulled from raw RFC 822."""

    def __init__(
        self,
        *,
        subject: str,
        to: list[str],
        cc: list[str],
        bcc: list[str],
        body: str,
        body_type: str,
        attachments: list[tuple[str, bytes]],
    ) -> None:
        self.attachments = attachments
        self.bcc = bcc
        self.body = body
        self.body_type = body_type
        self.cc = cc
        self.subject = subject
        self.to = to


def _addresses(message: EmailMessage, header: str) -> list[str]:
    out: list[str] = []
    for value in message.get_all(header, []):
        for addr in str(value).split(","):
            cleaned = addr.strip()
            if cleaned:
                out.append(cleaned)
    return out


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


def _body_label(raw: str) -> str:
    label = raw.strip().lower()
    if label not in {"html", "text"}:
        raise ValueError("--body-type must be 'text' or 'html'")
    return label


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
    label: str,
    config: BlumkinConfig,
    no_signature: bool,
) -> str:
    """Resolve the optional reply/forward lead text, with the signature appended."""
    if body is None and body_file is None:
        return append_mail_signature("", body_type=label, config=config, no_signature=no_signature)
    content, resolved_label, _ = resolve_mail_body(body=body, body_file=body_file, body_type=label)
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
    creds = get_credentials(cfg, allow_interactive=False)
    return build_api_service("gmail", "v1", creds=creds, config=cfg)


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


def _parse_raw_message(draft: dict[str, Any]) -> _ParsedMessage:
    raw = ((draft.get("message") or {}).get("raw")) or ""
    data = base64.urlsafe_b64decode(raw.encode() + b"=" * (-len(raw) % 4)) if raw else b""
    message = BytesParser(policy=_default_policy).parsebytes(data)
    body_part = message.get_body(preferencelist=("html", "plain"))
    body_type = "text"
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        body_type = "html" if body_part.get_content_subtype() == "html" else "text"
    attachments: list[tuple[str, bytes]] = []
    for part in message.iter_attachments():
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes | bytearray):
            continue
        attachments.append((part.get_filename() or "attachment", bytes(payload)))
    return _ParsedMessage(
        subject=str(message.get("Subject") or "").strip(),
        to=_addresses(message, "To"),
        cc=_addresses(message, "Cc"),
        bcc=_addresses(message, "Bcc"),
        body=body,
        body_type=body_type,
        attachments=attachments,
    )


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
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


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


def _thread_headers(headers: Mapping[str, str]) -> dict[str, str]:
    message_id = headers.get("message-id", "")
    references = " ".join(part for part in (headers.get("references", ""), message_id) if part)
    out: dict[str, str] = {}
    if message_id:
        out["In-Reply-To"] = message_id
    if references:
        out["References"] = references
    return out
