from __future__ import annotations

from email import policy
from email.message import Message
from email.parser import BytesParser

from mailarchive.models import Attachment, ParsedMail


def _text_body(message: Message) -> str:
    if message.is_multipart():
        plain_parts: list[str] = []
        html_parts: list[str] = []
        for part in message.walk():
            if part.is_multipart() or part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            if content_type not in {"text/plain", "text/html"}:
                continue
            try:
                content = part.get_content()
            except (LookupError, UnicodeError):
                payload = part.get_payload(decode=True) or b""
                content = payload.decode("utf-8", errors="replace")
            if content_type == "text/plain":
                plain_parts.append(str(content))
            else:
                html_parts.append(str(content))
        return "\n".join(plain_parts or html_parts)
    try:
        return str(message.get_content())
    except (LookupError, UnicodeError):
        return (message.get_payload(decode=True) or b"").decode("utf-8", errors="replace")


def parse_mail(raw: bytes) -> ParsedMail:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    attachments: list[Attachment] = []
    for part in message.walk():
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if not filename and disposition != "attachment":
            continue
        content = part.get_payload(decode=True)
        if content is None:
            continue
        attachments.append(Attachment(filename=filename or "Attachment", content=content))

    recipients = ", ".join(
        str(message.get(header, "")) for header in ("To", "Cc", "Bcc") if message.get(header)
    )
    return ParsedMail(
        raw=raw,
        subject=str(message.get("Subject", "(no subject)")),
        sender=str(message.get("From", "")),
        recipients=recipients,
        body=_text_body(message),
        message_id=str(message.get("Message-ID", "")),
        date_header=str(message.get("Date", "")),
        attachments=attachments,
    )
