import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from mailarchive.credentials import MemoryCredentialStore
from mailarchive.models import Account, Settings
from mailarchive.runner import STARTUP_DELAY_SECONDS, BackgroundRunner, polling_interval_minutes
from mailarchive.service import AccountRunResult, ArchiveService, EventLevel
from mailarchive.storage import ArchiveState


class RunnerTests(unittest.TestCase):
    def test_busy_runs_retry_without_advancing_completion_or_losing_manual_requests(self) -> None:
        for manual in (False, True):
            with self.subTest(manual=manual), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                accounts = [Account("First"), Account("Second")]
                settings = Settings(str(root / "archive"), accounts=accounts)
                service = ArchiveService(
                    MemoryCredentialStore(), ArchiveState(root / "state.sqlite3")
                )
                service._run_account = Mock(
                    side_effect=lambda account, *_: AccountRunResult(account.id)
                )
                runner = BackgroundRunner(service, Mock(return_value=settings))
                if manual:
                    runner._last_run = {account.id: 99.0 for account in accounts}
                    self.assertTrue(runner.run_now())
                previous_completions = runner._last_run.copy()

                with patch("mailarchive.runner.time.monotonic", return_value=100.0):
                    with service.account_change():
                        runner._run_due_accounts()
                    self.assertEqual(runner._last_run, previous_completions)
                    self.assertEqual(runner._force, manual)
                    service._run_account.assert_not_called()

                    runner._run_due_accounts()

                self.assertEqual(service._run_account.call_count, 2)
                self.assertEqual(runner._last_run, {account.id: 100.0 for account in accounts})
                self.assertFalse(runner._force)

    def test_only_returned_accounts_receive_a_completion_time(self) -> None:
        first, second = Account("First"), Account("Second")
        settings = Settings.defaults()
        settings.accounts = [first, second]
        service = Mock()
        service.run_once.return_value = [AccountRunResult(first.id)]
        runner = BackgroundRunner(service, lambda: settings)

        with patch("mailarchive.runner.time.monotonic", return_value=100.0):
            runner._run_due_accounts()

        self.assertEqual(runner._last_run, {first.id: 100.0})

    def test_failed_run_is_reported_and_retried_without_recording_completion(self) -> None:
        account = Account("Test")
        settings = Settings.defaults()
        settings.accounts = [account]
        service = Mock()
        service.run_once.side_effect = [OSError("disk full"), [AccountRunResult(account.id)]]
        runner = BackgroundRunner(service, lambda: settings)
        runner._stop = Mock()
        runner._stop.is_set.side_effect = [False, False, True]
        runner._wake = Mock()

        def check_wait(*, timeout):
            self.assertFalse(runner._running)
            if service.run_once.call_count == 1:
                self.assertNotIn(account.id, runner._last_run)

        runner._wake.wait.side_effect = check_wait
        with (
            patch("mailarchive.runner.time.monotonic", side_effect=[100.0, 115.0, 116.0]),
            self.assertLogs("mailarchive.runner", level="ERROR"),
        ):
            runner._loop()

        self.assertEqual(service.run_once.call_args_list, [call(settings, {account.id})] * 2)
        self.assertEqual(runner._last_run, {account.id: 116.0})
        event = service.event_handler.call_args.args[0]
        self.assertEqual(event.level, EventLevel.ERROR)
        self.assertIn("disk full", event.message)
        self.assertTrue(runner.run_now())

    def test_failing_error_callback_does_not_stop_polling(self) -> None:
        settings = Settings.defaults()
        settings.accounts = [Account("Test")]
        service = Mock()
        service.run_once.side_effect = [RuntimeError("run failed"), []]
        service.event_handler.side_effect = RuntimeError("callback failed")
        runner = BackgroundRunner(service, lambda: settings)
        runner._stop = Mock()
        runner._stop.is_set.side_effect = [False, False, True]
        runner._wake = Mock()

        with self.assertLogs("mailarchive.runner", level="ERROR") as logs:
            runner._loop()

        self.assertEqual(service.run_once.call_count, 2)
        self.assertIn("callback failed", "\n".join(logs.output))
        self.assertFalse(runner._running)

    def test_settings_provider_failure_does_not_stop_polling(self) -> None:
        settings = Settings.defaults()
        settings.accounts = [Account("Test")]
        provider = Mock(side_effect=[RuntimeError("settings unavailable"), settings])
        service = Mock()
        service.run_once.return_value = []
        runner = BackgroundRunner(service, provider)
        runner._stop = Mock()
        runner._stop.is_set.side_effect = [False, False, True]
        runner._wake = Mock()

        with self.assertLogs("mailarchive.runner", level="ERROR"):
            runner._loop()

        service.run_once.assert_called_once_with(settings, {settings.accounts[0].id})
        self.assertIn("settings unavailable", service.event_handler.call_args.args[0].message)

    def test_account_uses_global_polling_interval_by_default(self) -> None:
        settings = Settings.defaults()
        settings.default_poll_minutes = 12
        account = Account(label="Default", poll_minutes=None)

        self.assertEqual(polling_interval_minutes(account, settings), 12)

    def test_account_polling_override_takes_precedence(self) -> None:
        settings = Settings.defaults()
        settings.default_poll_minutes = 12
        account = Account(label="Override", poll_minutes=3)

        self.assertEqual(polling_interval_minutes(account, settings), 3)

    def test_start_is_idempotent_and_stop_wakes_and_joins_worker(self) -> None:
        service = Mock()
        runner = BackgroundRunner(service, Settings.defaults)

        with patch("mailarchive.runner.threading.Thread") as thread_factory:
            worker = thread_factory.return_value
            worker.is_alive.return_value = True

            runner.start()
            runner.start()
            runner.stop()

        thread_factory.assert_called_once_with(
            target=runner._loop,
            name="MailArchive-Polling",
            daemon=True,
        )
        worker.start.assert_called_once_with()
        worker.join.assert_called_once_with(timeout=5)
        self.assertTrue(runner._stop.is_set())
        self.assertTrue(runner._wake.is_set())

    def test_run_now_forces_a_recent_account_during_one_controlled_loop(self) -> None:
        account = Account(label="Recent", enabled=True)
        settings = Settings.defaults()
        settings.accounts = [account]
        service = Mock()
        service.run_once.return_value = [AccountRunResult(account.id)]
        runner = BackgroundRunner(service, lambda: settings)
        runner._last_run[account.id] = 99.0
        runner._stop = Mock()
        runner._stop.is_set.side_effect = [False, True]
        runner._wake = Mock()

        runner.run_now()
        with patch("mailarchive.runner.time.monotonic", side_effect=[100.0, 101.0]):
            runner._loop()

        runner._wake.set.assert_called_once_with()
        self.assertEqual(
            runner._wake.wait.call_args_list,
            [call(timeout=STARTUP_DELAY_SECONDS), call(timeout=15)],
        )
        self.assertEqual(runner._wake.clear.call_count, 2)
        service.run_once.assert_called_once_with(settings, {account.id})
        self.assertFalse(runner._force)
        self.assertEqual(runner._last_run[account.id], 101.0)

    def test_loop_runs_only_due_enabled_accounts_and_records_completion_time(self) -> None:
        first_run = Account(label="First run", enabled=True)
        overdue = Account(label="Overdue", enabled=True, poll_minutes=2)
        recent = Account(label="Recent", enabled=True, poll_minutes=5)
        disabled = Account(label="Disabled", enabled=False)
        settings = Settings.defaults()
        settings.accounts = [first_run, overdue, recent, disabled]
        service = Mock()
        service.run_once.return_value = [
            AccountRunResult(first_run.id),
            AccountRunResult(overdue.id),
        ]
        runner = BackgroundRunner(service, lambda: settings)
        runner._last_run.update({overdue.id: 100.0, recent.id: 950.0})
        runner._stop = Mock()
        runner._stop.is_set.side_effect = [False, True]
        runner._wake = Mock()

        def wait_before_check(*, timeout):
            if timeout == STARTUP_DELAY_SECONDS:
                service.run_once.assert_not_called()
                self.assertEqual(runner._last_run, {overdue.id: 100.0, recent.id: 950.0})

        runner._wake.wait.side_effect = wait_before_check
        with patch("mailarchive.runner.time.monotonic", side_effect=[1000.0, 1001.0]):
            runner._loop()

        self.assertEqual(
            runner._wake.wait.call_args_list,
            [call(timeout=STARTUP_DELAY_SECONDS), call(timeout=15)],
        )
        due = {first_run.id, overdue.id}
        service.run_once.assert_called_once_with(settings, due)
        self.assertEqual(runner._last_run[first_run.id], 1001.0)
        self.assertEqual(runner._last_run[overdue.id], 1001.0)
        self.assertEqual(runner._last_run[recent.id], 950.0)
        self.assertNotIn(disabled.id, runner._last_run)

    def test_run_now_interrupts_startup_delay(self) -> None:
        account = Account(label="Manual", enabled=True)
        settings = Settings.defaults()
        settings.accounts = [account]
        completed = threading.Event()
        service = Mock()

        def complete_run(*_):
            completed.set()
            return [AccountRunResult(account.id)]

        service.run_once.side_effect = complete_run
        runner = BackgroundRunner(service, lambda: settings)
        waiting = threading.Event()
        real_wait = runner._wake.wait

        def observe_wait(*, timeout):
            if timeout == STARTUP_DELAY_SECONDS:
                waiting.set()
            return real_wait(timeout=timeout)

        with patch.object(runner._wake, "wait", side_effect=observe_wait):
            try:
                runner.start()
                self.assertTrue(waiting.wait(timeout=2), "Worker did not start waiting.")
                service.run_once.assert_not_called()
                runner.run_now()
                self.assertTrue(completed.wait(timeout=2), "Manual run did not wake the worker.")
            finally:
                runner.stop()

        service.run_once.assert_called_once_with(settings, {account.id})

    def test_repeated_manual_requests_do_not_queue_another_run(self) -> None:
        account = Account(label="Manual", enabled=True)
        settings = Settings.defaults()
        settings.accounts = [account]
        service = Mock()
        runner = BackgroundRunner(service, lambda: settings)
        runner._stop = Mock()
        runner._stop.is_set.side_effect = [False, True]
        runner._wake = Mock()

        self.assertTrue(runner.run_now())
        self.assertFalse(runner.run_now())

        def check_running(*_):
            self.assertFalse(runner.run_now())
            self.assertFalse(runner._force)
            return [AccountRunResult(account.id)]

        service.run_once.side_effect = check_running
        runner._loop()

        service.run_once.assert_called_once_with(settings, {account.id})
        self.assertFalse(runner._running)
        self.assertTrue(runner.run_now())

    def test_manual_run_without_enabled_accounts_reaches_service(self) -> None:
        settings = Settings.defaults()
        settings.accounts = [Account(label="Disabled", enabled=False)]
        service = Mock()
        service.run_once.return_value = []
        runner = BackgroundRunner(service, lambda: settings)
        runner._stop = Mock()
        runner._stop.is_set.side_effect = [False, True]
        runner._wake = Mock()
        runner.run_now()

        runner._loop()

        service.run_once.assert_called_once_with(settings, set())
        self.assertFalse(runner._running)

    def test_stop_interrupts_startup_delay_without_checking_mail(self) -> None:
        service = Mock()
        settings_provider = Mock(return_value=Settings.defaults())
        runner = BackgroundRunner(service, settings_provider)
        waiting = threading.Event()
        real_wait = runner._wake.wait

        def observe_wait(*, timeout):
            if timeout == STARTUP_DELAY_SECONDS:
                waiting.set()
            return real_wait(timeout=timeout)

        with patch.object(runner._wake, "wait", side_effect=observe_wait):
            try:
                runner.start()
                self.assertTrue(waiting.wait(timeout=2), "Worker did not start waiting.")
            finally:
                runner.stop()

        self.assertFalse(runner._thread.is_alive())
        settings_provider.assert_not_called()
        service.run_once.assert_not_called()


if __name__ == "__main__":
    unittest.main()
