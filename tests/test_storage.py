import errno
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from unittest.mock import patch

from mailarchive.mail_parser import parse_mail
from mailarchive.models import Condition, DateFolderPosition, MailField, Rule, SaveMode
from mailarchive.storage import (
    ArchiveState,
    ArchiveStorage,
    _atomic_write,
    _publish_file,
    destination_path,
    safe_filename,
)
from tests.helpers import sample_mail


class StorageTests(unittest.TestCase):
    def test_direct_attachments_follow_date_folders_and_all_save_modes(self) -> None:
        mail = parse_mail(sample_mail(attachments=[("receipt.pdf", b"receipt")]))
        for position in DateFolderPosition:
            for subfolder in ("", "Finance/Supplier"):
                for mode in SaveMode:
                    with (
                        self.subTest(position=position, subfolder=subfolder, mode=mode),
                        tempfile.TemporaryDirectory() as temporary,
                    ):
                        root = Path(temporary)
                        folders = ["Finance", "Supplier"] if subfolder else []
                        if position == DateFolderPosition.BEFORE_SUBFOLDER:
                            folders = ["2026", "09"] + folders
                        elif position == DateFolderPosition.AFTER_SUBFOLDER:
                            folders += ["2026", "09"]
                        target = root.joinpath(*folders)
                        rule = Rule(
                            "Invoices",
                            subfolder,
                            save_mode=mode,
                            date_folder_position=position,
                            attachments_in_destination=True,
                        )
                        result = ArchiveStorage(root).archive(mail, rule)
                        self.assertEqual(result.destination, target)
                        self.assertTrue(all(path.parent == target for path in result.files))
                        self.assertEqual(
                            len(result.files), 2 if mode == SaveMode.EMAIL_AND_ATTACHMENTS else 1
                        )
                        self.assertFalse(any(path.is_dir() for path in target.iterdir()))
                        if mode != SaveMode.EMAIL_ONLY:
                            self.assertEqual((target / "receipt.pdf").read_bytes(), b"receipt")
                        if mode != SaveMode.ATTACHMENTS_ONLY:
                            self.assertEqual(next(target.glob("*.eml")).read_bytes(), mail.raw)

    def test_direct_attachments_preserve_collisions_and_reuse_files_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage = ArchiveStorage(root)
            rule = Rule(
                "Invoices", save_mode=SaveMode.ATTACHMENTS_ONLY, attachments_in_destination=True
            )
            first = parse_mail(sample_mail(attachments=[("receipt.pdf", b"first")]))
            second = parse_mail(sample_mail(attachments=[("RECEIPT.pdf", b"second")]))

            first_result = storage.archive(first, rule)
            second_result = storage.archive(second, rule)

            self.assertEqual(first_result.files, [root / "receipt.pdf"])
            self.assertEqual(second_result.files, [root / "RECEIPT-2.pdf"])
            self.assertEqual(storage.archive(first, rule), first_result)
            self.assertEqual(storage.archive(second, rule), second_result)
            self.assertEqual(
                {path.name: path.read_bytes() for path in root.iterdir()},
                {"receipt.pdf": b"first", "RECEIPT-2.pdf": b"second"},
            )

    def test_direct_attachments_keep_duplicate_occurrences_and_sanitized_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage = ArchiveStorage(root)
            rule = Rule(
                "Invoices", save_mode=SaveMode.ATTACHMENTS_ONLY, attachments_in_destination=True
            )
            mail = parse_mail(
                sample_mail(
                    attachments=[
                        ("report?.pdf", b"same"),
                        ("report*.pdf", b"same"),
                        ("REPORT_.pdf", b"different"),
                        ("report_-2.pdf", b"numbered"),
                    ]
                )
            )

            result = storage.archive(mail, rule)

            self.assertEqual(
                [(path.name, path.read_bytes()) for path in result.files],
                [
                    ("report_.pdf", b"same"),
                    ("report_-2.pdf", b"same"),
                    ("REPORT_-3.pdf", b"different"),
                    ("report_-2-2.pdf", b"numbered"),
                ],
            )
            self.assertEqual(storage.archive(mail, rule), result)
            self.assertEqual(len(list(root.iterdir())), 4)

    def test_direct_attachments_skip_existing_directories_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Archive"
            root.mkdir()
            (root / "receipt.pdf").mkdir()
            outside = Path(temporary) / "outside.pdf"
            outside.write_bytes(b"outside")
            try:
                (root / "receipt-2.pdf").symlink_to(outside)
            except OSError:
                self.skipTest("File symlinks are unavailable")
            mail = parse_mail(sample_mail(attachments=[("receipt.pdf", b"outside")]))
            rule = Rule(
                "Invoices", save_mode=SaveMode.ATTACHMENTS_ONLY, attachments_in_destination=True
            )

            result = ArchiveStorage(root).archive(mail, rule)

            self.assertEqual(result.files, [root / "receipt-3.pdf"])
            self.assertEqual(outside.read_bytes(), b"outside")
            self.assertTrue((root / "receipt.pdf").is_dir())
            self.assertTrue((root / "receipt-2.pdf").is_symlink())

    def test_email_does_not_overwrite_a_file_from_an_earlier_flat_archive(self) -> None:
        for direct in (False, True):
            with self.subTest(direct=direct), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                storage = ArchiveStorage(root)
                rule = Rule(
                    "Mail", save_mode=SaveMode.EMAIL_ONLY, attachments_in_destination=direct
                )
                mail = parse_mail(sample_mail())
                original = storage.archive(mail, rule).files[0]
                original.write_bytes(b"existing attachment")

                result = storage.archive(mail, rule)

                self.assertEqual(original.read_bytes(), b"existing attachment")
                self.assertEqual(result.files, [original.with_stem(original.stem + "-2")])
                self.assertEqual(result.files[0].read_bytes(), mail.raw)
                self.assertEqual(storage.archive(mail, rule), result)
                self.assertEqual(len(list(root.iterdir())), 2)

    def test_direct_attachment_cannot_reuse_the_email_file_in_the_same_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage = ArchiveStorage(root)
            rule = Rule("Mail", attachments_in_destination=True)
            mail = parse_mail(sample_mail(attachments=[("forward.eml", b"forwarded")]))
            email_path = storage.archive(mail, replace(rule, save_mode=SaveMode.EMAIL_ONLY)).files[
                0
            ]
            # Simulate an attachment whose original name collides with the email's generated name.
            mail.attachments[0].filename = email_path.name
            mail.attachments[0].content = mail.raw

            result = storage.archive(mail, rule)

            self.assertEqual(
                result.files, [email_path, email_path.with_stem(email_path.stem + "-2")]
            )
            self.assertTrue(all(path.read_bytes() == mail.raw for path in result.files))
            self.assertEqual(storage.archive(mail, rule), result)

    def test_partial_archive_failure_cleans_up_and_retry_reuses_completed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage = ArchiveStorage(root)
            rule = Rule("Invoices", attachments_in_destination=True)
            mail = parse_mail(sample_mail(attachments=[("one.pdf", b"one"), ("two.pdf", b"two")]))

            def fail_second_attachment(source, destination):
                if destination.name == "two.pdf":
                    raise OSError("disk full")
                return _publish_file(source, destination)

            with patch("mailarchive.storage._publish_file", side_effect=fail_second_attachment):
                with self.assertRaisesRegex(OSError, "disk full"):
                    storage.archive(mail, rule)

            self.assertEqual(len(list(root.iterdir())), 2)
            self.assertFalse((root / "two.pdf").exists())
            result = storage.archive(mail, rule)
            self.assertEqual(len(list(root.iterdir())), 3)
            self.assertEqual([path.name for path in result.files[1:]], ["one.pdf", "two.pdf"])
            self.assertEqual(storage.archive(mail, rule), result)

    def test_file_created_after_directory_scan_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rule = Rule(
                "Invoices", save_mode=SaveMode.ATTACHMENTS_ONLY, attachments_in_destination=True
            )
            mail = parse_mail(sample_mail(attachments=[("receipt.pdf", b"new")]))
            raced = False

            def create_competing_file(source, destination):
                nonlocal raced
                if not raced:
                    raced = True
                    destination.write_bytes(b"existing")
                return _publish_file(source, destination)

            with patch("mailarchive.storage._publish_file", side_effect=create_competing_file):
                result = ArchiveStorage(root).archive(mail, rule)

            self.assertEqual(result.files, [root / "receipt-2.pdf"])
            self.assertEqual((root / "receipt.pdf").read_bytes(), b"existing")
            self.assertEqual((root / "receipt-2.pdf").read_bytes(), b"new")
            self.assertEqual(len(list(root.iterdir())), 2)

    def test_process_abort_never_leaves_an_empty_final_file_and_retry_keeps_original_name(self):
        script = textwrap.dedent("""
            import os
            import sys
            from pathlib import Path
            from types import SimpleNamespace
            import mailarchive.storage as storage

            target, phase, method = sys.argv[1:]
            if method == "link":
                storage.ctypes.CDLL = lambda *args, **kwargs: SimpleNamespace()
            publish = storage._publish_file
            def crash(source, destination):
                if phase == "after":
                    publish(source, destination)
                os._exit(73)
            storage._publish_file = crash
            storage._atomic_write(Path(target), b"invoice")
        """)
        methods = ("native", "link") if sys.platform == "linux" else ("native",)
        for phase in ("before", "after"):
            for method in methods:
                with (
                    self.subTest(phase=phase, method=method),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary)
                    original = root / "invoice.pdf"
                    child = subprocess.run(
                        [sys.executable, "-c", script, str(original), phase, method],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    self.assertEqual(child.returncode, 73, child.stderr)
                    if phase == "before":
                        self.assertFalse(original.exists())
                    else:
                        self.assertEqual(original.read_bytes(), b"invoice")

                    rule = Rule(
                        "Invoices",
                        save_mode=SaveMode.ATTACHMENTS_ONLY,
                        attachments_in_destination=True,
                    )
                    mail = parse_mail(sample_mail(attachments=[("invoice.pdf", b"invoice")]))
                    result = ArchiveStorage(root).archive(mail, rule)

                    self.assertEqual(result.files, [original])
                    self.assertEqual(original.read_bytes(), b"invoice")
                    self.assertEqual(list(root.glob("*.pdf")), [original])

    def test_preexisting_empty_file_is_not_treated_as_an_abandoned_reservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "invoice.pdf"
            original.write_bytes(b"")
            rule = Rule(
                "Invoices", save_mode=SaveMode.ATTACHMENTS_ONLY, attachments_in_destination=True
            )
            mail = parse_mail(sample_mail(attachments=[("invoice.pdf", b"invoice")]))

            result = ArchiveStorage(root).archive(mail, rule)

            self.assertEqual(original.read_bytes(), b"")
            self.assertEqual(result.files, [root / "invoice-2.pdf"])
            self.assertEqual(result.files[0].read_bytes(), b"invoice")

    @unittest.skipUnless(sys.platform == "linux", "Linux publication fallback")
    def test_link_fallback_is_exclusive_when_rename_noreplace_is_unavailable(self):
        for error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            with (
                self.subTest(error=error),
                tempfile.TemporaryDirectory() as temporary,
                patch("mailarchive.storage.ctypes.CDLL") as libc,
                patch("mailarchive.storage.ctypes.get_errno", return_value=error),
            ):
                libc.return_value.renameat2.return_value = -1
                target = Path(temporary) / "invoice.pdf"
                _atomic_write(target, b"original")
                with self.assertRaises(FileExistsError):
                    _atomic_write(target, b"replacement")
                self.assertEqual(target.read_bytes(), b"original")
                self.assertEqual(list(Path(temporary).iterdir()), [target])

    @unittest.skipUnless(sys.platform == "linux", "Linux publication fallback")
    def test_unsupported_publication_fails_without_creating_a_final_file(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("mailarchive.storage.ctypes.CDLL") as libc,
            patch("mailarchive.storage.ctypes.get_errno", return_value=errno.EOPNOTSUPP),
            patch(
                "mailarchive.storage.os.link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")
            ),
        ):
            libc.return_value.renameat2.return_value = -1
            root = Path(temporary)
            with self.assertRaises(OSError):
                _atomic_write(root / "invoice.pdf", b"invoice")
            self.assertEqual(list(root.iterdir()), [])

    def test_shared_destination_is_scanned_once_per_run_for_every_save_mode(self):
        real_iterdir = Path.iterdir
        scans, entries = {}, {}

        def counted_iterdir(directory):
            scans[directory] = scans.get(directory, 0) + 1
            for path in real_iterdir(directory):
                entries[directory] = entries.get(directory, 0) + 1
                yield path

        for mode in SaveMode:
            for direct in (False, True):
                with (
                    self.subTest(mode=mode, direct=direct),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary)
                    for index in range(128):
                        (root / f"existing-{index}.txt").touch()
                    storage = ArchiveStorage(root)
                    rule = Rule("Invoices", save_mode=mode, attachments_in_destination=direct)
                    with patch.object(Path, "iterdir", new=counted_iterdir):
                        for index in range(40):
                            mail = parse_mail(
                                sample_mail(
                                    subject=f"Invoice {index}",
                                    attachments=[(f"invoice-{index}.pdf", b"invoice")],
                                )
                            )
                            result = storage.archive(mail, rule)
                    self.assertEqual(scans[root], 1)
                    self.assertEqual(entries[root], 128)
                    self.assertEqual(storage.archive(mail, rule), result)

    def test_cached_destination_reconciles_external_additions_renames_and_removals(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def mark_external_change():
                # Some filesystems give several operations the same timestamp.
                # Make each external edit a distinct metadata change without sleeps.
                info = root.stat()
                os.utime(root, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))

            storage = ArchiveStorage(root)
            rule = Rule(
                "Invoices", save_mode=SaveMode.ATTACHMENTS_ONLY, attachments_in_destination=True
            )
            first = parse_mail(sample_mail(attachments=[("first.pdf", b"first")]))
            storage.archive(first, rule)
            external = root / "INVOICE.PDF"
            external.write_bytes(b"external")
            mark_external_change()
            mail = parse_mail(sample_mail(attachments=[("invoice.pdf", b"invoice")]))

            result = storage.archive(mail, rule)
            self.assertEqual(result.files, [root / "invoice-2.pdf"])
            self.assertEqual(external.read_bytes(), b"external")
            renamed = root / "INVOICE-RENAMED.PDF"
            result.files[0].rename(renamed)
            mark_external_change()
            renamed_mail = parse_mail(
                sample_mail(attachments=[("invoice-renamed.pdf", b"invoice")])
            )
            self.assertEqual(storage.archive(renamed_mail, rule).files, [renamed])
            external.unlink()
            mark_external_change()
            self.assertEqual(storage.archive(mail, rule).files, [root / "invoice.pdf"])

    def test_retry_reconciles_case_rename_that_coincides_with_another_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage = ArchiveStorage(root)
            rule = Rule(
                "Invoices", save_mode=SaveMode.ATTACHMENTS_ONLY, attachments_in_destination=True
            )
            first = parse_mail(sample_mail(attachments=[("first.pdf", b"first")]))
            second = parse_mail(sample_mail(attachments=[("second.pdf", b"second")]))
            original = storage.archive(first, rule).files[0]
            renamed = root / "FIRST.pdf"

            def publish_with_external_rename(source, destination):
                original.rename(renamed)
                _publish_file(source, destination)

            with patch(
                "mailarchive.storage._publish_file", side_effect=publish_with_external_rename
            ):
                storage.archive(second, rule)
            result = storage.archive(first, rule)

            self.assertEqual(len(result.files), 1)
            self.assertTrue(result.files[0].samefile(renamed))
            self.assertEqual(result.files[0].read_bytes(), b"first")
            self.assertEqual({path.name for path in root.iterdir()}, {"FIRST.pdf", "second.pdf"})

    def test_direct_attachments_only_without_attachments_creates_no_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rule = Rule(
                "Invoices",
                "Finance",
                save_mode=SaveMode.ATTACHMENTS_ONLY,
                attachments_in_destination=True,
            )
            result = ArchiveStorage(root).archive(parse_mail(sample_mail()), rule)
            self.assertEqual(result.files, [])
            self.assertFalse(result.destination.exists())

    def test_archive_destinations_cover_both_orders_root_and_all_save_modes(self) -> None:
        mail = parse_mail(sample_mail(attachments=[("receipt.pdf", b"receipt")]))
        for position in DateFolderPosition:
            for subfolder in ("", r"Finance\Supplier"):
                for mode in SaveMode:
                    with (
                        self.subTest(position=position, subfolder=subfolder, mode=mode),
                        tempfile.TemporaryDirectory() as temporary,
                    ):
                        root = Path(temporary) / "Archive"
                        folders = ["Finance", "Supplier"] if subfolder else []
                        if position == DateFolderPosition.BEFORE_SUBFOLDER:
                            folders = ["2026", "09"] + folders
                        elif position == DateFolderPosition.AFTER_SUBFOLDER:
                            folders += ["2026", "09"]
                        target = root.joinpath(*folders)
                        rule = Rule(
                            "Invoices", subfolder, save_mode=mode, date_folder_position=position
                        )
                        result = ArchiveStorage(root).archive(mail, rule)
                        self.assertEqual(result.destination, target)
                        emails = list(target.glob("*.eml"))
                        attachments = list(target.glob("*_Attachments/receipt.pdf"))
                        self.assertEqual(len(emails), int(mode != SaveMode.ATTACHMENTS_ONLY))
                        self.assertEqual(len(attachments), int(mode != SaveMode.EMAIL_ONLY))
                        if emails:
                            self.assertEqual(emails[0].read_bytes(), mail.raw)
                        if attachments:
                            self.assertEqual(attachments[0].read_bytes(), b"receipt")
                        self.assertEqual(set(result.files), set(emails + attachments))

    def test_date_folders_and_filename_use_the_same_local_mail_date(self) -> None:
        for header in (
            "Thu, 31 Dec 2026 23:30:00 -0500",
            "Fri, 01 Jan 2027 00:30:00 +1400",
            "Sun, 01 Nov 2026 00:30:00 +0200",
            "Fri, 11 Sep 2026 09:30:00",
        ):
            with self.subTest(header=header), tempfile.TemporaryDirectory() as temporary:
                mail = parse_mail(sample_mail())
                mail.date_header = header
                expected = parsedate_to_datetime(header)
                if expected.tzinfo is None:
                    expected = expected.replace(tzinfo=timezone.utc)
                expected = expected.astimezone()
                root = Path(temporary)
                rule = Rule(
                    "Mail", "Inbox", date_folder_position=DateFolderPosition.AFTER_SUBFOLDER
                )
                result = ArchiveStorage(root).archive(mail, rule)
                self.assertEqual(
                    result.destination,
                    root / "Inbox" / expected.strftime("%Y") / expected.strftime("%m"),
                )
                self.assertTrue(
                    result.files[0].name.startswith(expected.strftime("%Y-%m-%d_%H-%M-%S"))
                )

    def test_missing_or_invalid_date_uses_one_archive_timestamp(self) -> None:
        fallback = datetime(2027, 1, 1, 12, 0, tzinfo=timezone.utc).astimezone()
        for header in ("", "invalid date"):
            with (
                self.subTest(header=header),
                tempfile.TemporaryDirectory() as temporary,
                patch("mailarchive.storage.datetime", wraps=datetime) as clock,
            ):
                clock.now.return_value = fallback
                mail = parse_mail(sample_mail())
                mail.date_header = header
                root = Path(temporary)
                rule = Rule("Mail", "", date_folder_position=DateFolderPosition.BEFORE_SUBFOLDER)
                result = ArchiveStorage(root).archive(mail, rule)
                self.assertEqual(result.destination, root / "2027" / "01")
                self.assertTrue(
                    result.files[0].name.startswith(fallback.strftime("%Y-%m-%d_%H-%M-%S"))
                )
                clock.now.assert_called_once_with()

    def test_complete_dated_destination_rejects_symlink_escape(self) -> None:
        for position, link_parts in (
            (DateFolderPosition.BEFORE_SUBFOLDER, ("2026", "09", "Finance")),
            (DateFolderPosition.AFTER_SUBFOLDER, ("Finance", "2026", "09")),
        ):
            with self.subTest(position=position), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "Archive"
                outside = Path(temporary) / "Outside"
                outside.mkdir()
                link = root.joinpath(*link_parts)
                link.parent.mkdir(parents=True)
                try:
                    link.symlink_to(outside, target_is_directory=True)
                except OSError:
                    self.skipTest("Directory symlinks are unavailable")
                rule = Rule("Mail", "Finance", date_folder_position=position)
                with self.assertRaisesRegex(ValueError, "inside the archive"):
                    ArchiveStorage(root).archive(parse_mail(sample_mail()), rule)
                self.assertEqual(list(outside.iterdir()), [])

    def test_archives_eml_and_duplicate_attachment_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Archive"
            mail = parse_mail(
                sample_mail(attachments=[("receipt.pdf", b"one"), ("receipt.pdf", b"two")])
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
            self.assertFalse(result.destination.exists())

    def test_destination_cannot_escape_archive(self) -> None:
        root = Path("/tmp/example-archive")
        for invalid in ("../private", "/etc", " /etc ", r"C:\\Windows", r"\\server\\share"):
            for position in DateFolderPosition:
                with self.subTest(invalid=invalid, position=position):
                    with self.assertRaises(ValueError):
                        destination_path(root, invalid, position)

    def test_windows_reserved_filename_is_safe(self) -> None:
        self.assertEqual(safe_filename("CON.txt"), "_CON.txt")

    def test_safe_filename_normalizes_invalid_blank_and_long_values(self) -> None:
        self.assertEqual(safe_filename("  ", "Fallback"), "Fallback")
        self.assertEqual(safe_filename("report:  2026?.pdf"), "report_ 2026_.pdf")
        self.assertEqual(len(safe_filename("x" * 200, max_length=12)), 12)

    def test_destination_normalizes_relative_segments(self) -> None:
        root = Path("/tmp/example-archive")
        self.assertEqual(destination_path(root, " Finance/./2026 "), root / "Finance" / "2026")
        for empty in ("", " ", "."):
            with self.subTest(empty=empty):
                self.assertEqual(destination_path(root, empty), root)

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

    def test_initial_scan_checkpoint_can_hide_and_later_reveal_skipped_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = ArchiveState(Path(temporary) / "state.sqlite3")

            self.assertFalse(state.has_completed_initial_scan("account", "provider:inbox"))
            state.complete_initial_scan("account", "provider:inbox", {"existing-1", "existing-2"})

            self.assertTrue(state.has_completed_initial_scan("account", "provider:inbox"))
            self.assertEqual(
                state.processed_message_ids("account", "provider:inbox", include_skipped=True),
                {"existing-1", "existing-2"},
            )
            self.assertEqual(state.processed_message_ids("account", "provider:inbox"), set())

    def test_unmatched_checks_are_persistent_and_specific_to_account_namespace_and_rules(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            state = ArchiveState(path)
            state.record_unmatched("account", "provider:inbox", "1", "rules-v1")
            state = ArchiveState(path)
            self.assertEqual(
                state.unmatched_message_ids("account", "provider:inbox", "rules-v1"), {"1"}
            )
            for account, namespace, fingerprint in (
                ("other-account", "provider:inbox", "rules-v1"),
                ("account", "provider:archive", "rules-v1"),
                ("account", "provider:inbox", "rules-v2"),
            ):
                self.assertEqual(
                    state.unmatched_message_ids(account, namespace, fingerprint), set()
                )
            self.assertFalse(state.was_processed("account", "provider:inbox", "1"))
            state.record_unmatched("account", "provider:inbox", "1", "rules-v2")
            self.assertEqual(
                state.unmatched_message_ids("account", "provider:inbox", "rules-v1"), set()
            )
            self.assertEqual(
                state.unmatched_message_ids("account", "provider:inbox", "rules-v2"), {"1"}
            )

    def test_successful_archive_removes_previous_unmatched_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = ArchiveState(root / "state.sqlite3")
            state.record_unmatched("account", "provider:inbox", "1", "rules")
            mail = parse_mail(sample_mail())
            rule = Rule("All", "Inbox")
            result = ArchiveStorage(root / "Archive").archive(mail, rule)
            state.record("account", "provider:inbox", "1", mail, rule, result)
            self.assertTrue(state.was_processed("account", "provider:inbox", "1"))
            self.assertEqual(
                state.unmatched_message_ids("account", "provider:inbox", "rules"), set()
            )

    def test_unmatched_history_follows_database_copy_and_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = ArchiveState(root / "source.sqlite3")
            source.record_unmatched("account", "provider:inbox", "source", "rules")
            copied = source.migrated_to(root / "copied.sqlite3")
            self.assertEqual(
                copied.unmatched_message_ids("account", "provider:inbox", "rules"), {"source"}
            )
            destination = ArchiveState(root / "destination.sqlite3")
            destination.record_unmatched("account", "provider:inbox", "destination", "rules")
            merged = source.migrated_to(destination.database_path)
            self.assertEqual(
                merged.unmatched_message_ids("account", "provider:inbox", "rules"),
                {"source", "destination"},
            )

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
            source.complete_initial_scan("account", "provider:inbox", {"skipped-message"})
            destination.record(
                "account",
                "provider:inbox",
                "destination-message",
                mail,
                rule,
                archive_result,
            )

            migrated = source.migrated_to(destination.database_path)

            self.assertTrue(migrated.was_processed("account", "provider:inbox", "source-message"))
            self.assertTrue(
                migrated.was_processed("account", "provider:inbox", "destination-message")
            )
            self.assertTrue(migrated.has_completed_initial_scan("account", "provider:inbox"))
            self.assertIn(
                "skipped-message",
                migrated.processed_message_ids("account", "provider:inbox", include_skipped=True),
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
