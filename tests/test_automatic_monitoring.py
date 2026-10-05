"""Global pause, persisted account deadlines, and cancellation through the facade."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from mailarchive.application.account_status import AccountStatusService
from mailarchive.application.errors import WorkspaceError
from mailarchive.application.execution import ExecutionCoordinator
from mailarchive.application.polling import (
    AutomaticMonitoringState,
    PollingCheckpoint,
    PollingSchedule,
)
from mailarchive.bootstrap import create_application
from mailarchive.domain.configuration import Account, Mailbox, Rule, RuleTarget, Settings
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.profile_database import ProfileDatabase
from mailarchive.infrastructure.profile_location import ConfigStore
from tests.concurrency import THREAD_TIMEOUT
from tests.test_check_cancellation import ControlledSource
from tests.test_restart_core import FakeSource, Registry

UTC = timezone.utc


class PollingScheduleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "workspace.sqlite3"
        self.state = ProfileDatabase(self.database)
        self.now = datetime(2026, 1, 1, 12, tzinfo=UTC)
        self.monotonic = 1000.0
        accounts = [
            Account(
                str(minutes),
                "imap.example.org",
                f"{minutes}@example.org",
                poll_minutes=minutes,
                mailboxes=[Mailbox(f"{minutes}@example.org", ["INBOX"])],
            )
            for minutes in (10, 20)
        ]
        self.settings = Settings(
            accounts=accounts,
            rules=[Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])],
        )
        self.service = Mock()
        self.statuses = AccountStatusService()
        self.service.account_status.side_effect = lambda account, settings, **kwargs: (
            self.statuses.resolve(account, settings.rules)
        )
        self.service.has_automatic_work.return_value = False
        self.service.run_once.side_effect = self.finish_accounts

    def finish_accounts(self, settings, accounts, **kwargs):
        for account_id in accounts:
            kwargs["on_account_finished"](account_id)
        return []

    def coordinator(self):
        return ExecutionCoordinator(
            self.service,
            lambda: self.settings,
            Mock(),
            polling_schedule=self.state.polling,
            automatic_monitoring_paused=self.settings.automatic_monitoring_paused,
            utc_now=lambda: self.now,
        )

    def seed_checked_accounts(self):
        checked = self.now - timedelta(minutes=8)
        for account in self.settings.accounts:
            self.state.polling.save(account.id, PollingCheckpoint(checked, checked))

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.monotonic += seconds

    def test_pause_time_counts_and_only_overdue_account_runs(self):
        self.seed_checked_accounts()
        with patch("mailarchive.application.execution.time.monotonic", lambda: self.monotonic):
            coordinator = self.coordinator()
            self.settings.automatic_monitoring_paused = True
            coordinator.settings_changed(self.settings)
            self.advance(300)
            coordinator._poll(False)
            self.service.run_once.assert_not_called()
            self.settings.automatic_monitoring_paused = False
            coordinator.settings_changed(self.settings)
            coordinator._poll(False)
            fast, slow = self.settings.accounts
            self.assertEqual(self.service.run_once.call_args.args[1], {fast.id})
            saved = self.state.polling.load()
            self.assertEqual(saved[fast.id].last_checked_at, self.now)
            self.assertEqual(saved[slow.id].last_checked_at, self.now - timedelta(minutes=13))
            self.service.run_once.reset_mock()
            coordinator._poll(False)
            self.service.run_once.assert_not_called()

    def test_short_pause_waits_until_exact_original_deadline(self):
        self.seed_checked_accounts()
        with patch("mailarchive.application.execution.time.monotonic", lambda: self.monotonic):
            coordinator = self.coordinator()
            self.settings.automatic_monitoring_paused = True
            coordinator.settings_changed(self.settings)
            self.advance(60)
            self.settings.automatic_monitoring_paused = False
            coordinator.settings_changed(self.settings)
            coordinator._poll(False)
            self.service.run_once.assert_not_called()
            self.advance(60)
            coordinator._poll(False)
            self.assertEqual(
                self.service.run_once.call_args.args[1], {self.settings.accounts[0].id}
            )

    def test_restart_preserves_elapsed_time_and_missed_intervals_run_once(self):
        self.seed_checked_accounts()
        self.settings.automatic_monitoring_paused = True
        self.state.configuration.save_settings(self.settings)
        self.advance(3 * 24 * 60 * 60)
        self.state = ProfileDatabase(self.database)
        self.settings = self.state.configuration.load_settings()
        with patch("mailarchive.application.execution.time.monotonic", lambda: self.monotonic):
            coordinator = self.coordinator()
            self.assertEqual(
                coordinator.automatic_monitoring_state(), AutomaticMonitoringState.PAUSED
            )
            coordinator._poll(False)
            self.service.run_once.assert_not_called()
            self.settings.automatic_monitoring_paused = False
            coordinator.settings_changed(self.settings)
            coordinator._poll(False)
            self.assertEqual(
                self.service.run_once.call_args.args[1], {a.id for a in self.settings.accounts}
            )
            coordinator._poll(False)
            self.service.run_once.assert_called_once()

    def test_first_due_time_survives_pause_and_restart_before_first_check(self):
        with patch("mailarchive.application.execution.time.monotonic", lambda: self.monotonic):
            coordinator = self.coordinator()
            coordinator._schedule.initialize(account.id for account in self.settings.accounts)
            self.settings.automatic_monitoring_paused = True
            coordinator.settings_changed(self.settings)
            self.advance(20)
            coordinator = self.coordinator()
            self.settings.automatic_monitoring_paused = False
            coordinator.settings_changed(self.settings)
            coordinator._poll(False)
            self.service.run_once.assert_not_called()
            self.advance(10)
            coordinator._poll(False)
            self.assertEqual(
                self.service.run_once.call_args.args[1], {a.id for a in self.settings.accounts}
            )

    def test_interval_edits_use_last_check_without_resetting_it(self):
        self.seed_checked_accounts()
        self.settings.accounts[0].poll_minutes = None
        self.settings.default_poll_minutes = 15
        with patch("mailarchive.application.execution.time.monotonic", lambda: self.monotonic):
            coordinator = self.coordinator()
            coordinator.settings_changed(self.settings)
            coordinator._poll(False)
            self.service.run_once.assert_not_called()
            self.settings.default_poll_minutes = 5
            coordinator.settings_changed(self.settings)
            coordinator._poll(False)
            self.assertEqual(
                self.service.run_once.call_args.args[1], {self.settings.accounts[0].id}
            )

    def test_failed_checkpoint_save_keeps_previous_deadline(self):
        self.seed_checked_accounts()
        saved = self.state.polling.load()
        with patch("mailarchive.application.execution.time.monotonic", lambda: self.monotonic):
            coordinator = self.coordinator()
            self.advance(300)
            with patch.object(self.state.polling, "save", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    coordinator._poll(False)
            self.assertEqual(self.state.polling.load(), saved)
            self.service.run_once.reset_mock()
            coordinator._poll(False)
            self.assertEqual(
                self.service.run_once.call_args.args[1], {self.settings.accounts[0].id}
            )

    def test_stop_before_start_preserves_initial_checkpoint_and_deferral(self):
        first, second = self.settings.accounts
        with patch("mailarchive.application.execution.time.monotonic", lambda: self.monotonic):
            schedule = PollingSchedule(self.state.polling, utc_now=lambda: self.now)
            schedule.defer_after_stop(first.id)
            schedule.initialize(account.id for account in self.settings.accounts)
            saved = self.state.polling.load()
            self.assertEqual(saved[first.id], PollingCheckpoint(self.now + timedelta(seconds=30)))
            self.advance(30)
            self.assertFalse(schedule.is_due(first.id, 10 * 60))
            self.assertTrue(schedule.is_due(second.id, 20 * 60))

    def test_existing_profile_is_extended_without_changing_configuration(self):
        self.state.configuration.save_settings(self.settings)
        with self.state.connection() as db, db:
            row = db.execute("SELECT id, payload FROM config_revision WHERE active=1").fetchone()
            old_settings = json.loads(row["payload"])
            del old_settings["automatic_monitoring_paused"]
            db.execute(
                "UPDATE config_revision SET payload=? WHERE id=?",
                (json.dumps(old_settings), row["id"]),
            )
            db.execute("DROP TABLE polling_schedule")
        reopened = ProfileDatabase(self.database)
        self.assertEqual(reopened.configuration.load_settings(), self.settings)
        self.assertFalse(reopened.configuration.load_settings().automatic_monitoring_paused)
        self.assertEqual(reopened.polling.load(), {})
        checkpoint = PollingCheckpoint(self.now, self.now)
        reopened.polling.save(self.settings.accounts[0].id, checkpoint)
        self.assertEqual(
            ProfileDatabase(self.database).polling.load(),
            {self.settings.accounts[0].id: checkpoint},
        )

    def test_damaged_schedule_is_rejected_without_replacing_it(self):
        with self.state.connection() as db, db:
            db.execute("DROP TABLE polling_schedule")
            db.execute("CREATE TABLE polling_schedule(unrelated TEXT)")
            db.execute("INSERT INTO polling_schedule VALUES ('preserve')")
        with self.assertRaisesRegex(WorkspaceError, "polling schedule is damaged"):
            ProfileDatabase(self.database)
        with self.state.connection() as db:
            self.assertEqual(
                db.execute("SELECT unrelated FROM polling_schedule").fetchone()[0], "preserve"
            )

    def test_invalid_or_naive_timestamps_are_rejected(self):
        for value in ("invalid", "2026-01-01T12:00:00"):
            with self.subTest(value=value), self.state.connection() as db, db:
                db.execute(
                    "INSERT OR REPLACE INTO polling_schedule VALUES ('a', ?, NULL)", (value,)
                )
            with self.assertRaisesRegex(WorkspaceError, "polling schedule is damaged"):
                ProfileDatabase(self.database)


class AutomaticMonitoringTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ConfigStore(self.root / "profile")
        self.mailboxes = [
            Mailbox(f"owner{n}@example.org", ["INBOX"], archive_existing_messages=True)
            for n in (1, 2)
        ]
        self.accounts = [
            Account(
                f"Mail {n}",
                "imap.example.org",
                mailbox.address,
                mailboxes=[mailbox],
                poll_minutes=n,
            )
            for n, mailbox in enumerate(self.mailboxes, 1)
        ]
        self.rule = Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])
        self.store.save(
            Settings(
                accounts=self.accounts,
                rules=[self.rule],
                start_at_login=False,
                automatic_monitoring_paused=True,
            )
        )
        state = ProfileDatabase(self.store.state_database_path())
        for account in self.accounts:
            state.polling.save(account.id, PollingCheckpoint(datetime(2026, 1, 1, tzinfo=UTC)))
        self.source = ControlledSource()
        self.finished = threading.Event()
        self.progress = []
        self.app = self.open_application()
        self.coordinator = self.app._context.execution
        self.addCleanup(self.app.close)
        self.addCleanup(self.source.release.set)

    def open_application(self):
        with (
            patch(
                "mailarchive.bootstrap.MessageSourceRegistry", return_value=Registry(self.source)
            ),
            patch("mailarchive.bootstrap.set_start_at_login"),
        ):
            application = create_application(self.store, MemoryCredentialStore())
        application.set_observers(lambda event: None, self.receive_progress)
        return application

    def receive_progress(self, progress):
        self.progress.append(progress)
        if not progress.active:
            self.finished.set()

    def start_automatic(self):
        self.finished.clear()
        self.app.start()
        self.app.set_automatic_monitoring_paused(False)

    def pause_blocked(self):
        self.assertTrue(self.source.entered.wait(THREAD_TIMEOUT))
        self.app.set_automatic_monitoring_paused(True)
        self.assertEqual(self.app.automatic_monitoring_state(), AutomaticMonitoringState.PAUSING)
        self.source.release.set()
        self.assertTrue(self.finished.wait(THREAD_TIMEOUT))
        self.assertEqual(self.app.automatic_monitoring_state(), AutomaticMonitoringState.PAUSED)

    def assert_no_automatic_work(self):
        with patch.object(self.coordinator.service, "run_once") as run:
            self.coordinator._poll(False)
            run.assert_not_called()

    def test_pause_during_download_retains_work_and_resumes_without_duplicate_outputs(self):
        self.source.phase = "download"
        self.start_automatic()
        self.pause_blocked()
        self.assertEqual(self.app.status().spool_bytes, 0)
        self.assertFalse((self.root / "archive").exists())
        saved = ProfileDatabase(self.app.database_path).polling.load()
        self.assertTrue(all(c.last_checked_at is None for c in saved.values()))
        self.assert_no_automatic_work()
        self.finished.clear()
        self.source.phase = "none"
        self.app.set_automatic_monitoring_paused(False)
        self.assertTrue(self.finished.wait(THREAD_TIMEOUT))
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 2)
        self.assertEqual(self.app.current_jobs(), ())
        saved = ProfileDatabase(self.app.database_path).polling.load()
        self.assertTrue(all(c.last_checked_at is not None for c in saved.values()))

    def test_pause_during_publication_keeps_receipt_and_retries_from_local_copy(self):
        self.source.phase = "none"
        rule = self.app.settings.rules[0]
        rule.targets.append(RuleTarget(str(self.root / "second")))
        self.app.save_rules([rule])
        writer = self.coordinator.service.engine.output_files
        publish = writer.publish

        def blocked_publish(path, content):
            self.source.block()
            publish(path, content)

        with patch.object(writer, "publish", side_effect=blocked_publish):
            self.start_automatic()
            self.pause_blocked()
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 1)
        self.assertFalse((self.root / "second").exists())
        self.assertGreater(self.app.status().spool_bytes, 0)
        self.assert_no_automatic_work()
        self.source.messages.clear()
        self.finished.clear()
        self.app.set_automatic_monitoring_paused(False)
        self.assertTrue(self.finished.wait(THREAD_TIMEOUT))
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 1)
        self.assertEqual(len(list((self.root / "second").glob("*.eml"))), 1)
        self.assertEqual(self.app.status().spool_bytes, 0)

    def test_completed_first_account_is_saved_when_later_account_is_paused(self):
        first = FakeSource({})
        registry = Mock()
        registry.get.side_effect = lambda account: (
            first if account.id == self.accounts[0].id else self.source
        )
        self.coordinator.service.source_registry = registry
        self.start_automatic()
        self.pause_blocked()
        state = ProfileDatabase(self.app.database_path)
        saved = state.polling.load()
        self.assertIsNotNone(saved[self.accounts[0].id].last_checked_at)
        self.assertIsNone(saved[self.accounts[1].id].last_checked_at)
        self.assertTrue(self.app.close())
        reopened = self.open_application()
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.settings.automatic_monitoring_paused)
        self.assertEqual(reopened.automatic_monitoring_state(), AutomaticMonitoringState.PAUSED)
        self.assertEqual(ProfileDatabase(reopened.database_path).polling.load(), saved)
        reopened.set_automatic_monitoring_paused(False)
        with patch.object(reopened._context.execution.service, "run_once") as run:
            reopened._context.execution._poll(False)
        self.assertEqual(run.call_args.args[1], {self.accounts[1].id})

    def test_manual_check_and_past_mail_run_are_available_while_paused(self):
        self.source.phase = "none"
        self.app.start()
        self.assertIsInstance(self.app.check_now(), str)
        self.assertTrue(self.finished.wait(THREAD_TIMEOUT))
        self.assertEqual(self.app.automatic_monitoring_state(), AutomaticMonitoringState.PAUSED)
        self.finished.clear()
        operation = self.app.apply_rule_to_past_mail(self.rule.id, None, None, "UTC")
        self.assertTrue(self.finished.wait(THREAD_TIMEOUT))
        self.assertEqual(
            self.app.activity_detail("operation:" + operation).item.status, "completed"
        )
        self.assertTrue(self.app.settings.automatic_monitoring_paused)
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 2)

    def test_explicit_retry_can_finish_failed_output_while_paused(self):
        self.source.phase = "none"
        self.app.start()
        writer = self.coordinator.service.engine.output_files
        with patch.object(writer, "publish", side_effect=OSError("offline")):
            self.assertIsInstance(self.app.check_now(), str)
            self.assertTrue(self.finished.wait(THREAD_TIMEOUT))
        plan = self.coordinator.service.delivery.open_plans()[0]
        self.finished.clear()
        self.app.retry_activity("mail:" + plan["id"])
        self.assertTrue(self.finished.wait(THREAD_TIMEOUT))
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 1)
        self.assertEqual(self.app.automatic_monitoring_state(), AutomaticMonitoringState.PAUSED)

    def test_failed_resume_keeps_persisted_pause_and_worker_gate(self):
        with patch.object(self.app._context, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.app.set_automatic_monitoring_paused(False)
        self.assertTrue(self.app.settings.automatic_monitoring_paused)
        self.assertTrue(self.store.load().automatic_monitoring_paused)
        self.assertEqual(self.app.automatic_monitoring_state(), AutomaticMonitoringState.PAUSED)
        self.assert_no_automatic_work()

    def test_fast_toggle_changes_do_not_clear_inflight_cancellation(self):
        self.start_automatic()
        self.assertTrue(self.source.entered.wait(THREAD_TIMEOUT))
        for _ in range(4):
            self.app.set_automatic_monitoring_paused(True)
            self.app.set_automatic_monitoring_paused(False)
        self.app.set_automatic_monitoring_paused(True)
        self.source.release.set()
        self.assertTrue(self.finished.wait(THREAD_TIMEOUT))
        self.assertEqual(self.source.downloads, 0)
        self.assertEqual(self.app.automatic_monitoring_state(), AutomaticMonitoringState.PAUSED)
        self.assert_no_automatic_work()

    def test_pause_is_specific_to_profile_and_survives_switching_back(self):
        original = self.app.database_path
        other = self.root / "other" / "workspace.sqlite3"
        self.app.start()
        self.app.switch_profile(other)
        self.assertFalse(self.app.settings.automatic_monitoring_paused)
        self.assertEqual(self.app.automatic_monitoring_state(), AutomaticMonitoringState.ACTIVE)
        self.app.switch_profile(original)
        self.assertTrue(self.app.settings.automatic_monitoring_paused)
        self.assertEqual(self.app.automatic_monitoring_state(), AutomaticMonitoringState.PAUSED)


if __name__ == "__main__":
    unittest.main()
