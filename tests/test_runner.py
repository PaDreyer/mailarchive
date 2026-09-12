import threading
import unittest
from unittest.mock import Mock, call, patch

from mailarchive.models import Account, Settings
from mailarchive.runner import STARTUP_DELAY_SECONDS, BackgroundRunner, polling_interval_minutes


class RunnerTests(unittest.TestCase):
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
        service.run_once.side_effect = lambda *_: completed.set()
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
