import unittest
from email.message import EmailMessage
from itertools import product
from unittest.mock import MagicMock

from mailarchive.mail_parser import _text_body, parse_mail
from tests.helpers import mail_with_attachment_headers, sample_mail


class MailParserTests(unittest.TestCase):
    def test_extracts_headers_body_and_attachments(self) -> None:
        parsed = parse_mail(
            sample_mail(
                subject="March invoice",
                attachments=[("invoice.pdf", b"%PDF-test")],
            )
        )
        self.assertEqual(parsed.subject, "March invoice")
        self.assertEqual(parsed.sender, "invoices@example.com")
        self.assertIn("attached", parsed.body)
        self.assertEqual(parsed.attachments[0].filename, "invoice.pdf")
        self.assertEqual(parsed.attachments[0].content, b"%PDF-test")

    def test_html_body_is_used_when_plain_text_is_absent(self) -> None:
        message = EmailMessage()
        message["From"] = "sender@example.com"
        message["To"] = "to@example.com"
        message["Cc"] = "copy@example.com"
        message["Bcc"] = "hidden@example.com"
        message.set_content("<strong>HTML only</strong>", subtype="html")

        parsed = parse_mail(message.as_bytes())

        self.assertIn("HTML only", parsed.body)
        self.assertEqual(
            parsed.recipients,
            "to@example.com, copy@example.com, hidden@example.com",
        )
        self.assertEqual(parsed.subject, "(no subject)")
        self.assertEqual(parsed.message_id, "")

    def test_broken_address_headers_preserve_message_and_attachment_content(self) -> None:
        for header, value in product(("From", "To", "Cc", "Bcc"), ('"', '""', "bad@@example.com")):
            with self.subTest(header=header, value=value):
                raw = f"{header}: {value}\r\n".encode() + sample_mail(
                    subject="Überweisung", attachments=[("invoice.pdf", b"%PDF-test")]
                )

                parsed = parse_mail(raw)

                self.assertEqual(parsed.raw, raw)
                self.assertEqual(parsed.subject, "Überweisung")
                self.assertEqual(parsed.sender, "" if header == "From" else "invoices@example.com")
                self.assertIn("Your invoice is attached.", parsed.body)
                self.assertEqual(parsed.attachments[0].filename, "invoice.pdf")
                self.assertEqual(parsed.attachments[0].content, b"%PDF-test")
                if header != "From":
                    self.assertEqual(
                        parsed.recipients,
                        value if header == "To" else f"customer@example.org, {value}",
                    )

    def test_empty_and_group_address_headers_do_not_require_a_sender(self) -> None:
        for value in ("", "undisclosed-recipients:;", "<>", '"Name" <>'):
            with self.subTest(value=value):
                parsed = parse_mail(f"From: {value}\r\n\r\nBody\r\n".encode())
                self.assertEqual(parsed.sender, "")
                self.assertIn("Body", parsed.body)

    def test_attachment_without_filename_gets_fallback_name(self) -> None:
        raw = (
            b"From: sender@example.com\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=x\r\n\r\n"
            b"--x\r\nContent-Type: text/plain\r\n\r\nBody\r\n"
            b"--x\r\nContent-Disposition: attachment\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\nZGF0YQ==\r\n"
            b"--x--\r\n"
        )

        parsed = parse_mail(raw)

        self.assertEqual(parsed.attachments[0].filename, "Attachment")
        self.assertEqual(parsed.attachments[0].content, b"data")

    def test_malformed_mime_parameters_preserve_attachment_names_and_bytes(self) -> None:
        cases = (
            (b"Content-Disposition: attachment; filename*=\"utf-8''\"", "Attachment"),
            (b"Content-Disposition: attachment; filename*=\"''\"", "Attachment"),
            (
                b"Content-Disposition: attachment; filename*0*=\"utf-8''\";\r\n"
                b" filename*1*=Rechnung-%C3%A4.pdf",
                "Rechnung-ä.pdf",
            ),
            (
                b"Content-Type: application/pdf; name*=\"utf-8''\"\r\n"
                b'Content-Disposition: attachment; filename="invoice.pdf"',
                "invoice.pdf",
            ),
            (
                b"Content-Type: application/pdf; name*0*=\"utf-8''\";\r\n name*1*=invoice.pdf",
                "invoice.pdf",
            ),
        )
        for headers, filename in cases:
            with self.subTest(headers=headers):
                raw = mail_with_attachment_headers(headers)

                parsed = parse_mail(raw)

                self.assertEqual(parsed.raw, raw)
                self.assertEqual(parsed.subject, "Invoice")
                self.assertEqual(parsed.sender, "invoices@example.com")
                self.assertEqual(parsed.recipients, "customer@example.org")
                self.assertIn("Your invoice is attached.", parsed.body)
                self.assertEqual(len(parsed.attachments), 1)
                self.assertEqual(parsed.attachments[0].filename, filename)
                self.assertEqual(parsed.attachments[0].content, b"%PDF-test")

    def test_malformed_root_and_body_mime_parameters_preserve_multipart_structure(self) -> None:
        raw = mail_with_attachment_headers(
            b'Content-Disposition: attachment; filename="invoice.pdf"'
        )
        for original, replacement in (
            (
                b"multipart/mixed; boundary=invoice",
                b"multipart/mixed; boundary=invoice; name*=\"utf-8''\"",
            ),
            (
                b"text/plain; charset=utf-8",
                b"text/plain; charset=utf-8; name*=\"utf-8''\"",
            ),
        ):
            with self.subTest(original=original):
                parsed = parse_mail(raw.replace(original, replacement))

                self.assertIn("Your invoice is attached.", parsed.body)
                self.assertEqual(len(parsed.attachments), 1)
                self.assertEqual(parsed.attachments[0].filename, "invoice.pdf")
                self.assertEqual(parsed.attachments[0].content, b"%PDF-test")

    def test_body_decode_failure_falls_back_to_replacement_text(self) -> None:
        message = MagicMock()
        message.is_multipart.return_value = False
        message.get_content.side_effect = UnicodeError("bad encoding")
        message.get_payload.return_value = b"broken-\xff"

        self.assertEqual(_text_body(message), "broken-�")

    def test_multipart_body_skips_non_text_and_uses_decode_fallback(self) -> None:
        container = MagicMock()
        container.is_multipart.return_value = True

        nested = MagicMock()
        nested.is_multipart.return_value = True
        attachment = MagicMock()
        attachment.is_multipart.return_value = False
        attachment.get_content_disposition.return_value = "attachment"
        binary = MagicMock()
        binary.is_multipart.return_value = False
        binary.get_content_disposition.return_value = None
        binary.get_content_type.return_value = "application/octet-stream"
        text = MagicMock()
        text.is_multipart.return_value = False
        text.get_content_disposition.return_value = None
        text.get_content_type.return_value = "text/plain"
        text.get_content.side_effect = LookupError("unknown charset")
        text.get_payload.return_value = b"fallback"
        container.walk.return_value = [nested, attachment, binary, text]

        self.assertEqual(_text_body(container), "fallback")


if __name__ == "__main__":
    unittest.main()
