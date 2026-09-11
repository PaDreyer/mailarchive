import os
import tempfile
import unittest
from pathlib import Path

from mailarchive.platform_integration import (
    SingleInstance,
    _set_linux_autostart,
    tray_backend_is_available,
)


class PlatformIntegrationTests(unittest.TestCase):
    def test_xorg_backend_requires_a_system_tray_manager(self) -> None:
        class XorgIcon:
            def __init__(self, manager) -> None:
                self.manager = manager

            def _get_systray_manager(self):
                return self.manager

        XorgIcon.__module__ = "pystray._xorg"

        self.assertFalse(tray_backend_is_available(XorgIcon(None)))
        self.assertTrue(tray_backend_is_available(XorgIcon(object())))

    def test_non_xorg_backend_does_not_require_xorg_selection(self) -> None:
        class AppIndicatorIcon:
            pass

        AppIndicatorIcon.__module__ = "pystray._appindicator"
        self.assertTrue(tray_backend_is_available(AppIndicatorIcon()))

    def test_linux_autostart_file_is_created_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "autostart" / "mailarchive.desktop"
            _set_linux_autostart(True, path, ["/opt/Mail Archive/MailArchive", "--minimized"])
            content = path.read_text(encoding="utf-8")
            self.assertIn("Name=MailArchive", content)
            self.assertIn('Exec="/opt/Mail Archive/MailArchive" "--minimized"', content)
            _set_linux_autostart(False, path, [])
            self.assertFalse(path.exists())

    @unittest.skipIf(os.name == "nt", "Linux file lock")
    def test_linux_single_instance_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            previous = os.environ.get("XDG_RUNTIME_DIR")
            os.environ["XDG_RUNTIME_DIR"] = temporary
            first = SingleInstance("MailArchive-Test")
            second = SingleInstance("MailArchive-Test")
            try:
                self.assertFalse(first.already_running)
                self.assertTrue(second.already_running)
            finally:
                second.close()
                first.close()
                if previous is None:
                    os.environ.pop("XDG_RUNTIME_DIR", None)
                else:
                    os.environ["XDG_RUNTIME_DIR"] = previous


if __name__ == "__main__":
    unittest.main()
