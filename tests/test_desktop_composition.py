"""The real desktop composes against a fresh profile and application facade."""

import gc
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from mailarchive.bootstrap import create_application
from mailarchive.domain.configuration import Settings
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.profile_location import ConfigStore
from mailarchive.presentation.desktop import DesktopApp
from mailarchive.presentation.window import create_root


class DesktopCompositionTests(unittest.TestCase):
    def setUp(self):
        try:
            root = create_root()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")

        def close_window():
            for timer in root.tk.call("after", "info"):
                root.after_cancel(timer)
            root.destroy()

        self.addCleanup(gc.collect)
        self.addCleanup(close_window)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config = ConfigStore(Path(temporary.name))
        config.save(Settings(start_at_login=False))
        with patch("mailarchive.bootstrap.set_start_at_login"):
            application = create_application(config, MemoryCredentialStore())
        self.addCleanup(application.close)
        with patch("mailarchive.presentation.desktop.TrayController"):
            desktop = DesktopApp(root, application)
        application.set_observers(desktop.on_service_event, desktop.on_run_progress)
        application.start()
        root.update()
        self.root = root
        self.desktop = desktop
        self.application = application

    def wait_until_idle(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.root.update()
            if not self.desktop._archive_running:
                return
            time.sleep(0.01)
        self.fail(f"The mail check stayed busy: {self.desktop.progress_var.get()}")

    def test_empty_profile_check_finishes_and_can_be_clicked_again(self):
        for _ in range(2):
            self.desktop.check_button.invoke()
            self.wait_until_idle()
            self.assertFalse(self.desktop.check_button.instate(["disabled"]))
            self.assertIsNone(self.desktop._progress_timer)
            self.assertEqual(self.desktop.progress_var.get(), "No enabled mailboxes to check.")
        self.assertEqual(self.application.current_jobs(), ())
        self.assertEqual(self.application.activity_page().items, ())

    def test_failure_before_processing_ends_check_and_allows_next_click(self):
        service = self.application._context.execution.service
        with (
            patch.object(service, "has_automatic_work", side_effect=OSError("Read failed")),
            self.assertLogs("mailarchive.application.execution", level="ERROR"),
        ):
            self.desktop.check_button.invoke()
            self.wait_until_idle()
        self.assertEqual(self.desktop.progress_var.get(), "Mail check failed.")
        self.assertFalse(self.desktop.check_button.instate(["disabled"]))
        self.desktop.check_button.invoke()
        self.wait_until_idle()
        self.assertEqual(self.desktop.progress_var.get(), "No enabled mailboxes to check.")

    def test_fresh_profile_builds_desktop_and_reuses_single_activity_window(self):
        desktop, root, application = self.desktop, self.root, self.application
        desktop.show_archive_activity()
        dialog = desktop.activity_dialog
        root.update()
        desktop.show_archive_activity()
        self.assertIs(desktop.activity_dialog, dialog)
        self.assertEqual(application.current_jobs(), ())
        self.assertEqual(application.activity_page().items, ())
        self.assertEqual(application.status().pending_count, 0)
        dialog.destroy()
