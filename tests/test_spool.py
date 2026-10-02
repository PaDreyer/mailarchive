"""Local raw-mail retention and filesystem safety boundaries."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mailarchive.infrastructure.spool import LocalSpool, MessageTooLargeError, SpoolError


class LocalSpoolTests(unittest.TestCase):
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
            temporary_file = spool.path / "intake-interrupted.tmp"
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
