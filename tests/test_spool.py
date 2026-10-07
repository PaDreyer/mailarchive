"""Local raw-mail retention and filesystem safety boundaries."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mailarchive.infrastructure.spool import LocalSpool, MessageTooLargeError, SpoolError


class LocalSpoolTests(unittest.TestCase):
    def test_relative_raw_references_are_read_retained_and_discarded_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            spool = LocalSpool(Path(temporary) / "work")
            retained, _ = spool.stage([b"accepted"])
            orphan, _ = spool.stage([b"orphan"])
            relative = Path(os.path.relpath(retained))
            self.assertEqual(spool.read(relative), b"accepted")
            spool.cleanup_unreferenced({str(relative)})
            self.assertTrue(retained.exists())
            self.assertFalse(orphan.exists())
            spool.discard(relative)
            self.assertFalse(retained.exists())

    def test_parent_alias_is_supported_without_following_file_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spool = LocalSpool(root / "work")
            raw, _ = spool.stage([b"accepted"])
            alias = root / "work-alias"
            file_link = spool.path / "file-link.eml"
            try:
                alias.symlink_to(spool.path, target_is_directory=True)
                file_link.symlink_to(raw)
            except (OSError, NotImplementedError):
                self.skipTest("Symlinks are unavailable")
            self.assertEqual(spool.read(alias / raw.name), b"accepted")
            with self.assertRaises(SpoolError):
                spool.read(file_link)
            self.assertEqual(raw.read_bytes(), b"accepted")

    def test_foreign_retained_reference_blocks_cleanup_of_unverified_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spool = LocalSpool(root / "work")
            raw, _ = spool.stage([b"accepted"])
            with self.assertRaisesRegex(SpoolError, "outside the work directory"):
                spool.cleanup_unreferenced({str(root / "foreign" / raw.name)})
            self.assertEqual(raw.read_bytes(), b"accepted")

    def test_stage_read_and_discard_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            spool = LocalSpool(Path(temporary) / "work")
            raw = b"Subject: accepted\r\n\r\nBody"

            path, digest = spool.stage([raw[:10], raw[10:]])

            self.assertEqual(path.parent, spool.path)
            self.assertEqual(path.suffix, ".eml")
            self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
            self.assertEqual(spool.read(path), raw)
            self.assertEqual(spool.usage_bytes(), len(raw))
            spool.discard(path)
            self.assertFalse(path.exists())
            self.assertEqual(spool.usage_bytes(), 0)

    def test_failed_intake_leaves_no_visible_or_temporary_work_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            spool = LocalSpool(Path(temporary) / "work")
            with patch("mailarchive.infrastructure.spool.MAX_MESSAGE_BYTES", 4):
                with self.assertRaises(MessageTooLargeError):
                    spool.stage([b"123", b"45"])
            self.assertEqual(list(spool.path.iterdir()), [])

    def test_recovery_preserves_referenced_raw_mail_and_removes_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            spool = LocalSpool(Path(temporary) / "work")
            retained, _ = spool.stage([b"retained"])
            orphan, _ = spool.stage([b"orphan"])
            descriptor, temporary_name = tempfile.mkstemp(
                prefix="intake-", suffix=".tmp", dir=spool.path
            )
            os.close(descriptor)
            temporary_file = Path(temporary_name)
            temporary_file.write_bytes(b"partial")

            spool.cleanup_unreferenced({str(retained)})

            self.assertEqual(spool.read(retained), b"retained")
            self.assertFalse(orphan.exists())
            self.assertFalse(temporary_file.exists())

    def test_read_rejects_outside_path_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spool = LocalSpool(root / "work")
            outside = root / "outside.eml"
            outside.write_bytes(b"private")
            with self.assertRaisesRegex(SpoolError, "outside the work directory"):
                spool.read(outside)
            link = spool.path / "link.eml"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable")
            with self.assertRaises(SpoolError):
                spool.read(link)
            self.assertEqual(outside.read_bytes(), b"private")

    def test_directory_replacement_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spool = LocalSpool(root / "work")
            spool.path.rename(root / "old-work")
            try:
                spool.path.symlink_to(root / "old-work", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable")
            with self.assertRaises(SpoolError):
                spool.stage([b"message"])
            self.assertEqual(list((root / "old-work").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
