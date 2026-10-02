"""Profile lifetime and configuration behavior through the application boundary."""

from __future__ import annotations

import tempfile
import threading
import unittest
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

from mailarchive.application.background import BackgroundTasks
from mailarchive.application.profile import ProfileContext
from mailarchive.application.session import MailArchiveApplication
from mailarchive.domain.configuration import Account, Rule, RuleTarget, Settings
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.presentation.account_form import AccountSubmission


class Profiles:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.persisted = {path: Settings.defaults()}
        self.contexts = []
        self.fail_path = None
        self.fail_start_path = None

    def open(self, path, on_event, on_progress):
        if path == self.fail_path:
            raise RuntimeError("Invalid profile")
        settings = deepcopy(self.persisted.setdefault(path, Settings.defaults()))

        def save(candidate):
            self.persisted[path] = deepcopy(candidate)

        execution = Mock()
        execution.shutdown.return_value = True
        if path == self.fail_start_path:
            execution.start.side_effect = RuntimeError("New worker start failed")
        context = ProfileContext(
            path, settings, save, execution, Mock(), Mock(), Mock(), nullcontext, Mock(), on_event
        )
        self.contexts.append(context)
        return context

    def activate(self, path):
        self.path = path


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profiles = Profiles(self.root / "first" / "workspace.sqlite3")
        self.credentials = MemoryCredentialStore()
        self.authorize = Mock()
        self.startup = Mock()
        self.app = MailArchiveApplication(
            self.profiles,
            self.credentials,
            authorize=self.authorize,
            configure_startup=self.startup,
            update_check=lambda: "release",
        )
        self.addCleanup(self.app.close)

    def test_settings_are_detached_and_failed_save_keeps_current_configuration(self):
        candidate = self.app.settings
        candidate.default_poll_minutes = 11
        self.assertEqual(self.app.settings.default_poll_minutes, 5)
        self.profiles.contexts[0].save = Mock(side_effect=OSError("disk full"))
        with self.assertRaisesRegex(OSError, "disk full"):
            self.app.save_settings(candidate)
        self.assertEqual(self.app.settings.default_poll_minutes, 5)

    def test_failed_account_save_restores_credentials_and_does_not_publish_account(self):
        account = Account("Mail", "imap.example.org", "owner@example.org")
        self.credentials.set(account.id, "previous secret")
        self.profiles.contexts[0].save = Mock(side_effect=OSError("disk full"))
        with self.assertRaisesRegex(OSError, "disk full"):
            self.app.save_account(AccountSubmission(account, {"password": "new secret"}, True))
        self.assertEqual(self.app.settings.accounts, [])
        self.assertEqual(self.credentials.get(account.id), "previous secret")

    def test_rule_changes_publish_only_after_save_and_do_not_share_mutable_targets(self):
        rule = Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])
        self.app.save_rules([rule])
        rule.targets[0].path = str(self.root / "changed")
        self.assertEqual(self.app.settings.rules[0].targets[0].path, str(self.root / "archive"))

    def test_failed_settings_save_restores_startup_setting(self):
        candidate = self.app.settings
        candidate.start_at_login = False
        self.profiles.contexts[0].save = Mock(side_effect=OSError("disk full"))
        with self.assertRaises(OSError):
            self.app.save_settings(candidate)
        self.assertEqual([call.args for call in self.startup.call_args_list], [(False,), (True,)])

    def test_profile_switch_waits_for_old_worker_before_opening_new_profile(self):
        self.app.start()
        old = self.profiles.contexts[0]
        old.execution.shutdown.return_value = False
        destination = self.root / "second" / "workspace.sqlite3"
        with self.assertRaisesRegex(RuntimeError, "still stopping"):
            self.app.switch_profile(destination, timeout=0)
        self.assertEqual(len(self.profiles.contexts), 1)
        self.assertEqual(self.app.database_path, old.database_path)
        self.assertEqual(self.profiles.path, old.database_path)
        old.execution.shutdown.return_value = True
        self.app.switch_profile(destination)
        self.assertEqual(self.profiles.path, destination)
        self.profiles.contexts[-1].execution.start.assert_called_once()

    def test_failed_switch_rebuilds_previous_context_without_rebinding_stopped_services(self):
        self.app.start()
        old = self.profiles.contexts[0]
        self.profiles.fail_path = self.root / "invalid" / "workspace.sqlite3"
        with self.assertRaisesRegex(RuntimeError, "Invalid profile"):
            self.app.switch_profile(self.profiles.fail_path)
        self.assertEqual(self.app.database_path, old.database_path)
        self.assertIsNot(self.profiles.contexts[-1], old)
        self.profiles.contexts[-1].execution.start.assert_called_once()
        old.execution.start.assert_called_once()

    def test_failed_new_worker_start_restores_locator_and_fresh_previous_worker(self):
        self.app.start()
        old = self.profiles.contexts[0]
        destination = self.root / "second" / "workspace.sqlite3"
        self.profiles.fail_start_path = destination

        with self.assertRaisesRegex(RuntimeError, "New worker start failed"):
            self.app.switch_profile(destination)

        self.assertEqual(self.profiles.path, old.database_path)
        self.assertEqual(self.app.database_path, old.database_path)
        replacement = self.profiles.contexts[1]
        replacement.execution.shutdown.assert_called_once()
        self.assertIsNot(self.profiles.contexts[-1], old)
        self.profiles.contexts[-1].execution.start.assert_called_once()
        self.assertTrue(self.app.check_now())

    def test_partly_started_replacement_waits_to_stop_before_old_worker_restarts(self):
        self.app.start()
        old = self.profiles.contexts[0]
        destination = self.root / "second" / "workspace.sqlite3"
        release = threading.Event()
        recovered = threading.Event()
        original_open = self.profiles.open

        def open_profile(path, on_event, on_progress):
            context = original_open(path, on_event, on_progress)
            if path == destination:
                context.execution.start.side_effect = RuntimeError("Partial worker start")

                def shutdown(*, timeout=5.0):
                    release.wait(min(timeout, 0.02))
                    return release.is_set()

                context.execution.shutdown.side_effect = shutdown
            elif context is not old:
                recovered.set()
            return context

        self.profiles.open = open_profile
        with self.assertRaisesRegex(RuntimeError, "Partial worker start"):
            self.app.switch_profile(destination, timeout=0)
        self.assertEqual(self.profiles.path, destination)
        with self.assertRaisesRegex(RuntimeError, "changing profiles"):
            self.app.check_now()
        self.assertEqual(len(self.profiles.contexts), 2)

        release.set()
        self.assertTrue(recovered.wait(2))
        with self.app._lock:
            pass
        self.assertEqual(self.profiles.path, old.database_path)
        self.assertEqual(self.app.database_path, old.database_path)
        self.assertTrue(self.app.check_now())

    def test_timed_out_switch_recovers_old_monitoring_after_worker_exits(self):
        self.app.start()
        old = self.profiles.contexts[0]
        release = threading.Event()
        recovered = threading.Event()
        original_open = self.profiles.open

        def open_profile(path, on_event, on_progress):
            context = original_open(path, on_event, on_progress)
            if path == old.database_path and context is not old:
                recovered.set()
            return context

        def shutdown(*, timeout=5.0):
            release.wait(min(timeout, 0.02))
            return release.is_set()

        self.profiles.open = open_profile
        old.execution.shutdown.side_effect = shutdown
        with self.assertRaisesRegex(RuntimeError, "still stopping"):
            self.app.switch_profile(self.root / "second" / "workspace.sqlite3", timeout=0)
        with self.assertRaisesRegex(RuntimeError, "changing profiles"):
            self.app.check_now()

        release.set()
        self.assertTrue(recovered.wait(2))
        with self.app._lock:
            pass
        self.assertEqual(self.app.database_path, old.database_path)
        self.assertTrue(self.app.check_now())
        self.profiles.contexts[-1].execution.start.assert_called_once()

    def test_close_during_timed_out_switch_never_restarts_previous_worker(self):
        self.app.start()
        old = self.profiles.contexts[0]
        release = threading.Event()

        def shutdown(*, timeout=5.0):
            release.wait(min(timeout, 0.02))
            return release.is_set()

        old.execution.shutdown.side_effect = shutdown
        with self.assertRaisesRegex(RuntimeError, "still stopping"):
            self.app.switch_profile(self.root / "second" / "workspace.sqlite3", timeout=0)
        self.assertFalse(self.app.close(timeout=0))
        release.set()
        if self.app._recovery_thread:
            self.app._recovery_thread.join(2)
        self.assertEqual(len(self.profiles.contexts), 1)
        old.execution.start.assert_called_once()

    def test_generic_settings_save_cannot_change_accounts(self):
        candidate = self.app.settings
        candidate.accounts.append(Account("Mail", "imap.example.org", "owner@example.org"))
        with self.assertRaisesRegex(ValueError, "account editor"):
            self.app.save_settings(candidate)
        self.assertEqual(self.app.settings.accounts, [])

    def test_account_authorization_is_cancelled_before_profile_replacement(self):
        started = threading.Event()
        stopped = threading.Event()

        def authorize(account, credentials, *, cancelled):
            started.set()
            self.assertTrue(cancelled.wait(2))
            stopped.set()

        self.authorize.side_effect = authorize
        account = Account("Mail", "imap.example.org", "owner@example.org")
        self.app.save_account(AccountSubmission(account, {}, False))
        self.assertTrue(self.app.authorize_account(account.id))
        self.assertTrue(started.wait(1))
        self.app.switch_profile(self.root / "second" / "workspace.sqlite3")
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.app.authorizing_account_ids, frozenset())

    def test_update_completion_runs_on_dispatching_thread(self):
        calls = []
        owner = threading.get_ident()
        self.app.check_for_updates(
            lambda result, error: calls.append((result, error, threading.get_ident()))
        )
        self.assertTrue(self.app._background.wait(1))
        self.assertEqual(calls, [])
        self.app.dispatch_callbacks()
        self.assertEqual(calls, [("release", "", owner)])


class BackgroundTests(unittest.TestCase):
    def test_close_reports_active_task_and_waits_for_its_safe_completion(self):
        tasks = BackgroundTasks()
        release = threading.Event()
        entered = threading.Event()

        def work():
            entered.set()
            release.wait(2)

        try:
            tasks.submit(work, lambda result: None)
            self.assertTrue(entered.wait(1))
            self.assertFalse(tasks.close(0))
            with self.assertRaisesRegex(RuntimeError, "closing"):
                tasks.submit(lambda: None, lambda result: None)
        finally:
            release.set()
            self.assertTrue(tasks.close(2))
