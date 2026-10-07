"""Retry admission covers queued work and native database claim contention."""

import threading
import unittest
from datetime import datetime, timezone

from mailarchive.application.events import ExecutionState
from mailarchive.application.execution import ExecutionCoordinator
from mailarchive.application.source_port import RemoteMessage
from tests import test_retry_boundaries as retry_fixture
from tests.concurrency import THREAD_TIMEOUT
from tests.test_restart_core import FakeSource, Registry, raw_mail


class RetryAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = retry_fixture.ManualRetryBoundaryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.progress = []
        self.events = []
        self.observed = threading.Condition()
        self.dispatched = threading.Event()
        self.fixture.service.event_handler = self.events.append
        self.coordinator = ExecutionCoordinator(
            self.fixture.service,
            lambda: self.fixture.settings,
            self.fixture.state.operations,
            automatic_monitoring_paused=True,
            progress_handler=self.observe,
        )
        self.addCleanup(self.coordinator.shutdown)

    def observe(self, progress):
        with self.observed:
            self.progress.append(progress)
            if progress.state == ExecutionState.RUNNING:
                self.dispatched.set()
            self.observed.notify_all()

    def terminals(self):
        return [item for item in self.progress if item.state and not item.state.active]

    def wait_for_terminals(self, count):
        with self.observed:
            self.assertTrue(
                self.observed.wait_for(lambda: len(self.terminals()) >= count, THREAD_TIMEOUT),
                "Retry processing did not finish",
            )

    def test_queued_manual_retry_is_accepted_once(self):
        operation = self.fixture._first_attempt()
        key = "operation:" + operation
        self.assertTrue(self.coordinator.retry_activity(key))
        self.assertFalse(self.coordinator.retry_activity(key))
        self.assertEqual(list(self.coordinator._manual), [operation])

    def test_active_manual_retry_waiting_for_sqlite_writer_is_not_readmitted(self):
        operation = self.fixture._first_attempt()
        self.fixture.obstruction.unlink()
        self.events.clear()
        self.coordinator.start()
        with self.fixture.state.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self.assertTrue(self.coordinator.retry_activity("operation:" + operation))
                self.assertTrue(self.dispatched.wait(THREAD_TIMEOUT))
                self.assertFalse(self.coordinator.retry_activity("operation:" + operation))
            finally:
                db.rollback()
        # A subsequent independent operation drains every earlier command. This
        # catches a ghost terminal event even after the original job succeeded.
        barrier = self.coordinator.apply_to_past_mail(self.fixture.rule.id, None, None, "UTC")
        self.wait_for_terminals(2)
        self.assertEqual(
            [item.state for item in self.terminals()],
            [ExecutionState.COMPLETED, ExecutionState.COMPLETED],
        )
        for operation_id in (operation, barrier):
            self.assertEqual(
                self.fixture.state.operations.manual_operation(operation_id)["status"],
                "completed",
            )
        self.assertEqual(len(list((self.fixture.obstruction / "archive").glob("*.eml"))), 2)
        self.assertFalse([event for event in self.events if event.level.value == "error"])

    def test_queued_and_active_mail_retry_are_deduplicated_until_completion(self):
        self.fixture.mailbox.archive_existing_messages = True
        source = FakeSource(
            {
                "1": RemoteMessage(
                    "1", raw_mail(), datetime(2026, 1, 1, tzinfo=timezone.utc), "imap_internaldate"
                )
            }
        )
        self.fixture.service.source_registry = Registry(source)
        first = self.fixture.service.run_once(self.fixture.settings)[0]
        self.assertEqual((first.archived, first.failed), (0, 1))
        plan = self.fixture.state.delivery.work_plans()[0]
        key = "mail:" + plan["id"]
        self.fixture.obstruction.unlink()
        self.events.clear()
        with self.fixture.state.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self.assertTrue(self.coordinator.retry_activity(key))
                self.assertFalse(self.coordinator.retry_activity(key))
                self.coordinator.start()
                self.assertTrue(self.dispatched.wait(THREAD_TIMEOUT))
                self.assertFalse(self.coordinator.retry_activity(key))
            finally:
                db.rollback()
        self.wait_for_terminals(1)
        self.assertIsInstance(self.coordinator.check_mail_now(), str)
        self.wait_for_terminals(2)
        self.assertEqual(
            [item.state for item in self.terminals()],
            [ExecutionState.COMPLETED, ExecutionState.COMPLETED],
        )
        with self.fixture.state.connection() as db:
            status = db.execute("SELECT status FROM plan WHERE id=?", (plan["id"],)).fetchone()[0]
            self.assertEqual(status, "complete")
        self.assertEqual(len(list((self.fixture.obstruction / "archive").glob("*.eml"))), 1)
        self.assertFalse([event for event in self.events if event.level.value == "error"])

    def test_failed_retry_can_be_accepted_again_after_its_execution_finishes(self):
        operation = self.fixture._first_attempt()
        key = "operation:" + operation
        self.coordinator.start()
        self.assertTrue(self.coordinator.retry_activity(key))
        self.wait_for_terminals(1)
        self.assertEqual(self.terminals()[0].state, ExecutionState.FAILED)
        self.fixture.obstruction.unlink()
        self.assertTrue(self.coordinator.retry_activity(key))
        self.wait_for_terminals(2)
        self.assertEqual(self.terminals()[1].state, ExecutionState.COMPLETED)
        self.assertEqual(
            self.fixture.state.operations.manual_operation(operation)["status"], "completed"
        )
        self.assertEqual(len(list((self.fixture.obstruction / "archive").glob("*.eml"))), 2)
