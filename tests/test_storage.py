import tempfile
import unittest
import sqlite3
from pathlib import Path

from mailarchive.mail_parser import parse_mail
from mailarchive.models import Condition, MailField, Rule, SaveMode
from mailarchive.storage import ArchiveState, ArchiveStorage, destination_path, safe_filename
from tests.helpers import sample_mail


class StorageTests(unittest.TestCase):
    def test_archives_eml_and_duplicate_attachment_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Archive"
            mail = parse_mail(
                sample_mail(
                    attachments=[("receipt.pdf", b"one"), ("receipt.pdf", b"two")]
                )
            )
            rule = Rule(
                "Invoices",
                "Finance/2026",
                [Condition(MailField.ALL)],
                SaveMode.EMAIL_AND_ATTACHMENTS,
            )
            result = ArchiveStorage(root).archive(mail, rule)
            self.assertEqual(len(result.files), 3)
            self.assertEqual(len(list((root / "Finance" / "2026").glob("*.eml"))), 1)
            attachment_names = sorted(path.name for path in result.files if path.suffix == ".pdf")
            self.assertEqual(attachment_names, ["receipt-2.pdf", "receipt.pdf"])

    def test_attachments_only_writes_no_eml(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            mail = parse_mail(sample_mail(attachments=[("image.png", b"png")]))
            rule = Rule("Images", "Images", save_mode=SaveMode.ATTACHMENTS_ONLY)
            result = ArchiveStorage(Path(temporary)).archive(mail, rule)
            self.assertEqual([path.suffix for path in result.files], [".png"])

    def test_destination_cannot_escape_archive(self) -> None:
        root = Path("/tmp/example-archive")
        for invalid in ("../private", "/etc", r"C:\\Windows", r"\\server\\share"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    destination_path(root, invalid)

    def test_windows_reserved_filename_is_safe(self) -> None:
        self.assertEqual(safe_filename("CON.txt"), "_CON.txt")

    def test_state_deduplicates_per_account_and_source_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = ArchiveState(root / "state.sqlite3")
            mail = parse_mail(sample_mail())
            rule = Rule("Other", "Inbox")
            archive_result = ArchiveStorage(root / "Archive").archive(mail, rule)
            state.record("account", "provider:inbox", "message-7", mail, rule, archive_result)
            self.assertTrue(state.was_processed("account", "provider:inbox", "message-7"))
            self.assertFalse(state.was_processed("account", "provider:archive", "message-7"))

    def test_legacy_imap_state_is_migrated_to_general_message_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE processed_mail (
                    account_id TEXT NOT NULL,
                    uid_validity TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    archived_at TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    rule_name TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    files_json TEXT NOT NULL,
                    PRIMARY KEY (account_id, uid_validity, uid)
                )
                """
            )
            connection.execute(
                "INSERT INTO processed_mail VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("account", "123", "7", "2026-01-01", "Subject", "Rule", "Inbox", "[]"),
            )
            connection.commit()
            connection.close()

            state = ArchiveState(database)

            self.assertTrue(state.was_processed("account", "imap:123", "7"))


if __name__ == "__main__":
    unittest.main()
