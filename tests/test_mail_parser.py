import unittest

from mailarchive.mail_parser import parse_mail
from tests.helpers import sample_mail


class MailParserTests(unittest.TestCase):
    def test_extracts_headers_body_and_attachments(self) -> None:
        parsed = parse_mail(
            sample_mail(
                subject="March invoice",
                attachments=[("invoice.pdf", b"%PDF-test")],
            )
        )
        self.assertEqual(parsed.subject, "March invoice")
        self.assertIn("invoices@example.com", parsed.sender)
        self.assertIn("attached", parsed.body)
        self.assertEqual(parsed.attachments[0].filename, "invoice.pdf")
        self.assertEqual(parsed.attachments[0].content, b"%PDF-test")


if __name__ == "__main__":
    unittest.main()
