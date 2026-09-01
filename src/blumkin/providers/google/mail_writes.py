"""Google Gmail draft write skills (create / update / delete / send).

Payloads mirror ``blumkin.skills.mail`` so ``--json`` schemas stay identical
across providers. Gmail has no upload-session step: a draft is one raw RFC 822
message, so attachments ride inside the MIME body rather than as a separate
collection with per-item ids (``attachments[].id`` is therefore ``null``).

``id`` in the returned ``draft`` object is the Gmail **draft** id, which is what
``mail send-draft`` / ``mail delete-draft`` expect back (Gmail keys those on the
draft id, not the underlying message id).
"""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import Sequence
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as _default_policy
from typing import Any

from googleapiclient.errors import HttpError

from blumkin.config import BlumkinConfig, load_config
from blumkin.providers.google.mail import _html_to_text
from blumkin.providers.google_auth import get_credentials
from blumkin.providers.google_http import build_api_service, execute
from blumkin.skills.mail import (
    MailDraftNotFoundError,
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


def _build_message(
    *,
    subject: str,
    to: Sequence[str],
    cc: Sequence[str],
    bcc: Sequence[str],
    content: str,
    body_type: str,
    attachments: Sequence[tuple[str, bytes]],
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    if to:
        message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
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


def _get_draft(service: Any, draft_id: str) -> dict[str, Any]:
    try:
        return execute(service.users().drafts().get(userId="me", id=draft_id, format="raw"))
    except HttpError as exc:
        if _http_status(exc) == 404:
            raise MailDraftNotFoundError(f"message not found: {draft_id}") from exc
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


def _raw(message: EmailMessage) -> str:
    return base64.urlsafe_b64encode(message.as_bytes()).decode()
