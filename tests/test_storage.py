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

    def test_email_only_writes_eml_without_attachment_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mail = parse_mail(sample_mail(attachments=[("image.png", b"png")]))
            rule = Rule("Email", "Inbox", save_mode=SaveMode.EMAIL_ONLY)

            result = ArchiveStorage(root).archive(mail, rule)

            self.assertEqual(len(result.files), 1)
            self.assertEqual(result.files[0].suffix, ".eml")
            self.assertEqual(result.files[0].read_bytes(), mail.raw)
            self.assertFalse(any(path.is_dir() for path in (root / "Inbox").glob("*_Attachments")))

    def test_attachments_only_with_no_attachments_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rule = Rule("Attachments", "Inbox", save_mode=SaveMode.ATTACHMENTS_ONLY)
            result = ArchiveStorage(Path(temporary)).archive(parse_mail(sample_mail()), rule)

            self.assertEqual(result.files, [])

    def test_destination_cannot_escape_archive(self) -> None:
        root = Path("/tmp/example-archive")
        for invalid in ("../private", "/etc", r"C:\\Windows", r"\\server\\share"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    destination_path(root, invalid)

    def test_windows_reserved_filename_is_safe(self) -> None:
        self.assertEqual(safe_filename("CON.txt"), "_CON.txt")

    def test_safe_filename_normalizes_invalid_blank_and_long_values(self) -> None:
        self.assertEqual(safe_filename("  ", "Fallback"), "Fallback")
        self.assertEqual(safe_filename("report:  2026?.pdf"), "report_ 2026_.pdf")
        self.assertEqual(len(safe_filename("x" * 200, max_length=12)), 12)

    def test_destination_normalizes_relative_segments(self) -> None:
        root = Path("/tmp/example-archive")
        self.assertEqual(destination_path(root, " Finance/./2026 "), root / "Finance" / "2026")
        for invalid in ("", " ", "."):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "cannot be empty"):
                    destination_path(root, invalid)

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
            self.assertEqual(
                state.processed_message_ids("account", "provider:inbox"), {"message-7"}
            )
            self.assertEqual(state.recent(1)[0]["subject"], mail.subject)

    def test_migrating_to_same_database_returns_existing_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = ArchiveState(Path(temporary) / "state.sqlite3")
            self.assertIs(state.migrated_to(state.database_path), state)

    def test_state_database_can_be_migrated_to_a_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_database = root / "original" / "state.sqlite3"
            state = ArchiveState(original_database)
            mail = parse_mail(sample_mail())
            rule = Rule("Other", "Inbox")
            archive_result = ArchiveStorage(root / "Archive").archive(mail, rule)
            state.record("account", "provider:inbox", "message-7", mail, rule, archive_result)

            migrated = state.migrated_to(root / "custom" / "mail.db")

            self.assertTrue(migrated.was_processed("account", "provider:inbox", "message-7"))
            self.assertTrue(original_database.exists())
            self.assertEqual(migrated.database_path, (root / "custom" / "mail.db").resolve())

    def test_migrating_to_an_existing_database_merges_processing_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = ArchiveState(root / "source.sqlite3")
            destination = ArchiveState(root / "destination.sqlite3")
            mail = parse_mail(sample_mail())
            rule = Rule("Other", "Inbox")
            archive_result = ArchiveStorage(root / "Archive").archive(mail, rule)
            source.record("account", "provider:inbox", "source-message", mail, rule, archive_result)
            destination.record(
                "account",
                "provider:inbox",
                "destination-message",
                mail,
                rule,
                archive_result,
            )

            migrated = source.migrated_to(destination.database_path)

            self.assertTrue(
                migrated.was_processed("account", "provider:inbox", "source-message")
            )
            self.assertTrue(
                migrated.was_processed("account", "provider:inbox", "destination-message")
            )

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
