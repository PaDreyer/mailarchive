from __future__ import annotations

from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parseaddr

from mailarchive.models import Attachment, ParsedMail


class _ArchiveEmailPolicy(policy.EmailPolicy):
    def header_fetch_parse(self, name: str, value: str) -> str:
        try:
            return super().header_fetch_parse(name, value)
        except IndexError:
            # Malformed structured headers can crash the stdlib parser, including
            # MIME parameters such as filename*="utf-8''". Recover at the policy
            # boundary: Content-Type is read while building the MIME tree, before
            # parse_mail can inspect individual parts. Keep the raw parameters so
            # Message's legacy MIME helpers can still recover names and boundaries.
            return str(policy.compat32.header_fetch_parse(name, value))


_ARCHIVE_POLICY = _ArchiveEmailPolicy()


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


def _address_header(message: Message, name: str) -> str | None:
    header = message.get(name)
    if not getattr(header, "defects", ()):
        return header

    # Some Python versions normalize malformed addresses to placeholders such as
    # <> with recorded header defects. Preserve the original text in that case.
    raw_value = next(
        (value for key, value in message.raw_items() if key.lower() == name.lower()), ""
    )
    return str(policy.compat32.header_fetch_parse(name, raw_value))


def _sender_address(message: Message) -> str:
    header = _address_header(message, "From")
    if header is None:
        return ""
    addresses = getattr(header, "addresses", None)
    if addresses is None:
        return parseaddr(header)[1]
    return addresses[0].addr_spec if addresses else ""


def parse_mail(raw: bytes) -> ParsedMail:
    message = BytesParser(policy=_ARCHIVE_POLICY).parsebytes(raw)
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

    recipient_headers = (_address_header(message, name) for name in ("To", "Cc", "Bcc"))
    recipients = ", ".join(str(header) for header in recipient_headers if header)
    return ParsedMail(
        raw=raw,
        subject=str(message.get("Subject", "(no subject)")),
        sender=_sender_address(message),
        recipients=recipients,
        body=_text_body(message),
        message_id=str(message.get("Message-ID", "")),
        date_header=str(message.get("Date", "")),
        attachments=attachments,
    )
