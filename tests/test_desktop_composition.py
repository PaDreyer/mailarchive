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
from mailarchive.application.execution import NO_RULES_NOTICE
from mailarchive.application.polling import AutomaticMonitoringState
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

    def wait_until_idle(self, message=None):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.root.update()
            if not self.desktop._archive_running and (
                message is None or self.desktop.progress_var.get() == message
            ):
                return
            time.sleep(0.01)
        self.fail(f"The mail check stayed busy: {self.desktop.progress_var.get()}")

    def test_empty_profile_check_finishes_and_can_be_clicked_again(self):
        for _ in range(2):
            self.desktop.check_button.invoke()
            self.wait_until_idle("No enabled mailboxes to check.")
            self.assertFalse(self.desktop.check_button.instate(["disabled"]))
            self.assertIsNone(self.desktop._progress_timer)
            self.assertEqual(self.desktop.progress_var.get(), "No enabled mailboxes to check.")
        self.assertEqual(self.application.current_jobs(), ())
        self.assertEqual(self.application.activity_page().items, ())

    def test_ruleless_check_shows_notice_without_busy_controls_or_provider_calls(self):
        source = self.configure_stoppable_check()
        self.desktop.settings = self.application.save_rules([])
        self.desktop.refresh_all()
        account = self.application.settings.accounts[0]
        self.assertEqual(
            self.desktop._account_monitoring_status(account), "Waiting for an active rule"
        )
        row = self.desktop.account_tree.item(account.id, "values")
        self.assertIn("Waiting for an active rule", row)
        for _ in range(2):
            self.desktop.check_button.invoke()
            self.wait_until_idle(NO_RULES_NOTICE)
            self.assertEqual(self.desktop.progress_var.get(), NO_RULES_NOTICE)
            self.assertEqual(self.desktop.check_button.cget("text"), "Check mail now")
            self.assertFalse(self.desktop.check_button.instate(["disabled"]))
            self.assertIsNone(self.desktop._check_id)
            self.assertIsNone(self.desktop._progress_timer)
            self.assertFalse(self.desktop._archive_running)
            self.assertFalse(self.desktop.progress_bar.winfo_ismapped())
            self.assertEqual(self.desktop.elapsed_var.get(), "")
        self.assertEqual(source.folders_seen, [])
        self.assertEqual(source.downloads, 0)
        self.assertEqual(self.application.current_jobs(), ())

    def test_account_status_uses_account_scope_and_enabled_rules(self):
        self.configure_stoppable_check()
        account = self.application.settings.accounts[0]
        rule = self.application.settings.rules[0]
        for enabled, scope in ((False, None), (True, []), (True, ["other"])):
            with self.subTest(enabled=enabled, scope=scope):
                rule.enabled, rule.account_ids = enabled, scope
                self.desktop.settings = self.application.save_rules([rule])
                self.assertEqual(
                    self.desktop._account_monitoring_status(account), "Waiting for an active rule"
                )
        rule.enabled, rule.account_ids = True, [account.id]
        self.desktop.settings = self.application.save_rules([rule])
        self.assertEqual(self.desktop._account_monitoring_status(account), "Setting up")

    def test_failure_before_processing_ends_check_and_allows_next_click(self):
        self.configure_stoppable_check()
        service = self.application._context.execution.service
        with (
            patch.object(service, "run_once", side_effect=OSError("Read failed")),
            self.assertLogs("mailarchive.application.execution", level="ERROR"),
        ):
            self.desktop.check_button.invoke()
            self.wait_until_idle()
        self.assertEqual(self.desktop.progress_var.get(), "Mail check failed.")
        self.assertFalse(self.desktop.check_button.instate(["disabled"]))
        self.application.delete_account(self.application.settings.accounts[0].id)
        self.desktop.check_button.invoke()
        self.wait_until_idle("No enabled mailboxes to check.")
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

    def test_global_pause_button_keeps_manual_checks_available_and_status_visible(self):
        source = self.configure_stoppable_check("none")
        desktop = self.desktop
        desktop.automatic_button.invoke()
        self.assertTrue(self.application.settings.automatic_monitoring_paused)
        self.assertEqual(desktop.automatic_button.cget("text"), "Resume automatic checks")
        self.assertEqual(desktop.automatic_status_var.get(), "Automatic checks paused")
        desktop.tray.set_monitoring_paused.assert_called_with(True)
        desktop.check_button.invoke()
        self.wait_until_idle()
        self.assertGreater(source.downloads, 0)
        self.assertEqual(desktop.automatic_status_var.get(), "Automatic checks paused")
        desktop._display_progress(
            RunProgress(
                "Delayed automatic completion.",
                active=False,
                origin="automatic",
                execution_id="old-run",
                state=ExecutionState.COMPLETED,
                sequence=100,
            )
        )
        self.assertEqual(desktop.automatic_status_var.get(), "Automatic checks paused")
        desktop.automatic_button.invoke()
        self.assertFalse(self.application.settings.automatic_monitoring_paused)
        self.assertEqual(desktop.automatic_button.cget("text"), "Pause automatic checks")
        self.assertEqual(desktop.automatic_status_var.get(), "Automatic checks active")
        desktop.tray.set_monitoring_paused.assert_called_with(False)

    def test_failed_pause_leaves_controls_and_application_active(self):
        with (
            patch.object(self.application._context, "save", side_effect=OSError("disk full")),
            patch("mailarchive.presentation.desktop.messagebox.showerror") as error,
        ):
            self.desktop.automatic_button.invoke()
        self.assertFalse(self.application.settings.automatic_monitoring_paused)
        self.assertEqual(
            self.application.automatic_monitoring_state(), AutomaticMonitoringState.ACTIVE
        )
        self.assertEqual(self.desktop.automatic_button.cget("text"), "Pause automatic checks")
        error.assert_called_once()
