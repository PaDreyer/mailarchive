import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import mailarchive.platform_integration as platform_integration
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

    def test_xorg_backend_rejects_missing_or_failing_manager_lookup(self) -> None:
        class MissingManagerIcon:
            pass

        class FailingManagerIcon:
            def _get_systray_manager(self):
                raise RuntimeError("display disconnected")

        MissingManagerIcon.__module__ = "pystray._xorg"
        FailingManagerIcon.__module__ = "pystray._xorg"

        self.assertFalse(tray_backend_is_available(MissingManagerIcon()))
        self.assertFalse(tray_backend_is_available(FailingManagerIcon()))

    def test_linux_autostart_file_is_created_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "autostart" / "mailarchive.desktop"
            _set_linux_autostart(True, path, ["/opt/Mail Archive/MailArchive", "--minimized"])
            content = path.read_text(encoding="utf-8")
            self.assertIn("Name=MailArchive", content)
            self.assertIn('Exec="/opt/Mail Archive/MailArchive" "--minimized"', content)
            _set_linux_autostart(False, path, [])
            self.assertFalse(path.exists())

    def test_application_command_covers_appimage_frozen_and_module_launches(self) -> None:
        with (
            patch.object(platform_integration.os, "name", "posix"),
            patch.dict(platform_integration.os.environ, {"APPIMAGE": "/opt/MailArchive.AppImage"}, clear=True),
        ):
            self.assertEqual(
                platform_integration.application_command(),
                ["/opt/MailArchive.AppImage", "--minimized"],
            )

        with (
            patch.object(platform_integration.os, "name", "nt"),
            patch.object(platform_integration.sys, "executable", r"C:\Program Files\MailArchive.exe"),
            patch.object(platform_integration.sys, "frozen", True, create=True),
        ):
            self.assertEqual(
                platform_integration.application_command(),
                [r"C:\Program Files\MailArchive.exe", "--minimized"],
            )

        with (
            patch.object(platform_integration.os, "name", "nt"),
            patch.object(platform_integration.sys, "executable", r"C:\Python\python.exe"),
            patch.object(platform_integration.sys, "frozen", False, create=True),
        ):
            self.assertEqual(
                platform_integration.application_command(),
                [r"C:\Python\python.exe", "-m", "mailarchive", "--minimized"],
            )

    def test_linux_start_at_login_delegates_to_autostart_writer(self) -> None:
        path = Path("/tmp/test-mailarchive.desktop")
        command = ["/opt/MailArchive", "--minimized"]
        with (
            patch.object(platform_integration.os, "name", "posix"),
            patch.object(platform_integration, "linux_autostart_path", return_value=path),
            patch.object(platform_integration, "application_command", return_value=command),
            patch.object(platform_integration, "_set_linux_autostart") as set_autostart,
        ):
            platform_integration.set_start_at_login(True)

        set_autostart.assert_called_once_with(True, path, command)

    def test_windows_start_at_login_sets_quoted_registry_command(self) -> None:
        key = object()
        open_key = MagicMock()
        open_key.return_value.__enter__.return_value = key
        registry = SimpleNamespace(
            HKEY_CURRENT_USER=object(),
            KEY_SET_VALUE=2,
            REG_SZ=1,
            OpenKey=open_key,
            SetValueEx=Mock(),
            DeleteValue=Mock(),
        )
        command = [r"C:\Program Files\MailArchive.exe", "--minimized"]

        with (
            patch.object(platform_integration.os, "name", "nt"),
            patch.dict(sys.modules, {"winreg": registry}),
            patch.object(platform_integration, "application_command", return_value=command),
        ):
            platform_integration.set_start_at_login(True)

        open_key.assert_called_once_with(
            registry.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            registry.KEY_SET_VALUE,
        )
        registry.SetValueEx.assert_called_once_with(
            key,
            "MailArchive",
            0,
            registry.REG_SZ,
            '"C:\\Program Files\\MailArchive.exe" --minimized',
        )

    def test_windows_start_at_login_ignores_missing_registry_value_on_disable(self) -> None:
        key = object()
        open_key = MagicMock()
        open_key.return_value.__enter__.return_value = key
        delete_value = Mock(side_effect=FileNotFoundError)
        registry = SimpleNamespace(
            HKEY_CURRENT_USER=object(),
            KEY_SET_VALUE=2,
            REG_SZ=1,
            OpenKey=open_key,
            SetValueEx=Mock(),
            DeleteValue=delete_value,
        )

        with (
            patch.object(platform_integration.os, "name", "nt"),
            patch.dict(sys.modules, {"winreg": registry}),
        ):
            platform_integration.set_start_at_login(False)

        delete_value.assert_called_once_with(key, "MailArchive")

    def test_windows_existing_window_is_restored_and_focused(self) -> None:
        user32 = Mock()
        user32.FindWindowW.return_value = 123
        with (
            patch.object(platform_integration.os, "name", "nt"),
            patch.object(platform_integration.ctypes, "WinDLL", return_value=user32, create=True),
        ):
            platform_integration.activate_existing_window()

        user32.FindWindowW.assert_called_once_with(None, "MailArchive")
        user32.ShowWindow.assert_called_once_with(123, 9)
        user32.SetForegroundWindow.assert_called_once_with(123)

    def test_windows_single_instance_uses_and_closes_named_mutex(self) -> None:
        kernel32 = Mock()
        kernel32.CreateMutexW.return_value = 456
        with (
            patch.object(platform_integration.os, "name", "nt"),
            patch.object(platform_integration.ctypes, "WinDLL", return_value=kernel32, create=True),
            patch.object(platform_integration.ctypes, "get_last_error", return_value=183, create=True),
        ):
            instance = SingleInstance("MailArchive-Test")
            self.assertTrue(instance.already_running)
            self.assertEqual(instance.handle, 456)
            instance.close()

        kernel32.CreateMutexW.assert_called_once_with(None, False, "Local\\MailArchive-Test")
        kernel32.CloseHandle.assert_called_once_with(456)
        self.assertIsNone(instance.handle)

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
