"""Filesystem probing for pending archive output recovery."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from mailarchive.infrastructure.output_files import LocalOutputFiles


class LocalOutputFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = LocalOutputFiles()

    def test_matching_pending_file_is_recognized_by_size_and_digest(self) -> None:
        path = self.root / "mail.eml"
        content = b"complete archive output"
        digest = hashlib.sha256(content).hexdigest()
        self.files.publish(path, content)

        self.assertTrue(self.files.occupied(path))
        self.assertTrue(self.files.matches(path, digest, len(content)))
        self.assertFalse(self.files.matches(path, digest, len(content) + 1))
        self.assertFalse(self.files.matches(path, "0" * 64, len(content)))

    def test_dangling_symlink_is_occupied_but_never_treated_as_a_receipt(self) -> None:
        path = self.root / "mail.eml"
        try:
            path.symlink_to(self.root / "missing.eml")
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks are unavailable on this platform")

        self.assertTrue(self.files.occupied(path))
        self.assertFalse(self.files.matches(path, "0" * 64, 0))

    def test_missing_parent_is_available_for_a_recorded_publication_attempt(self) -> None:
        parent = self.root / "offline"
        parent.write_text("not a directory")
        path = parent / "mail.eml"
        self.assertFalse(self.files.occupied(path))
        self.assertFalse(self.files.matches(path, "0" * 64, 0))
        with self.assertRaises(OSError):
            self.files.publish(path, b"content")


if __name__ == "__main__":
    unittest.main()
