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
