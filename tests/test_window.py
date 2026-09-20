from __future__ import annotations

import gc
import re
import shutil
import subprocess
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mailarchive import APP_NAME, APP_WINDOW_CLASS
from mailarchive.desktop import DesktopApp
from mailarchive.desktop_entry import autostart_entry
from mailarchive.linux_integration import AppImageIntegration
from mailarchive.models import Settings
from mailarchive.window import create_root


class WindowIdentityTests(unittest.TestCase):
    def test_window_matches_all_launchers_and_loads_bundled_icon(self) -> None:
        with (
            patch("mailarchive.window.tk.Tk") as tk_root,
            patch("mailarchive.window.ImageTk.PhotoImage") as photo,
        ):
            # Decode the real asset, including its pixels, without needing a display.
            photo.side_effect = lambda image, **kwargs: image.copy()
            root = create_root()

        tk_root.assert_called_once_with(className=APP_WINDOW_CLASS)
        root.iconname.assert_called_once_with(APP_NAME)
        default, image = root.iconphoto.call_args.args
        self.assertTrue(default)
        self.assertGreaterEqual(min(image.size), 64)
        self.assertIsNotNone(image.getbbox())
        launchers = (
            AppImageIntegration(Path("/tmp/MailArchive.AppImage"), None)._launcher().decode(),
            autostart_entry(["/tmp/MailArchive.AppImage", "--minimized"]),
            (Path(__file__).resolve().parents[1] / "packaging/linux/mailarchive.desktop").read_text(
                encoding="utf-8"
            ),
        )
        for launcher in launchers:
            self.assertIn(f"StartupWMClass={APP_WINDOW_CLASS}", launcher.splitlines())


class NativeWindowIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = create_root()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(gc.collect)
        self.addCleanup(self.root.destroy)
        self.root.title(f"MailArchive window identity test {self.root.winfo_id()}")
        self.desktop = object.__new__(DesktopApp)
        self.desktop.root = self.root
        self.desktop.settings = Settings(archive_root="/unused", minimize_to_tray=True)
        self.desktop.tray = SimpleNamespace(safe_to_hide=True)
        self.desktop.desktop_integration = None
        self.desktop._save_focused_setting = Mock()

    def test_identity_survives_starting_hidden_and_repeated_tray_restore(self) -> None:
        self.root.withdraw()
        for _ in range(3):
            self.desktop.show()
            self.root.update()
            self.assertEqual(self.root.state(), "normal")
            self.assertEqual(self.root.winfo_class(), APP_WINDOW_CLASS)
            self.assertEqual(self.root.iconname(), APP_NAME)
            self.desktop.hide_to_tray()
            self.root.update()
            self.assertEqual(self.root.state(), "withdrawn")

    @unittest.skipUnless(
        shutil.which("xprop") and shutil.which("xwininfo"), "X11 property tools unavailable"
    )
    def test_x11_dock_properties_survive_tray_restore(self) -> None:
        if self.root.tk.call("tk", "windowingsystem") != "x11":
            self.skipTest("X11 window properties")

        def properties(window: tk.Toplevel | tk.Tk) -> str:
            # Tk's client wrapper owns the hints; a name lookup can instead
            # find the window manager's decoration with the same title.
            tree = subprocess.check_output(
                ["xwininfo", "-id", str(window.winfo_id()), "-tree"], text=True, timeout=5
            )
            parent = re.search(r"Parent window id: (0x[0-9a-fA-F]+)", tree)
            self.assertIsNotNone(parent, tree)
            return subprocess.check_output(
                [
                    "xprop",
                    "-id",
                    parent.group(1),
                    "-f",
                    "_NET_WM_ICON",
                    "32c",
                    " = $0, $1\\n",
                    "WM_CLASS",
                    "WM_ICON_NAME",
                    "_NET_WM_ICON",
                ],
                text=True,
                timeout=5,
            )

        self.root.update()
        before = properties(self.root)
        self.assertIn(f'"{APP_WINDOW_CLASS}"', before)
        self.assertIn('WM_ICON_NAME(STRING) = "MailArchive"', before)
        self.assertRegex(before, r"_NET_WM_ICON\(CARDINAL\) = \d+, \d+")
        self.assertNotIn("not found", before)
        for _ in range(3):
            self.desktop.hide_to_tray()
            self.root.update()
            self.desktop.show()
            self.root.update()
            self.assertEqual(properties(self.root), before)

        dialog = tk.Toplevel(self.root)
        self.addCleanup(dialog.destroy)
        dialog.title(self.root.title() + " dialog")
        self.root.update()
        self.assertRegex(properties(dialog), r"_NET_WM_ICON\(CARDINAL\) = \d+, \d+")
