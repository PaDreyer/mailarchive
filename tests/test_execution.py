"""The single execution worker owns polling, manual requests, and shutdown."""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from mailarchive.application.cancellation import NO_CANCELLATION
from mailarchive.application.events import EventLevel, ExecutionState
from mailarchive.application.execution import NO_RULES_NOTICE, ExecutionCoordinator
from mailarchive.domain.configuration import Account, Mailbox, Rule, Settings
from mailarchive.domain.rules import has_enabled_rule_for_account
from tests.concurrency import THREAD_TIMEOUT


def configured_settings() -> Settings:
    mailbox = Mailbox("owner@example.org", ["INBOX"])
    account = Account("Owner", "imap.example.org", mailbox.address, mailboxes=[mailbox])
    account.poll_minutes = 1
    return Settings(accounts=[account], rules=[Rule("Archive")])


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = configured_settings()
        self.service = Mock()
        self.service.has_automatic_work.return_value = False
        self.service.automatic_source_intervals.side_effect = lambda settings: {
            mailbox.id: (account.id, (account.poll_minutes or settings.default_poll_minutes) * 60)
            for account in settings.accounts
            if account.enabled and has_enabled_rule_for_account(settings.rules, account.id)
            for mailbox in account.mailboxes
            if mailbox.enabled
        }
        self.service.run_once.return_value = [
            SimpleNamespace(account_id=self.settings.accounts[0].id)
        ]

        def finish_accounts(settings, accounts, **kwargs):
            for account_id in accounts:
                kwargs["on_account_finished"](account_id)
            return self.service.run_once.return_value

        self.service.run_once.side_effect = finish_accounts
        self.operations = Mock()
        self.coordinator = ExecutionCoordinator(
            self.service, lambda: self.settings, self.operations
        )

    def test_due_schedule_and_force_check(self) -> None:
        account_id = self.settings.accounts[0].id
        with patch("mailarchive.application.execution.time.monotonic", return_value=100.0):
            self.coordinator._poll(False)
        self.service.run_once.assert_called_once_with(
            self.settings,
            {account_id},
            force_retry=False,
            cancellation=NO_CANCELLATION,
            excluded_source_ids=frozenset(),
            on_account_finished=ANY,
        )
        self.service.run_once.reset_mock()

        with patch("mailarchive.application.execution.time.monotonic", return_value=159.0):
            self.coordinator._poll(False)
        self.service.run_once.assert_not_called()

        with patch("mailarchive.application.execution.time.monotonic", return_value=161.0):
            self.coordinator._poll(False)
        self.service.run_once.assert_called_once_with(
            self.settings,
            {account_id},
            force_retry=False,
            cancellation=NO_CANCELLATION,
            excluded_source_ids=frozenset(),
            on_account_finished=ANY,
        )
        self.service.run_once.reset_mock()

        with patch("mailarchive.application.execution.time.monotonic", return_value=162.0):
            self.coordinator._poll(True)
        self.service.run_once.assert_called_once_with(
            self.settings,
            {account_id},
            force_retry=True,
            cancellation=NO_CANCELLATION,
            excluded_source_ids=frozenset(),
            on_account_finished=ANY,
        )

    def test_due_saved_work_runs_without_due_mailbox(self) -> None:
        account_id = self.settings.accounts[0].id
        with patch("mailarchive.application.execution.time.monotonic", return_value=100.0):
            self.coordinator._record_account_check(account_id)
        self.service.has_automatic_work.return_value = True
        with patch("mailarchive.application.execution.time.monotonic", return_value=101.0):
            self.coordinator._poll(False)
        self.service.run_once.assert_called_once_with(
            self.settings,
            set(),
            force_retry=False,
            cancellation=NO_CANCELLATION,
            excluded_source_ids=frozenset(),
            on_account_finished=ANY,
        )

    def test_empty_or_disabled_mailboxes_do_not_start_processing(self) -> None:
        disabled_account = configured_settings()
        disabled_account.accounts[0].enabled = False
        disabled_mailbox = configured_settings()
        disabled_mailbox.accounts[0].mailboxes[0].enabled = False
        for settings in (Settings(), disabled_account, disabled_mailbox):
            with self.subTest(settings=settings):
                self.settings = settings
                self.assertEqual(self.coordinator._poll(True), "No enabled mailboxes to check.")
                self.service.run_once.assert_not_called()

    def test_saved_work_is_still_processed_without_configured_accounts(self) -> None:
        self.settings = Settings()
        self.service.has_automatic_work.return_value = True
        self.service.run_once.return_value = []
        self.coordinator._poll(False)
        self.service.run_once.assert_called_once_with(
            self.settings,
            set(),
            force_retry=False,
            cancellation=NO_CANCELLATION,
            excluded_source_ids=frozenset(),
            on_account_finished=ANY,
        )

    def test_settings_change_preserves_last_check(self) -> None:
        with patch("mailarchive.application.execution.time.monotonic", return_value=100.0):
            self.coordinator._record_account_check(self.settings.accounts[0].id)
        self.coordinator.settings_changed(self.settings)
        with patch("mailarchive.application.execution.time.monotonic", return_value=159.0):
            self.coordinator._poll(False)
        self.service.run_once.assert_not_called()
        with patch("mailarchive.application.execution.time.monotonic", return_value=160.0):
            self.coordinator._poll(False)
        self.assertEqual(self.service.run_once.call_args.args[1], {self.settings.accounts[0].id})

    def test_ruleless_clicks_do_not_queue_or_publish_progress(self):
        progress = []
        self.coordinator._progress_handler = progress.append
        other = configured_settings().accounts[0]
        other.enabled = False
        self.settings.accounts.append(other)
        for rules in (
            [],
            [Rule("Disabled", enabled=False)],
            [Rule("Disabled account", account_ids=[other.id])],
            [Rule("Deleted account", account_ids=["deleted"])],
            [Rule("No accounts", account_ids=[])],
        ):
            with self.subTest(rules=rules):
                self.settings.rules = rules
                for _ in range(2):
                    self.assertIsNone(self.coordinator.check_mail_now())
                    self.assertTrue(self.coordinator.is_idle())
                    self.assertIsNone(self.coordinator._check)
                event = self.service.event_handler.call_args.args[0]
                self.assertEqual((event.level, event.message), (EventLevel.INFO, NO_RULES_NOTICE))
        self.service.automatic_source_intervals.assert_not_called()
        self.service.run_once.assert_not_called()
        self.assertEqual(progress, [])

    def test_scheduler_skips_ruleless_accounts_silently_without_changing_last_check(self):
        account_id = self.settings.accounts[0].id
        self.settings.rules = []
        with patch("mailarchive.application.execution.time.monotonic", return_value=10.0):
            self.coordinator._record_account_check(account_id)
        with patch("mailarchive.application.execution.time.monotonic", return_value=69.0):
            for _ in range(3):
                self.coordinator._poll(False)
            self.settings.rules = [Rule("Archive")]
            self.coordinator._poll(False)
        self.service.run_once.assert_not_called()
        self.service.event_handler.assert_not_called()
        with patch("mailarchive.application.execution.time.monotonic", return_value=70.0):
            self.coordinator._poll(False)
        self.assertEqual(self.service.run_once.call_args.args[1], {account_id})

    def test_partial_coverage_only_schedules_covered_account_and_reports_skip_count(self):
        account = self.settings.accounts[0]
        other = configured_settings().accounts[0]
        self.settings.accounts.append(other)
        self.settings.rules[0].account_ids = [account.id]
        with patch("mailarchive.application.execution.time.monotonic", return_value=10.0):
            self.coordinator._record_account_check(other.id)
        with patch("mailarchive.application.execution.time.monotonic", return_value=100.0):
            message = self.coordinator._poll(True)
        self.assertEqual(self.service.run_once.call_args.args[1], {account.id})
        self.assertEqual(message, "Mail check finished. Skipped 1 account without an active rule.")
        self.service.run_once.reset_mock()
        self.settings.rules[0].account_ids = [other.id]
        with patch("mailarchive.application.execution.time.monotonic", return_value=100.0):
            self.coordinator._poll(False)
        self.assertEqual(self.service.run_once.call_args.args[1], {other.id})

    def test_queued_check_revalidates_rules_and_allows_another_click(self):
        finished = threading.Event()
        progress = []

        def report(update):
            progress.append(update)
            if not update.active:
                finished.set()

        self.coordinator._progress_handler = report
        self.addCleanup(self.coordinator.shutdown)
        check_id = self.coordinator.check_mail_now()
        self.assertIsInstance(check_id, str)
        self.settings.rules = []
        self.coordinator.start()
        self.assertTrue(finished.wait(THREAD_TIMEOUT))
        self.assertEqual(progress[-1].message, NO_RULES_NOTICE)
        self.service.run_once.assert_not_called()
        finished.clear()
        self.settings.rules = [Rule("Archive")]
        self.assertIsInstance(self.coordinator.check_mail_now(), str)
        self.assertTrue(finished.wait(THREAD_TIMEOUT))
        self.service.run_once.assert_called_once()

    def test_one_worker_and_bounded_shutdown(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(self.coordinator.shutdown)
        self.addCleanup(release.set)

        def run_once(*_args, **_kwargs):
            entered.set()
            if not release.wait(THREAD_TIMEOUT):
                raise RuntimeError("test worker did not release")
            return [SimpleNamespace(account_id=self.settings.accounts[0].id)]

        self.service.run_once.side_effect = run_once
        self.coordinator.start()
        self.coordinator.start()
        self.assertTrue(self.coordinator.check_mail_now())
        self.assertTrue(entered.wait(THREAD_TIMEOUT))
        self.assertFalse(self.coordinator.check_mail_now())
        self.assertFalse(self.coordinator.shutdown(timeout=0))
        release.set()
        self.assertTrue(self.coordinator.shutdown(timeout=THREAD_TIMEOUT))
        self.assertFalse(self.coordinator.check_mail_now())
        self.service.request_shutdown.assert_called()

    def test_worker_reports_failure_and_survives_for_next_request(self) -> None:
        finished = threading.Event()
        second = threading.Event()
        calls = 0
        self.coordinator._progress_handler = lambda progress: (
            finished.set() if not progress.active else None
        )
        self.addCleanup(self.coordinator.shutdown)

        def run_once(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("simulated provider failure")
            second.set()
            return [SimpleNamespace(account_id=self.settings.accounts[0].id)]

        self.service.run_once.side_effect = run_once
        self.coordinator.start()
        with self.assertLogs("mailarchive.application.execution", level="ERROR"):
            self.assertTrue(self.coordinator.check_mail_now())
            self.assertTrue(finished.wait(THREAD_TIMEOUT))
        self.assertTrue(self.coordinator.is_idle())
        # A completed failed request no longer owns the worker, so another can run.
        self.assertTrue(self.coordinator.check_mail_now())
        self.assertTrue(second.wait(THREAD_TIMEOUT))
        self.assertTrue(self.coordinator.shutdown(timeout=THREAD_TIMEOUT))
        self.assertEqual(calls, 2)

    def test_stop_queued_check_does_not_stop_another_active_operation(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        progress = []
        self.coordinator._progress_handler = progress.append

        def manual(_operation_id):
            entered.set()
            if not release.wait(THREAD_TIMEOUT):
                raise AssertionError("Manual operation was not released")
            finished.set()

        self.service.run_range_operation.side_effect = manual
        self.addCleanup(self.coordinator.shutdown)
        self.addCleanup(release.set)
        with self.coordinator._condition:
            self.coordinator._manual.append("other-operation")
            check_id = self.coordinator.check_mail_now()
            self.coordinator.start()
        self.assertTrue(entered.wait(THREAD_TIMEOUT))
        self.assertTrue(self.coordinator.stop_check(check_id))
        self.assertFalse(self.coordinator.is_idle())
        self.operations.request_stop_manual_operation.assert_not_called()
        terminal = [p for p in progress if p.execution_id == check_id and not p.active]
        self.assertEqual([p.state for p in terminal], [ExecutionState.STOPPED])
        release.set()
        self.assertTrue(finished.wait(THREAD_TIMEOUT))
        self.service.run_once.assert_not_called()


if __name__ == "__main__":
    unittest.main()
