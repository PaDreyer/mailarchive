"""Whole-selection operation lifecycle and durable stop behavior."""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mailarchive.application.activity import ActivityQueries
from mailarchive.application.execution import ExecutionCoordinator
from mailarchive.application.source_port import RemoteMessage
from mailarchive.domain.configuration import (
    MICROSOFT_IMAP_HOST,
    Account,
    AuthMode,
    Mailbox,
    Rule,
    RuleTarget,
    Settings,
)
from mailarchive.infrastructure.activity_repository import SqliteActivityRepository
from mailarchive.infrastructure.operation_repository import OperationRepository
from mailarchive.infrastructure.output_files import _atomic_write as real_atomic_write
from tests.concurrency import THREAD_TIMEOUT
from tests.test_restart_core import FakeSource, PagedRangeSource, Registry, raw_mail
from tests.workspace_fixture import WorkspaceStore, make_service


class OperationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mailbox = Mailbox("one@example.org", ["INBOX"])
        self.second_mailbox = Mailbox("two@example.org", ["INBOX"])
        self.account = Account(
            "Mail",
            "imap.example.org",
            self.mailbox.address,
            mailboxes=[self.mailbox],
        )
        self.second_account = Account(
            "Other",
            "imap.example.org",
            self.second_mailbox.address,
            mailboxes=[self.second_mailbox],
        )
        self.rule = Rule(
            "Archive", targets=[RuleTarget(str(self.root / "A")), RuleTarget(str(self.root / "B"))]
        )
        self.settings = Settings(accounts=[self.account, self.second_account], rules=[self.rule])
        self.source = FakeSource(
            {
                "1": RemoteMessage(
                    "1", raw_mail(), datetime(2026, 1, 1, tzinfo=timezone.utc), "imap_internaldate"
                )
            }
        )
        self.state = WorkspaceStore(self.root / "profile" / "workspace.sqlite3")
        self.service = make_service(self.state, Registry(self.source))

    def _prepare(self) -> str:
        return self.service.prepare_range_operation(
            self.settings,
            {self.mailbox.id, self.second_mailbox.id},
            rule_id=self.rule.id,
        )

    def test_recovery_closes_interrupted_attempt_before_retry(self) -> None:
        operation_id = self._prepare()
        operations = OperationRepository(self.state.connection, self.state.configuration)
        self.assertTrue(operations.claim_manual_operation(operation_id))
        operations.mark_operation_source(operation_id, self.mailbox.id, "running")
        with self.state.connection() as db, db:
            operations.recover(db)
        first = (
            ActivityQueries(SqliteActivityRepository(self.state.connection))
            .detail("operation:" + operation_id)
            .attempts[0]
        )
        self.assertEqual(first.status, "interrupted")
        self.assertIsNotNone(first.finished_at)
        self.assertEqual(first.sources[0].status, "failed")
        self.assertIn("Interrupted", first.sources[0].error)
        self.assertTrue(operations.claim_manual_operation(operation_id))
        operations.finish_manual_operation(operation_id, "Retry failed")
        attempts = (
            ActivityQueries(SqliteActivityRepository(self.state.connection))
            .detail("operation:" + operation_id)
            .attempts
        )
        self.assertEqual(
            [(attempt.number, attempt.status) for attempt in attempts],
            [(1, "interrupted"), (2, "failed")],
        )

    def test_recovery_reconciles_waiting_without_open_plan(self) -> None:
        operation_id = self._prepare()
        operations = OperationRepository(self.state.connection, self.state.configuration)
        with self.state.connection() as db, db:
            db.execute("UPDATE manual_operation SET status='waiting' WHERE id=?", (operation_id,))
            operations.recover(db)
        self.assertEqual(operations.manual_operation(operation_id)["status"], "completed")

    def test_stop_returns_while_publication_is_in_flight_then_retains_receipt(self) -> None:
        operation_id = self._prepare()
        entered = threading.Event()
        release = threading.Event()
        failures: list[BaseException] = []

        def slow_publish(path, content):
            entered.set()
            if not release.wait(THREAD_TIMEOUT):
                raise RuntimeError("Timed out waiting for test publication")
            return real_atomic_write(path, content)

        def run() -> None:
            try:
                self.service.run_range_operation(operation_id)
            except BaseException as exc:
                failures.append(exc)

        with patch(
            "mailarchive.infrastructure.output_files._atomic_write", side_effect=slow_publish
        ):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(THREAD_TIMEOUT))
                self.assertTrue(self.state.request_stop_manual_operation(operation_id))
                self.assertEqual(self.state.manual_operation(operation_id)["status"], "stopping")
                self.assertTrue(worker.is_alive())
            finally:
                release.set()
                worker.join(THREAD_TIMEOUT)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.state.manual_operation(operation_id)["status"], "stopped")
        with self.state.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM receipt").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM scan_run").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM output_attempt").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT status FROM plan").fetchone()[0], "paused")
        self.assertEqual(len(list((self.root / "A").glob("*.eml"))), 1)
        self.assertEqual(len(list((self.root / "B").glob("*.eml"))), 0)
        self.assertFalse(self.state.automatic_work_due())

    def test_queued_stop_never_opens_a_mailbox(self) -> None:
        operation_id = self._prepare()
        self.state.request_stop_manual_operation(operation_id)
        self.assertEqual(self.service.run_range_operation(operation_id), [])
        self.assertEqual(self.state.manual_operation(operation_id)["status"], "stopped")
        with self.state.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM scan_run").fetchone()[0], 0)

    def test_coordinator_persists_selection_before_dispatch(self) -> None:
        coordinator = ExecutionCoordinator(
            self.service, lambda: self.settings, self.state.operations
        )
        operation_id = coordinator.apply_to_past_mail(self.rule.id, None, None, "UTC")
        operation = self.state.manual_operation(operation_id)
        self.assertEqual(operation["status"], "queued")
        self.assertEqual(
            [row["source_id"] for row in self.state.manual_operation_sources(operation_id)],
            [self.mailbox.id, self.second_mailbox.id],
        )
        self.assertTrue(coordinator.shutdown())
        self.assertEqual(self.state.manual_operation(operation_id)["status"], "interrupted")

    def test_offline_target_waits_then_retries_from_raw_without_provider(self) -> None:
        self.account.auth_mode = AuthMode.OAUTH_USER
        self.account.host = MICROSOFT_IMAP_HOST
        obstruction = self.root / "offline"
        obstruction.write_text("unavailable")
        self.rule.targets[1] = RuleTarget(str(obstruction / "archive"))
        operation_id = self.service.prepare_range_operation(
            self.settings, {self.mailbox.id}, rule_id=self.rule.id
        )
        self.service.run_range_operation(operation_id)
        self.assertEqual(self.state.manual_operation(operation_id)["status"], "waiting")
        activity = ActivityQueries(SqliteActivityRepository(self.state.connection))
        self.assertEqual([item.key for item in activity.current()], ["operation:" + operation_id])
        self.assertEqual(activity.history().items, ())
        detail = activity.detail("operation:" + operation_id)
        self.assertEqual([output.status for output in detail.mail[0].outputs], ["done", "error"])

        self.source.messages.clear()
        self.service.account_statuses.require_authorization(self.account)
        self.service.require_operation_authorization(operation_id)
        obstruction.unlink()
        self.assertEqual(self.service.resume_open(), (1, 0))
        self.assertEqual(self.state.manual_operation(operation_id)["status"], "completed")
        self.assertEqual(activity.current(), ())
        self.assertEqual(len(activity.history().items), 1)
        self.assertEqual(self.source.fetch_count, 1)

    def test_stopping_waiting_operation_prevents_later_output_retry(self) -> None:
        obstruction = self.root / "offline"
        obstruction.write_text("unavailable")
        self.rule.targets[1] = RuleTarget(str(obstruction / "archive"))
        operation_id = self.service.prepare_range_operation(
            self.settings, {self.mailbox.id}, rule_id=self.rule.id
        )
        self.service.run_range_operation(operation_id)
        self.assertEqual(self.state.manual_operation(operation_id)["status"], "waiting")
        self.assertTrue(self.state.request_stop_manual_operation(operation_id))
        self.assertEqual(self.state.manual_operation(operation_id)["status"], "stopping")
        self.state.finalize_stop_manual_operation(operation_id)
        obstruction.unlink()

        self.assertEqual(self.service.resume_open(), (0, 0))
        self.assertEqual(self.state.manual_operation(operation_id)["status"], "stopped")
        self.assertFalse((self.root / "offline" / "archive").exists())
        with self.state.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM receipt").fetchone()[0], 1)

    def test_retry_resumes_checkpoint_and_keeps_prior_scan_attempt(self) -> None:
        received = datetime(2026, 1, 1, tzinfo=timezone.utc)
        source = PagedRangeSource(
            {
                "1": RemoteMessage("1", raw_mail(), received, "imap_internaldate"),
                "2": RemoteMessage("2", raw_mail(), received, "imap_internaldate"),
            }
        )
        self.service.source_registry = Registry(source)
        operation_id = self.service.prepare_range_operation(
            self.settings, {self.mailbox.id}, rule_id=self.rule.id
        )
        self.service.run_range_operation(operation_id)
        self.assertEqual(self.state.manual_operation(operation_id)["status"], "failed")
        activity = ActivityQueries(SqliteActivityRepository(self.state.connection))
        first = activity.detail("operation:" + operation_id)
        self.assertEqual(first.sources[0].status, "failed")
        self.assertIn("second provider page failed", first.sources[0].error)
        self.assertEqual(len(first.attempts), 1)
        self.assertEqual(first.attempts[0].status, "failed")

        self.service.run_range_operation(operation_id)
        self.assertEqual(self.state.manual_operation(operation_id)["status"], "completed")
        detail = activity.detail("operation:" + operation_id)
        self.assertEqual(len(detail.attempts), 2)
        self.assertIn("second provider page failed", detail.attempts[0].error)
        self.assertEqual(detail.attempts[0].sources[0].status, "failed")
        self.assertEqual(detail.attempts[1].status, "completed")
        self.assertEqual(len(detail.mail), 2)


if __name__ == "__main__":
    unittest.main()
