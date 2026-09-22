from email.message import EmailMessage


def sample_mail(
    subject: str = "Monthly invoice",
    sender: str = "Accounting <invoices@example.com>",
    body: str = "Your invoice is attached.",
    attachments: list[tuple[str, bytes]] | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "customer@example.org"
    message["Subject"] = subject
    message["Date"] = "Fri, 11 Sep 2026 09:30:00 +0200"
    message["Message-ID"] = "<example-123@example.com>"
    message.set_content(body)
    for filename, content in attachments or []:
        message.add_attachment(
            content,
            maintype="application",
            subtype="octet-stream",
            filename=filename,
        )
    return message.as_bytes()


def mail_with_attachment_headers(headers: bytes) -> bytes:
    return (
        b"From: invoices@example.com\r\n"
        b"To: customer@example.org\r\n"
        b"Subject: Invoice\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=invoice\r\n\r\n"
        b"--invoice\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Your invoice is attached.\r\n"
        b"--invoice\r\n" + headers + b"\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi10ZXN0\r\n"
        b"--invoice--\r\n"
    )


def mail_target(account, *, mailbox=None, folder=None):
    from mailarchive.mail_identity import MailTarget
    from mailarchive.models import Mailbox

    mailbox = mailbox or (account.mailboxes[0] if account.mailboxes else Mailbox(account.username))
    return MailTarget(
        account,
        mailbox,
        folder if folder is not None else (mailbox.folders[0] if mailbox.folders else "INBOX"),
    )


def imap_namespace(account, uid_validity):
    from mailarchive.mail_identity import imap_scope

    return imap_scope(mail_target(account), uid_validity).processing_namespace
