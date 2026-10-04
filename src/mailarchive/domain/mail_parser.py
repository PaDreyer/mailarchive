from __future__ import annotations

import re
from email import policy
from email.message import Message
from email.parser import BytesHeaderParser, BytesParser, HeaderParser
from email.utils import parseaddr

from mailarchive.domain.configuration import Attachment, MailHeaders, ParsedMail


class _ArchiveEmailPolicy(policy.EmailPolicy):
    def header_fetch_parse(self, name: str, value: str) -> str:
        try:
            if name.lower() == "message-id":
                wrapped = value.strip()
                if wrapped.startswith("<[") and wrapped.endswith("]>"):
                    # Parse <[id@domain]> as <id@domain>. Accept the inner ID only
                    # when the structured parser validates it without defects.
                    # This changes parsed metadata; ParsedMail.raw stays intact.
                    header = super().header_fetch_parse(name, f"<{wrapped[2:-2]}>")
                    if not header.defects:
                        return header
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


def _known_header(message: Message, name: str) -> bool:
    values = message.get_all(name, [])
    return len(values) == 1 and hasattr(values[0], "defects") and not values[0].defects


def _mail_headers(message: Message) -> MailHeaders:
    if message.defects:
        return MailHeaders()
    recipient_names = ("To", "Cc", "Bcc")
    recipient_headers = [_address_header(message, name) for name in recipient_names]
    recipients_known = any(recipient_headers) and all(
        not message.get_all(name) or _known_header(message, name) for name in recipient_names
    )
    return MailHeaders(
        sender=_sender_address(message) if _known_header(message, "From") else None,
        recipients=(
            ", ".join(str(header) for header in recipient_headers if header)
            if recipients_known
            else None
        ),
        subject=str(message["Subject"]) if _known_header(message, "Subject") else None,
        date_header=str(message["Date"]) if _known_header(message, "Date") else None,
    )


def parse_headers(raw: bytes) -> MailHeaders:
    """Use the MIME parser's header policy, falling back for ambiguous metadata."""
    if b"\r\n\r\n" not in raw and b"\n\n" not in raw:
        return MailHeaders()
    try:
        return _mail_headers(BytesHeaderParser(policy=_ARCHIVE_POLICY).parsebytes(raw))
    except (ValueError, UnicodeError, IndexError):
        return MailHeaders()


def parse_header_pairs(pairs: list[tuple[str, str]]) -> MailHeaders:
    if any(
        any(character in name for character in ":\r\n") or re.search(r"\r(?!\n)|\n(?![ \t])", value)
        for name, value in pairs
    ):
        return MailHeaders()
    try:
        text = "".join(f"{name}: {value}\n" for name, value in pairs) + "\n"
        return _mail_headers(HeaderParser(policy=_ARCHIVE_POLICY).parsestr(text))
    except (ValueError, UnicodeError, IndexError):
        return MailHeaders()


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
