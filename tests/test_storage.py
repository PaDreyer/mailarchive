"""Full-path resolution and exclusive atomic archive publication."""

import errno
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mailarchive.storage import _atomic_write, destination_path, safe_filename


class StorageTests(unittest.TestCase):
    def test_full_path_template_uses_provider_time_and_literal_braces(self):
        received = datetime(2026, 1, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            path = f"{temporary}/{{year}}/{{month}}/{{{{literal}}}}"
            self.assertEqual(
                destination_path(Path(), path, mail_date=received),
                Path(temporary) / "2026" / "01" / "{literal}",
            )
            self.assertEqual(
                destination_path(Path(), path),
                Path(temporary) / "YYYY" / "MM" / "{literal}",
            )

    def test_relative_or_invalid_template_is_rejected(self):
        for candidate in (
            "relative",
            "../outside",
            "",
            " /tmp/out ",
            "/tmp/{unknown}",
            "/tmp/{year:02}",
        ):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                destination_path(Path(), candidate)

    def test_user_path_components_are_not_silently_renamed(self):
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary) / "A  B" / "report:final"
            self.assertEqual(destination_path(Path(), str(selected)), selected)

    def test_atomic_write_creates_parents_and_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "new" / "nested" / "mail.eml"
            _atomic_write(target, b"original")
            with self.assertRaises(FileExistsError):
                _atomic_write(target, b"replacement")
            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_publication_failure_leaves_no_partial_final_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "mail.eml"
            with patch("mailarchive.storage._publish_file", side_effect=OSError("unavailable")):
                with self.assertRaises(OSError):
                    _atomic_write(target, b"complete")
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_process_exit_before_or_after_publish_never_exposes_a_partial_file(self):
        script = textwrap.dedent("""
            import os, sys
            from pathlib import Path
            import mailarchive.storage as storage
            target, phase = sys.argv[1:]
            publish = storage._publish_file
            def interrupt(source, destination):
                if phase == "after":
                    publish(source, destination)
                os._exit(73)
            storage._publish_file = interrupt
            storage._atomic_write(Path(target), b"complete")
        """)
        for phase in ("before", "after"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary) / "mail.eml"
                child = subprocess.run(
                    [sys.executable, "-c", script, str(target), phase],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(child.returncode, 73, child.stderr)
                if phase == "before":
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(target.read_bytes(), b"complete")
                    with self.assertRaises(FileExistsError):
                        _atomic_write(target, b"different")

    @unittest.skipUnless(sys.platform == "linux", "Linux publication fallback")
    def test_link_fallback_is_exclusive(self):
        for error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            with (
                self.subTest(error=error),
                tempfile.TemporaryDirectory() as temporary,
                patch("mailarchive.storage.ctypes.CDLL") as libc,
                patch("mailarchive.storage.ctypes.get_errno", return_value=error),
            ):
                libc.return_value.renameat2.return_value = -1
                target = Path(temporary) / "mail.eml"
                _atomic_write(target, b"original")
                with self.assertRaises(FileExistsError):
                    _atomic_write(target, b"replacement")
                self.assertEqual(target.read_bytes(), b"original")

    @unittest.skipUnless(sys.platform == "linux", "Linux publication fallback")
    def test_unsupported_publication_reports_error_without_final_file(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("mailarchive.storage.ctypes.CDLL") as libc,
            patch("mailarchive.storage.ctypes.get_errno", return_value=errno.EOPNOTSUPP),
            patch(
                "mailarchive.storage.os.link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")
            ),
        ):
            libc.return_value.renameat2.return_value = -1
            target = Path(temporary) / "mail.eml"
            with self.assertRaises(OSError):
                _atomic_write(target, b"complete")
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_safe_attachment_names(self):
        self.assertEqual(safe_filename("CON.txt"), "_CON.txt")
        self.assertEqual(safe_filename("  ", "Fallback"), "Fallback")
        self.assertEqual(safe_filename("report:  2026?.pdf"), "report_ 2026_.pdf")
        self.assertEqual(len(safe_filename("x" * 200, max_length=12)), 12)


if __name__ == "__main__":
    unittest.main()
