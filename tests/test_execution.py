"""The single execution worker owns polling, manual requests, and shutdown."""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mailarchive.application.execution import ExecutionCoordinator
from mailarchive.domain.configuration import Account, Mailbox, Settings


def configured_settings() -> Settings:
    mailbox = Mailbox("owner@example.org", ["INBOX"])
    account = Account("Owner", "imap.example.org", mailbox.address, mailboxes=[mailbox])
    account.poll_minutes = 1
    return Settings(accounts=[account])


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = configured_settings()
        self.service = Mock()
        self.service.has_automatic_work.return_value = False
        self.service.run_once.return_value = [
            SimpleNamespace(account_id=self.settings.accounts[0].id)
        ]
        self.operations = Mock()
        self.coordinator = ExecutionCoordinator(
            self.service, lambda: self.settings, self.operations
        )

    def test_due_schedule_and_force_check(self) -> None:
        account_id = self.settings.accounts[0].id
        with patch("mailarchive.application.execution.time.monotonic", return_value=100.0):
            self.coordinator._poll(False)
        self.service.run_once.assert_called_once_with(
            self.settings, {account_id}, force_retry=False
        )
        self.service.run_once.reset_mock()

        with patch("mailarchive.application.execution.time.monotonic", return_value=159.0):
            self.coordinator._poll(False)
        self.service.run_once.assert_not_called()

        with patch("mailarchive.application.execution.time.monotonic", return_value=161.0):
            self.coordinator._poll(False)
        self.service.run_once.assert_called_once_with(
            self.settings, {account_id}, force_retry=False
        )
        self.service.run_once.reset_mock()

        with patch("mailarchive.application.execution.time.monotonic", return_value=162.0):
            self.coordinator._poll(True)
        self.service.run_once.assert_called_once_with(self.settings, {account_id}, force_retry=True)

    def test_due_saved_work_runs_without_due_mailbox(self) -> None:
        account_id = self.settings.accounts[0].id
        self.coordinator._last_run[account_id] = 100.0
        self.service.has_automatic_work.return_value = True
        with patch("mailarchive.application.execution.time.monotonic", return_value=101.0):
            self.coordinator._poll(False)
        self.service.run_once.assert_called_once_with(self.settings, set(), force_retry=False)

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
        self.coordinator._poll(True)
        self.service.run_once.assert_called_once_with(self.settings, set(), force_retry=True)

    def test_settings_change_resets_due_schedule(self) -> None:
        self.coordinator._last_run[self.settings.accounts[0].id] = 100.0
        self.coordinator.reset_schedule()
        self.assertEqual(self.coordinator._last_run, {})

    def test_one_worker_and_bounded_shutdown(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def run_once(*_args, **_kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test worker did not release")
            return [SimpleNamespace(account_id=self.settings.accounts[0].id)]

        self.service.run_once.side_effect = run_once
        self.coordinator.start()
        self.coordinator.start()
        self.assertTrue(self.coordinator.check_mail_now())
        self.assertTrue(entered.wait(5))
        self.assertFalse(self.coordinator.check_mail_now())
        self.assertFalse(self.coordinator.shutdown(timeout=0))
        release.set()
        self.assertTrue(self.coordinator.shutdown(timeout=5))
        self.assertFalse(self.coordinator.check_mail_now())
        self.service.request_shutdown.assert_called()

    def test_worker_reports_failure_and_survives_for_next_request(self) -> None:
        first = threading.Event()
        second = threading.Event()
        calls = 0

        def run_once(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                first.set()
                raise RuntimeError("simulated provider failure")
            second.set()
            return [SimpleNamespace(account_id=self.settings.accounts[0].id)]

        self.service.run_once.side_effect = run_once
        self.coordinator.start()
        with self.assertLogs("mailarchive.application.execution", level="ERROR"):
            self.assertTrue(self.coordinator.check_mail_now())
            self.assertTrue(first.wait(5))
        # A completed failed request no longer owns the worker, so another can run.
        for _ in range(100):
            if self.coordinator.check_mail_now():
                break
            second.wait(0.01)
        self.assertTrue(second.wait(5))
        self.assertTrue(self.coordinator.shutdown(timeout=5))
        self.assertGreaterEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
