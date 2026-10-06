"""The real desktop composes against a fresh profile and application facade."""

import tempfile
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.account_credentials import update_credential_data
from mailarchive.application.account_status import (
    AccountState,
    AuthorizationState,
    AuthorizationStatus,
)
from mailarchive.application.events import ExecutionState, RunProgress
from mailarchive.application.execution import NO_RULES_NOTICE
from mailarchive.application.polling import AutomaticMonitoringState
from mailarchive.bootstrap import create_application
from mailarchive.domain.configuration import (
    Account,
    Mailbox,
    Rule,
    RuleTarget,
    Settings,
)
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.oauth import MICROSOFT_MAIL_READ_SCOPE
from mailarchive.infrastructure.profile_location import ConfigStore
from mailarchive.presentation.desktop import DesktopApp
from mailarchive.presentation.dialogs import AccountDialog, RangeDialog
from mailarchive.presentation.window import create_root
from tests.concurrency import THREAD_TIMEOUT
from tests.oauth_fixture import microsoft_cache
from tests.test_check_cancellation import ControlledSource
from tests.test_restart_core import Registry
from tests.tk_test_case import TkTestCase


class DesktopCompositionTests(TkTestCase):
    def open_oauth_editor(self, *, authorize=False, editing=False):
        before = self.application.settings
        errors, dialogs = [], []

        def factory(*args, **kwargs):
            dialog = AccountDialog(*args, **kwargs)
            dialogs.append(dialog)

            def guarded(action):
                def callback():
                    try:
                        action()
                    except Exception as exc:
                        errors.append(exc)
                        dialog.destroy()

                return callback

            def finish_authorization():
                if dialog.authorization_label.cget("text") != "Authorized":
                    self.root.after(10, guarded(finish_authorization))
                    return
                self.assertTrue(dialog.winfo_exists())
                self.assertIsNone(dialog.result)
                self.assertEqual(self.application.settings, before)
                self.assertIn("Not saved yet", dialog.authorization_detail.cget("text"))
                dialog.save_button.invoke()

            def configure():
                if editing:
                    self.assertEqual(dialog.authorization_label.cget("text"), "Authorized")
                    self.assertEqual(dialog.authorize_button.cget("text"), "Reauthorize")
                    dialog.variables["label"].set("Renamed mailbox")
                    dialog.authorize_button.invoke()
                    self.assertEqual(self.application.settings, before)
                    self.root.after(10, guarded(finish_authorization))
                    return
                dialog.variables["label"].set("Microsoft mailbox")
                dialog.variables["provider"].set("Outlook / Microsoft 365 (Microsoft Graph)")
                dialog._provider_changed()
                dialog.variables["username"].set("owner@example.org")
                dialog.variables["client_id"].set("client")
                dialog.mailboxes = [Mailbox("owner@example.org", ["INBOX"])]
                dialog._refresh_mailboxes()
                dialog.update_idletasks()
                self.assertEqual(dialog.authorization_label.cget("text"), "Authorization required")
                self.assertLessEqual(
                    dialog.authorization_frame.winfo_y()
                    + dialog.authorization_frame.winfo_height(),
                    dialog.buttons.winfo_y(),
                )
                if authorize:
                    dialog.authorize_button.invoke()
                    self.assertTrue(dialog.winfo_exists())
                    self.assertEqual(self.application.settings, before)
                    self.root.after(10, guarded(finish_authorization))
                else:
                    dialog.save_button.invoke()

            self.root.after_idle(guarded(configure))
            return dialog

        with (
            patch("mailarchive.presentation.desktop.AccountDialog", side_effect=factory),
            self.tk_timeout(lambda: dialogs[-1].destroy() if dialogs else self.root.quit()),
        ):
            if editing:
                self.desktop.edit_account()
            else:
                self.desktop.add_account()
        if errors:
            raise errors[0]
        return self.application.settings.accounts[-1]

    def test_save_in_real_editor_selects_pending_account_without_authorizing(self):
        with patch.object(self.application, "_authorize") as authorize:
            account = self.open_oauth_editor()
            authorize.assert_not_called()
        self.assertEqual(self.desktop.account_tree.selection(), (account.id,))
        self.assertEqual(
            self.desktop.account_tree.item(account.id, "values")[-1], "Authorization required"
        )
        self.assertIn("Edit and choose Authorize", self.desktop.account_notice_var.get())
        self.assertTrue(self.application._background.wait(THREAD_TIMEOUT))
        for state in AuthorizationState:
            with self.subTest(authorization=state):
                self.application.account_statuses.set_authorization(
                    account, AuthorizationStatus(state)
                )
                self.desktop._refresh_account_rows()
                buttons = {
                    widget.cget("text")
                    for frame in self.desktop.accounts_tab.winfo_children()
                    for widget in frame.winfo_children()
                    if widget.winfo_class() == "TButton"
                }
                self.assertEqual(buttons, {"Add", "Edit", "Remove", "Reset paused folder"})

    def test_authorize_keeps_real_editor_open_without_saving_until_save(self):
        def grant(account, credentials, *, cancelled):
            update_credential_data(
                credentials,
                account.id,
                msal_cache=microsoft_cache(account, [MICROSOFT_MAIL_READ_SCOPE]),
            )

        with patch.object(self.application, "_authorize", side_effect=grant) as authorize:
            account = self.open_oauth_editor(authorize=True)
            self.wait_for_ui(
                lambda: (
                    self.application.account_status(account.id).state
                    == AccountState.WAITING_FOR_RULE
                ),
                "Authorization did not complete",
            )
            self.assertEqual(authorize.call_count, 1)
            self.assertEqual(self.desktop.account_tree.selection(), (account.id,))

            self.open_oauth_editor(editing=True)
            self.assertEqual(authorize.call_count, 2)
            self.assertEqual(len(self.application.settings.accounts), 1)
            self.assertEqual(self.application.settings.accounts[0].label, "Renamed mailbox")

    def test_open_editor_observes_a_background_credential_status_change(self):
        account = self.open_oauth_editor()
        self.assertTrue(self.application._background.wait(THREAD_TIMEOUT))
        statuses = self.application.account_statuses
        statuses.set_authorization(account, AuthorizationStatus(AuthorizationState.CHECKING))
        dialog = AccountDialog(
            self.root, 5, account, editor=self.application.account_editor(account.id)
        )
        self.addCleanup(dialog.destroy)
        self.assertEqual(dialog.authorization_label.cget("text"), "Checking authorization…")
        statuses.set_authorization(account, AuthorizationStatus(AuthorizationState.AUTHORIZED))
        self.wait_for_ui(
            lambda: dialog.authorization_label.cget("text") == "Authorized",
            "The editor did not observe the completed credential check",
        )
        self.assertEqual(dialog.authorize_button.cget("text"), "Reauthorize")

    def test_status_change_during_row_render_is_observed_by_the_next_ui_poll(self):
        self.application.set_automatic_monitoring_paused(True)
        account = self.open_oauth_editor()
        self.assertTrue(self.application._background.wait(THREAD_TIMEOUT))
        self.application.dispatch_callbacks()
        statuses = self.application.account_statuses
        statuses.set_authorization(account, AuthorizationStatus(AuthorizationState.CHECKING))
        original = self.application.account_status

        def complete_after_row_status_read(account_id):
            rendered = original(account_id)
            statuses.set_authorization(account, AuthorizationStatus(AuthorizationState.AUTHORIZED))
            return rendered

        with patch.object(
            self.application, "account_status", side_effect=complete_after_row_status_read
        ):
            self.desktop._refresh_account_rows()
        self.assertEqual(
            self.desktop.account_tree.item(account.id, "values")[-1], "Checking authorization…"
        )
        self.desktop._drain_ui_queue()
        self.assertEqual(
            self.desktop.account_tree.item(account.id, "values")[-1], "Waiting for an active rule"
        )
        self.assertEqual(self.desktop._account_status_revision[-1], statuses.revision)

    def test_authorization_cancel_and_timeout_stay_in_the_open_editor(self):
        dialog = AccountDialog(self.root, 5, editor=self.application.account_editor())
        self.addCleanup(dialog.destroy)
        dialog.variables["label"].set("Pending mailbox")
        dialog.variables["provider"].set("Outlook / Microsoft 365 (Microsoft Graph)")
        dialog._provider_changed()
        dialog.variables["username"].set("owner@example.org")
        dialog.variables["client_id"].set("client")
        dialog.mailboxes = [Mailbox("owner@example.org", ["INBOX"])]
        dialog._refresh_mailboxes()
        entered = threading.Event()

        def wait_for_cancel(account, credentials, *, cancelled):
            entered.set()
            self.assertTrue(cancelled.wait(THREAD_TIMEOUT))

        with patch.object(self.application, "_authorize", side_effect=wait_for_cancel):
            dialog.authorize_button.invoke()
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            self.assertTrue(dialog.save_button.instate(["disabled"]))
            self.assertFalse(dialog.cancel_authorization_button.instate(["disabled"]))
            dialog.cancel_authorization_button.invoke()
            self.wait_for_ui(
                lambda: "Authorization cancelled" in dialog.authorization_detail.cget("text"),
                "Cancellation did not appear in the open editor",
            )
        with patch.object(
            self.application, "_authorize", side_effect=TimeoutError("Browser sign-in timed out")
        ):
            dialog.authorize_button.invoke()
            self.wait_for_ui(
                lambda: "Browser sign-in timed out" in dialog.authorization_detail.cget("text"),
                "The authorization error did not appear in the open editor",
            )
        self.assertTrue(dialog.winfo_exists())
        self.assertIsNone(dialog.result)
        self.assertFalse(dialog.save_button.instate(["disabled"]))
        self.assertEqual(self.application.settings.accounts, [])

    def test_window_close_cancels_authorization_and_finishes_worker(self):
        dialog = AccountDialog(self.root, 5, editor=self.application.account_editor())
        self.addCleanup(lambda: dialog.destroy() if dialog.winfo_exists() else None)
        dialog.variables["label"].set("Pending mailbox")
        dialog.variables["provider"].set("Outlook / Microsoft 365 (Microsoft Graph)")
        dialog._provider_changed()
        dialog.variables["username"].set("owner@example.org")
        dialog.variables["client_id"].set("client")
        dialog.mailboxes = [Mailbox("owner@example.org", ["INBOX"])]
        dialog._refresh_mailboxes()
        entered, stopped = threading.Event(), threading.Event()

        def wait_for_cancel(account, credentials, *, cancelled):
            entered.set()
            self.assertTrue(cancelled.wait(THREAD_TIMEOUT))
            stopped.set()

        with patch.object(self.application, "_authorize", side_effect=wait_for_cancel):
            dialog.authorize_button.invoke()
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            self.root.tk.call(dialog.protocol("WM_DELETE_WINDOW"))
            self.assertTrue(self.application._background.wait(THREAD_TIMEOUT))
        self.assertTrue(stopped.is_set())
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(self.application.settings.accounts, [])
        self.assertIsNone(dialog.result)

    def test_authorization_completion_during_render_keeps_status_and_actions_consistent(self):
        dialog = AccountDialog(self.root, 5, editor=self.application.account_editor())
        self.addCleanup(dialog.destroy)
        dialog.variables["label"].set("Pending mailbox")
        dialog.variables["provider"].set("Outlook / Microsoft 365 (Microsoft Graph)")
        dialog._provider_changed()
        dialog.variables["username"].set("owner@example.org")
        dialog.variables["client_id"].set("client")
        dialog.mailboxes = [Mailbox("owner@example.org", ["INBOX"])]
        dialog._refresh_mailboxes()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def timeout(account, credentials, *, cancelled):
            entered.set()
            self.assertTrue(release.wait(THREAD_TIMEOUT))
            raise TimeoutError("Browser sign-in timed out")

        with patch.object(self.application, "_authorize", side_effect=timeout):
            dialog.authorize_button.invoke()
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            resolve = dialog.editor._resolve

            def complete_between_snapshot_and_render(submission, authorization):
                release.set()
                self.assertTrue(self.application._background.wait(THREAD_TIMEOUT))
                return resolve(submission, authorization)

            with patch.object(
                dialog.editor, "_resolve", side_effect=complete_between_snapshot_and_render
            ):
                dialog._update_authorization()
            self.assertEqual(dialog.authorization_label.cget("text"), "Authorizing…")
            self.assertTrue(dialog.save_button.instate(["disabled"]))
            self.assertNotIn("timed out", dialog.authorization_detail.cget("text"))
            dialog._update_authorization()
        self.assertEqual(dialog.authorization_label.cget("text"), "Authorization required")
        self.assertIn("timed out", dialog.authorization_detail.cget("text"))
        self.assertFalse(dialog.save_button.instate(["disabled"]))
        self.assertTrue(dialog.cancel_authorization_button.instate(["disabled"]))
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(self.application.settings.accounts, [])

    def test_unavailable_credentials_can_be_rechecked_only_in_editor(self):
        account = self.open_oauth_editor()
        self.assertTrue(self.application._background.wait(THREAD_TIMEOUT))
        update_credential_data(
            self.application._credentials,
            account.id,
            msal_cache=microsoft_cache(account, [MICROSOFT_MAIL_READ_SCOPE]),
        )
        statuses = self.application.account_statuses
        statuses.credentials_unavailable(account, "Credential store locked")
        dialog = AccountDialog(
            self.root, 5, account, editor=self.application.account_editor(account.id)
        )
        self.addCleanup(dialog.destroy)
        dialog.variables["label"].set("Unsaved name")
        before = self.application.settings
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = statuses._inspect

        def inspect(account):
            entered.set()
            self.assertTrue(release.wait(THREAD_TIMEOUT))
            return original(account)

        self.assertTrue(dialog.retry_credentials_button.winfo_manager())
        with (
            patch.object(statuses, "_inspect", side_effect=inspect),
            patch.object(self.application, "_authorize") as authorize,
        ):
            try:
                dialog.retry_credentials_button.invoke()
                self.assertTrue(entered.wait(THREAD_TIMEOUT))
                self.assertEqual(dialog.authorization_label.cget("text"), "Checking authorization…")
            finally:
                release.set()
            self.wait_for_ui(
                lambda: dialog.authorization_label.cget("text") == "Authorized",
                "The editor did not display the successful credential check",
            )
            authorize.assert_not_called()
        self.assertFalse(dialog.retry_credentials_button.winfo_manager())
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(dialog.variables["label"].get(), "Unsaved name")
        self.assertEqual(self.application.settings, before)

    def test_invalid_input_during_sign_in_preserves_progress_and_restores_controls(self):
        dialog = AccountDialog(self.root, 5, editor=self.application.account_editor())
        self.addCleanup(dialog.destroy)
        dialog.variables["label"].set("Pending mailbox")
        dialog.variables["provider"].set("Outlook / Microsoft 365 (Microsoft Graph)")
        dialog._provider_changed()
        dialog.variables["username"].set("owner@example.org")
        dialog.variables["client_id"].set("client")
        dialog.mailboxes = [Mailbox("owner@example.org", ["INBOX"])]
        dialog._refresh_mailboxes()
        entered = threading.Event()

        def wait_for_cancel(account, credentials, *, cancelled):
            entered.set()
            self.assertTrue(cancelled.wait(THREAD_TIMEOUT))

        with patch.object(self.application, "_authorize", side_effect=wait_for_cancel):
            dialog.authorize_button.invoke()
            try:
                self.assertTrue(entered.wait(THREAD_TIMEOUT))
                dialog.variables["username"].set("")
                self.assertEqual(dialog.authorization_label.cget("text"), "Authorizing…")
                self.assertTrue(dialog.authorize_button.instate(["disabled"]))
                self.assertFalse(dialog.cancel_authorization_button.instate(["disabled"]))
            finally:
                dialog.cancel_authorization_button.invoke()
            self.assertTrue(self.application._background.wait(THREAD_TIMEOUT))
            dialog._update_authorization()
        self.assertFalse(dialog.save_button.instate(["disabled"]))
        self.assertFalse(dialog.widgets["provider"].instate(["disabled"]))
        self.assertFalse(dialog.widgets["auth"].instate(["disabled"]))

    def setUp(self):
        try:
            root = create_root()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")

        def close_window():
            for timer in root.tk.call("after", "info"):
                root.after_cancel(timer)
            root.destroy()

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
        self.wait_for_ui(
            lambda: (
                not self.desktop._archive_running
                and (message is None or self.desktop.progress_var.get() == message)
            ),
            "The mail check stayed busy",
        )

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
                self.desktop._refresh_account_rows()
                self.assertEqual(
                    self.desktop.account_tree.item(account.id, "values")[-1],
                    "Waiting for an active rule",
                )
        rule.enabled, rule.account_ids = True, [account.id]
        self.desktop.settings = self.application.save_rules([rule])
        self.desktop._refresh_account_rows()
        self.assertEqual(self.desktop.account_tree.item(account.id, "values")[-1], "Setting up")

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

    def test_activity_retry_recovers_preparation_failure_without_remote_access_when_paused(self):
        self.application.set_automatic_monitoring_paused(True)
        source = self.configure_stoppable_check("none")
        self.desktop.settings = self.application.settings
        self.desktop.refresh_all()
        service = self.application._context.execution.service
        with patch.object(
            service.engine.output_files,
            "occupied",
            side_effect=PermissionError("Destination unavailable"),
        ):
            self.desktop.check_button.invoke()
            self.wait_until_idle("Mail check finished.")
        item = self.application.current_jobs()[0]
        self.assertTrue(item.can_retry)
        detail = self.application.activity_detail(item.key)
        self.assertEqual(detail.mail[0].outputs, ())
        self.assertIn("Destination unavailable", detail.error)
        account = self.application.settings.accounts[0]
        account.enabled = False
        self.application.save_account(
            AccountSubmission(account, {}, False), replacing_id=account.id
        )
        self.assertFalse(service.has_automatic_work(self.application.settings))
        self.desktop.show_archive_activity()
        dialog = self.desktop.activity_dialog
        self.addCleanup(dialog.destroy)
        dialog.geometry("1220x760")
        dialog.current_tree.selection_set(item.key)
        self.root.update()
        self.assertFalse(dialog.retry_button.instate(["disabled"]))
        self.assertTrue(dialog.retry_button.winfo_ismapped())
        self.assertGreaterEqual(
            dialog.retry_button.winfo_height(), dialog.retry_button.winfo_reqheight()
        )
        self.assertLessEqual(
            dialog.retry_button.winfo_rooty() + dialog.retry_button.winfo_height(),
            dialog.winfo_rooty() + dialog.winfo_height(),
        )
        downloads = source.downloads
        dialog.retry_button.invoke()
        destination = self.application.database_path.parent / "archive"
        self.wait_for_ui(
            lambda: (
                len(list(destination.glob("*.eml"))) == 1
                and self.application._context.execution.is_idle()
            ),
            "The activity retry did not finish local archive work",
        )
        self.assertEqual(source.downloads, downloads)
        self.assertEqual(self.application.current_jobs(), ())
        self.assertEqual(self.application.activity_detail(item.key).item.status, "complete")

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

    def test_past_mail_dialog_uses_system_timezone_for_day_boundaries(self):
        rule = Rule(
            "Archive", targets=[RuleTarget(str(self.application.database_path.parent / "archive"))]
        )
        self.desktop.settings = self.application.save_rules([rule])
        self.desktop.refresh_all()
        self.desktop.rule_tree.selection_set(rule.id)

        def open_dialog(parent, selected_rule, timezone_name):
            dialog = RangeDialog(parent, selected_rule, timezone_name)
            try:
                self.assertEqual(dialog.zone_var.get(), "Europe/Berlin")
                self.assertIn("Europe/Berlin", dialog.zone_box.cget("values"))
                dialog.start_var.set("2026-03-29")
                dialog.end_var.set("2026-03-29")
            finally:
                self.root.after_idle(submit_dialog, dialog)
            return dialog

        def submit_dialog(dialog):
            try:
                dialog._save()
            finally:
                if dialog.winfo_exists():
                    dialog.destroy()

        with (
            patch(
                "mailarchive.presentation.timezone_choices.get_localzone_name",
                return_value="Europe/Berlin",
            ),
            patch(
                "mailarchive.presentation.desktop.RangeDialog", side_effect=open_dialog
            ) as dialog,
            patch("mailarchive.presentation.dialogs.messagebox.askyesno", return_value=True),
            patch.object(self.application, "apply_rule_to_past_mail") as apply,
        ):
            self.desktop.run_rule_history_dialog()
        self.addCleanup(self.desktop.activity_dialog.destroy)
        dialog.assert_called_once()
        apply.assert_called_once_with(
            rule.id,
            datetime(2026, 3, 28, 23, tzinfo=timezone.utc),
            datetime(2026, 3, 29, 22, tzinfo=timezone.utc),
            "Europe/Berlin",
        )
        self.assertEqual(self.application.settings.archive_timezone, "UTC")

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

    def test_empty_mail_check_updates_account_rows_after_baseline_completion(self):
        self.application.set_automatic_monitoring_paused(True)
        source = self.configure_stoppable_check("scan")
        source.messages.clear()
        self.desktop.settings = self.application.settings
        account = self.desktop.settings.accounts[0]
        self.desktop.refresh_all()
        self.desktop.account_tree.selection_set(account.id)
        revision = self.application.account_statuses.revision
        self.desktop.check_button.invoke()
        try:
            self.assertTrue(source.entered.wait(THREAD_TIMEOUT))
            self.root.update()
            self.assertEqual(self.desktop.account_tree.item(account.id, "values")[-1], "Setting up")
        finally:
            source.release.set()
        self.wait_until_idle("Mail check finished.")
        self.assertEqual(self.application.account_status(account.id).state, AccountState.ACTIVE)
        self.assertEqual(self.application.account_statuses.revision, revision)
        self.assertEqual(self.desktop.account_tree.item(account.id, "values")[-1], "Active")
        self.assertEqual(self.desktop.account_tree.selection(), (account.id,))
        self.assertEqual(self.desktop.account_notice_var.get(), "Mail: Active")

    def test_completed_check_survives_profile_read_failure_and_recovers_without_status_change(self):
        self.addCleanup(self.desktop.progress_bar.stop)
        self.application.set_automatic_monitoring_paused(True)
        source = self.configure_stoppable_check("scan")
        source.messages.clear()
        self.desktop.settings = self.application.settings
        account = self.desktop.settings.accounts[0]
        self.desktop.refresh_all()
        self.desktop.account_tree.selection_set(account.id)
        self.root.update()
        revision = self.application.account_statuses.revision
        finished = threading.Event()

        def receive_progress(progress):
            self.desktop.on_run_progress(progress)
            if progress.origin == "check" and not progress.active:
                finished.set()

        self.application.set_observers(self.desktop.on_service_event, receive_progress)
        self.desktop.check_button.invoke()
        self.assertTrue(source.entered.wait(THREAD_TIMEOUT))
        self.root.update()
        previous_row = self.desktop.account_tree.item(account.id, "values")
        source.release.set()
        self.assertTrue(finished.wait(THREAD_TIMEOUT))
        self.assertTrue(self.application._context.execution.is_idle())
        self.assertTrue(self.application._background.wait(THREAD_TIMEOUT))
        profile = self.application.database_path.parent
        unavailable = profile.with_name(profile.name + "-unavailable")
        profile.rename(unavailable)
        try:
            with self.assertLogs("mailarchive.presentation.desktop", level="ERROR"):
                self.desktop._drain_ui_queue()
            self.assertFalse(self.desktop._archive_running)
            self.assertIsNone(self.desktop._check_id)
            self.assertIsNone(self.desktop._progress_timer)
            self.assertEqual(self.desktop.check_button.cget("text"), "Check mail now")
            self.assertFalse(self.desktop.check_button.instate(["disabled"]))
            self.assertFalse(self.desktop.progress_bar.winfo_ismapped())
            self.assertEqual(self.desktop.progress_var.get(), "Mail check finished.")
            self.assertEqual(self.desktop.account_tree.item(account.id, "values"), previous_row)
            self.assertEqual(self.desktop.account_tree.selection(), (account.id,))
            self.assertIn("Retrying", self.desktop.account_notice_var.get())
        finally:
            unavailable.rename(profile)
        self.wait_for_ui(
            lambda: self.desktop.account_tree.item(account.id, "values")[-1] == "Active",
            "The account list did not recover after the profile became available",
        )
        self.assertEqual(self.application.account_statuses.revision, revision)
        self.assertEqual(self.desktop.account_tree.selection(), (account.id,))
        self.assertEqual(self.desktop.account_notice_var.get(), "Mail: Active")
        self.desktop.check_button.invoke()
        self.wait_until_idle("Mail check finished.")

    def test_all_terminal_states_reset_controls_when_account_status_read_fails(self):
        self.application.set_automatic_monitoring_paused(True)
        self.configure_stoppable_check("none")
        self.desktop.settings = self.application.settings
        account = self.desktop.settings.accounts[0]
        self.desktop.refresh_all()
        self.desktop.account_tree.selection_set(account.id)
        previous_row = self.desktop.account_tree.item(account.id, "values")
        for origin in ("check", "automatic", "operation", "retry"):
            for state in (ExecutionState.COMPLETED, ExecutionState.FAILED, ExecutionState.STOPPED):
                with self.subTest(origin=origin, state=state):
                    execution_id = f"{origin}-{state.value}"
                    if origin == "check":
                        self.desktop._check_id = execution_id
                    self.desktop._display_progress(
                        RunProgress("Running", execution_id=execution_id, origin=origin, sequence=1)
                    )
                    self.assertTrue(self.desktop._archive_running)
                    with (
                        patch.object(
                            self.application, "account_status", side_effect=OSError("Read failed")
                        ),
                        self.assertLogs("mailarchive.presentation.desktop", level="ERROR"),
                    ):
                        self.desktop._display_progress(
                            RunProgress(
                                state.value,
                                active=False,
                                execution_id=execution_id,
                                origin=origin,
                                state=state,
                                sequence=2,
                            )
                        )
                    self.assertFalse(self.desktop._archive_running)
                    self.assertIsNone(self.desktop._progress_timer)
                    self.assertEqual(self.desktop.progress_var.get(), state.value)
                    self.assertEqual(self.desktop.check_button.cget("text"), "Check mail now")
                    self.assertFalse(self.desktop.check_button.instate(["disabled"]))
                    self.assertEqual(
                        self.desktop.account_tree.item(account.id, "values"), previous_row
                    )
                    self.assertEqual(self.desktop.account_tree.selection(), (account.id,))
                    self.wait_for_ui(
                        lambda: self.desktop.account_notice_var.get() == "Mail: Setting up",
                        "The status retry did not recover",
                    )

    def test_failed_status_snapshot_preserves_all_rows_and_bounds_retries(self):
        self.application.set_automatic_monitoring_paused(True)
        self.configure_stoppable_check("none")
        second = Account("Second", "imap.example.org", "second@example.org")
        self.application.save_account(AccountSubmission(second, {"password": "test"}, True))
        self.desktop.settings = self.application.settings
        self.desktop.refresh_all()
        self.desktop.account_tree.selection_set(second.id)
        self.root.update()
        rows = {
            item: self.desktop.account_tree.item(item, "values")
            for item in self.desktop.account_tree.get_children()
        }
        self.desktop.settings.accounts[0].label = "Changed label"
        original = self.application.account_status

        def fail_second(account_id):
            if account_id == second.id:
                raise OSError("Read failed")
            return original(account_id)

        with (
            patch.object(self.application, "account_status", side_effect=fail_second) as read,
            patch("mailarchive.presentation.desktop.time.monotonic", return_value=100.0) as clock,
            self.assertLogs("mailarchive.presentation.desktop", level="ERROR") as logs,
        ):
            self.desktop._refresh_account_rows()
            self.assertEqual(read.call_count, 2)
            for _ in range(3):
                self.desktop._drain_ui_queue()
            self.assertEqual(read.call_count, 2)
            clock.return_value = 101.1
            self.desktop._drain_ui_queue()
            self.assertEqual(read.call_count, 4)
            self.desktop._refresh_account_notice()
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(
            {
                item: self.desktop.account_tree.item(item, "values")
                for item in self.desktop.account_tree.get_children()
            },
            rows,
        )
        self.assertEqual(self.desktop.account_tree.selection(), (second.id,))
        self.desktop._drain_ui_queue()
        self.assertEqual(
            self.desktop.account_tree.item(self.desktop.settings.accounts[0].id, "values")[0],
            "Changed label",
        )
        self.assertEqual(self.desktop.account_tree.selection(), (second.id,))
        self.assertEqual(self.desktop.account_notice_var.get(), "Second: Setting up")

    def test_selection_notice_read_failure_keeps_rows_and_recovers(self):
        self.application.set_automatic_monitoring_paused(True)
        self.configure_stoppable_check("none")
        self.desktop.settings = self.application.settings
        account = self.desktop.settings.accounts[0]
        self.desktop.refresh_all()
        self.desktop.account_tree.selection_set(account.id)
        self.root.update()
        row = self.desktop.account_tree.item(account.id, "values")
        with (
            patch.object(self.application, "account_status", side_effect=OSError("Read failed")),
            self.assertLogs("mailarchive.presentation.desktop", level="ERROR"),
        ):
            self.desktop._refresh_account_notice()
        self.assertEqual(self.desktop.account_tree.item(account.id, "values"), row)
        self.assertEqual(self.desktop.account_tree.selection(), (account.id,))
        self.assertIn("Retrying", self.desktop.account_notice_var.get())
        self.wait_for_ui(
            lambda: self.desktop.account_notice_var.get() == "Mail: Setting up",
            "The account notice did not recover after the read failure",
        )

    def test_saved_account_is_selected_after_failed_status_refresh_recovers(self):
        self.application.set_automatic_monitoring_paused(True)
        self.configure_stoppable_check("none")
        account = self.application.settings.accounts[0]
        submission = AccountSubmission(account, {}, False)
        with (
            patch.object(self.application, "account_status", side_effect=OSError("Read failed")),
            self.assertLogs("mailarchive.presentation.desktop", level="ERROR"),
        ):
            self.desktop._account_editor_closed(SimpleNamespace(result=submission))
        self.assertEqual(self.desktop.account_tree.get_children(), ())
        self.assertEqual(self.desktop.progress_var.get(), "Mail: Account saved.")
        self.assertIn("Retrying", self.desktop.account_notice_var.get())
        self.wait_for_ui(
            lambda: self.desktop.account_tree.selection() == (account.id,),
            "The saved account was not selected after the status retry",
        )
        self.assertEqual(self.desktop.account_tree.item(account.id, "values")[-1], "Setting up")
        self.assertEqual(self.desktop.account_notice_var.get(), "Mail: Setting up")
        self.assertEqual(len(self.application.settings.accounts), 1)

    def test_real_button_stops_download_remains_responsive_and_starts_again(self):
        source = self.configure_stoppable_check()
        button = self.desktop.check_button
        button.invoke()
        first_id = self.desktop._check_id
        self.assertEqual(button.cget("text"), "Stop check")
        self.assertTrue(source.entered.wait(THREAD_TIMEOUT))
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
