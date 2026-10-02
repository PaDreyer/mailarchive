"""The real desktop composes against a fresh profile and application facade."""

import gc
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.events import ExecutionState, RunProgress
from mailarchive.bootstrap import create_application
from mailarchive.domain.configuration import Account, Mailbox, Rule, RuleTarget, Settings
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.profile_location import ConfigStore
from mailarchive.presentation.desktop import DesktopApp
from mailarchive.presentation.window import create_root
from tests.test_check_cancellation import ControlledSource
from tests.test_restart_core import Registry


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

    def configure_stoppable_check(self, phase="download"):
        source = ControlledSource()
        source.phase = phase
        self.addCleanup(source.release.set)
        mailbox = Mailbox("owner@example.org", ["INBOX"], archive_existing_messages=True)
        account = Account("Mail", "imap.example.org", mailbox.address, mailboxes=[mailbox])
        self.application.save_account(AccountSubmission(account, {"password": "test"}, True))
        self.application.save_rules(
            [
                Rule(
                    "Archive",
                    targets=[RuleTarget(str(self.application.database_path.parent / "archive"))],
                )
            ]
        )
        self.application._context.execution.service.source_registry = Registry(source)
        return source

    def test_real_button_stops_download_remains_responsive_and_starts_again(self):
        source = self.configure_stoppable_check()
        button = self.desktop.check_button
        button.invoke()
        first_id = self.desktop._check_id
        self.assertEqual(button.cget("text"), "Stop check")
        self.assertTrue(source.entered.wait(2))
        button.invoke()
        self.assertEqual(button.cget("text"), "Stopping")
        self.assertTrue(button.instate(["disabled"]))
        responsive = []
        self.root.after(0, lambda: responsive.append(True))
        self.root.update()
        self.assertEqual(responsive, [True])
        self.assertTrue(self.desktop._archive_running)
        # Unrelated and stale events cannot finish this check or undo Stop.
        self.desktop._display_progress(
            RunProgress(
                "Old check finished.",
                active=False,
                execution_id="old-check",
                origin="check",
                state=ExecutionState.COMPLETED,
                sequence=900,
            )
        )
        self.desktop._display_progress(
            RunProgress(
                "Other work finished.",
                active=False,
                execution_id="other-work",
                origin="automatic",
                state=ExecutionState.COMPLETED,
                sequence=901,
            )
        )
        self.assertEqual(button.cget("text"), "Stopping")
        source.release.set()
        self.wait_until_idle()
        self.assertEqual(self.desktop.progress_var.get(), "Mail check stopped.")
        self.assertIsNone(self.desktop._progress_timer)
        self.assertEqual(button.cget("text"), "Check mail now")
        self.assertFalse(button.instate(["disabled"]))
        self.desktop._display_progress(
            RunProgress(
                "Stale progress.",
                execution_id=first_id,
                origin="check",
                state=ExecutionState.RUNNING,
                sequence=999,
            )
        )
        self.assertFalse(self.desktop._archive_running)
        source.phase = "none"
        button.invoke()
        self.assertFalse(self.application.stop_check(first_id))
        self.wait_until_idle()
        self.assertEqual(self.desktop.progress_var.get(), "Mail check finished.")
        self.assertEqual(len(self.application.activity_page().items), 1)

    def test_real_button_stops_queued_check_without_starting_provider(self):
        source = self.configure_stoppable_check()
        coordinator = self.application._context.execution
        with coordinator._condition:
            self.desktop.check_button.invoke()
            self.assertEqual(self.desktop.check_button.cget("text"), "Stop check")
            self.desktop.check_button.invoke()
        self.wait_until_idle()
        self.assertEqual(self.desktop.progress_var.get(), "Mail check stopped.")
        self.assertEqual(source.folders_seen, [])
        self.assertFalse(self.desktop.check_button.instate(["disabled"]))
