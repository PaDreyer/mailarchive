"""The application reports actual remote and retained-local outcomes to its observers."""

import tempfile
import threading
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.cancellation import ProcessingStopped
from mailarchive.application.errors import RunNotActiveError
from mailarchive.application.events import ExecutionState
from mailarchive.application.intake_limits import MAX_MESSAGE_BYTES
from mailarchive.application.source_port import MailboxError, RemoteMessage
from mailarchive.bootstrap import create_application
from mailarchive.domain.configuration import Account, Mailbox, Rule, RuleTarget, Settings
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.profile_location import ConfigStore
from tests.concurrency import THREAD_TIMEOUT
from tests.test_restart_core import FakeSource, Registry, raw_mail


class ExecutionOutcomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        mailbox = Mailbox("fake@example.org", ["INBOX"], archive_existing_messages=True)
        self.account = Account("Fake", "imap.example.org", mailbox.address, mailboxes=[mailbox])
        self.rule = Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])
        self.source = FakeSource(
            {
                "1": RemoteMessage(
                    "1", raw_mail(), datetime(2026, 1, 1, tzinfo=timezone.utc), "imap_internaldate"
                )
            }
        )
        store = ConfigStore(self.root / "profile")
        store.save(
            Settings(
                accounts=[self.account],
                rules=[self.rule],
                automatic_monitoring_paused=True,
                start_at_login=False,
            )
        )
        with (
            patch(
                "mailarchive.bootstrap.MessageSourceRegistry", return_value=Registry(self.source)
            ),
            patch("mailarchive.bootstrap.set_start_at_login"),
        ):
            self.app = create_application(store, MemoryCredentialStore())
            self.app.start()
        self.addCleanup(self.app.close)
        self.progress = []
        self.events = []
        self.condition = threading.Condition()
        self.app.set_observers(self.events.append, self.observe)

    def observe(self, progress):
        with self.condition:
            self.progress.append(progress)
            self.condition.notify_all()

    def terminal(self, start, origin):
        with self.condition:

            def find():
                return next(
                    (
                        item
                        for item in self.progress[start:]
                        if item.origin == origin and not item.active
                    ),
                    None,
                )

            self.assertTrue(
                self.condition.wait_for(find, timeout=THREAD_TIMEOUT),
                "No final execution outcome arrived",
            )
            return find()

    def check(self):
        start = len(self.progress)
        self.assertIsInstance(self.app.check_now(), str)
        return self.terminal(start, "check")

    def retry(self, key, origin="retry"):
        start = len(self.progress)
        self.app.retry_activity(key)
        return self.terminal(start, origin)

    def block_output(self):
        obstruction = self.root / "offline"
        obstruction.write_text("not a directory")
        self.rule.targets = [RuleTarget(str(obstruction / "saved"))]
        self.app.save_rules([self.rule])
        return obstruction

    def resume_disabled_local_work(self):
        account = self.app.settings.accounts[0]
        account.enabled = False
        self.app.save_account(AccountSubmission(account, {}, False), replacing_id=account.id)
        self.source.messages.clear()
        service = self.app._context.execution.service
        with service.delivery.connection() as db, db:
            db.execute("UPDATE output SET retry_after='2000-01-01T00:00:00+00:00'")
        start = len(self.progress)
        self.app.set_automatic_monitoring_paused(False)
        return self.terminal(start, "automatic")

    def test_oversized_message_reports_failed_check_with_durable_rejection(self):
        self.source.messages["1"].raw_size = MAX_MESSAGE_BYTES + 1
        outcome = self.check()
        self.assertEqual(
            (outcome.state, outcome.message), (ExecutionState.FAILED, "Mail check failed.")
        )
        item = self.app.activity_page().items[0]
        self.assertEqual(self.app.activity_detail(item.key).mail[0].status, "rejected")
        self.assertEqual(self.app.current_jobs(), ())

    def test_download_error_reports_failed_check_and_retains_retryable_work(self):
        self.source.messages["1"].error = MailboxError("temporary provider failure")
        outcome = self.check()
        self.assertEqual(outcome.state, ExecutionState.FAILED)
        item = self.app.current_jobs()[0]
        self.assertIn("temporary provider failure", self.app.activity_detail(item.key).error)

    def test_partial_publication_fails_check_but_keeps_healthy_output(self):
        self.block_output()
        self.rule.targets.append(RuleTarget(str(self.root / "healthy")))
        self.app.save_rules([self.rule])
        self.assertEqual(self.check().state, ExecutionState.FAILED)
        detail = self.app.activity_detail(self.app.current_jobs()[0].key)
        self.assertEqual([output.status for output in detail.mail[0].outputs], ["error", "done"])
        self.assertEqual(len(list((self.root / "healthy").glob("*.eml"))), 1)

    def test_failed_output_retry_remains_failed_then_repair_reports_success(self):
        obstruction = self.block_output()
        self.assertEqual(self.check().state, ExecutionState.FAILED)
        key = self.app.current_jobs()[0].key
        self.assertEqual(self.retry(key).state, ExecutionState.FAILED)
        attempts = self.app.activity_detail(key).mail[0].outputs[0].attempts
        self.assertEqual([attempt.status for attempt in attempts], ["error", "error"])
        fetch_count = self.source.fetch_count
        obstruction.unlink()
        self.assertEqual(self.retry(key).state, ExecutionState.COMPLETED)
        self.assertEqual(self.source.fetch_count, fetch_count)
        self.assertEqual(len(list((obstruction / "saved").glob("*.eml"))), 1)

    def pending_with_healthy_output(self, *, past_mail=False):
        obstruction = self.block_output()
        self.rule.targets.append(RuleTarget(str(self.root / "healthy")))
        self.app.save_rules([self.rule])
        if past_mail:
            start = len(self.progress)
            operation = self.app.apply_rule_to_past_mail(self.rule.id, None, None, "UTC")
            outcome = self.terminal(start, "operation")
            key, origin = "operation:" + operation, "operation"
        else:
            outcome = self.check()
            key, origin = self.app.current_jobs()[0].key, "retry"
        self.assertEqual(outcome.state, ExecutionState.FAILED)
        return obstruction, key, origin

    def assert_preparation_failure_recovers(self, failure, *, past_mail=False):
        obstruction, key, origin = self.pending_with_healthy_output(past_mail=past_mail)
        service = self.app._context.execution.service
        plan = dict(service.delivery.open_plans()[0])
        raw_path = Path(plan["raw_path"])
        original = raw_path.read_bytes()
        before = self.app.activity_detail(key).mail[0]
        outputs_before = tuple(output.output_id for output in before.outputs)
        healthy = before.outputs[1]
        with service.delivery.connection() as db:
            receipts_before = [tuple(row) for row in db.execute("SELECT * FROM receipt")]
            intake_attempts = db.execute("SELECT attempts FROM intake").fetchone()[0]
        fetch_count = self.source.fetch_count

        if failure == "missing":
            raw_path.unlink()
            message = "local working copy"
        elif failure == "corrupt":
            raw_path.write_bytes(b"corrupt retained MIME")
            message = "damaged"
        else:
            message = "Archive preparation temporarily unavailable"

        planning_failure = (
            patch("mailarchive.application.engine.plan_outputs", side_effect=ValueError(message))
            if failure == "planning"
            else nullcontext()
        )
        with planning_failure:
            self.assertEqual(self.retry(key, origin).state, ExecutionState.FAILED)
        detail = self.app.activity_detail(key)
        self.assertIn(message, detail.mail[0].error)
        self.assertTrue(detail.item.can_retry)
        self.assertEqual(detail.mail[0].outputs, before.outputs)
        with service.delivery.connection() as db:
            intake = db.execute("SELECT error, attempts, retry_after FROM intake").fetchone()
            self.assertIn(message, intake["error"])
            self.assertEqual(intake["attempts"], intake_attempts)
            self.assertIsNotNone(intake["retry_after"])
            self.assertEqual(
                [tuple(row) for row in db.execute("SELECT * FROM receipt")], receipts_before
            )

        raw_path.write_bytes(original)
        obstruction.unlink()
        self.assertEqual(self.retry(key, origin).state, ExecutionState.COMPLETED)
        repaired = self.app.activity_detail(key)
        self.assertIsNone(repaired.mail[0].error)
        self.assertEqual(
            tuple(output.output_id for output in repaired.mail[0].outputs), outputs_before
        )
        self.assertEqual(repaired.mail[0].outputs[1], healthy)
        self.assertEqual(self.source.fetch_count, fetch_count)
        self.assertEqual(self.app.current_jobs(), ())
        self.assertEqual(len(list((self.root / "healthy").glob("*.eml"))), 1)
        self.assertEqual(len(list((obstruction / "saved").glob("*.eml"))), 1)
        with service.delivery.connection() as db:
            saved = db.execute("SELECT raw_path FROM plan WHERE id=?", (plan["id"],)).fetchone()
            self.assertEqual(saved["raw_path"], plan["raw_path"])
            intake = db.execute("SELECT error, attempts, retry_after FROM intake").fetchone()
            self.assertEqual(tuple(intake), (None, intake_attempts, None))

    def test_selected_mail_retry_records_missing_raw_copy_then_clears_error_after_repair(self):
        self.assert_preparation_failure_recovers("missing")

    def test_selected_mail_retry_records_corrupt_raw_copy_then_clears_error_after_repair(self):
        self.assert_preparation_failure_recovers("corrupt")

    def test_selected_mail_retry_records_output_preparation_failure_then_recovers(self):
        self.assert_preparation_failure_recovers("planning")

    def test_past_mail_retry_records_missing_raw_copy_without_repeating_successful_outputs(self):
        self.assert_preparation_failure_recovers("missing", past_mail=True)

    def test_background_resume_records_raw_failure_then_clears_it_after_repair(self):
        obstruction, key, _origin = self.pending_with_healthy_output()
        service = self.app._context.execution.service
        plan = service.delivery.open_plans()[0]
        raw_path = Path(plan["raw_path"])
        original = raw_path.read_bytes()
        before = self.app.activity_detail(key).mail[0].outputs
        raw_path.unlink()
        fetch_count = self.source.fetch_count
        self.assertEqual(self.resume_disabled_local_work().state, ExecutionState.FAILED)
        self.assertIn("local working copy", self.app.activity_detail(key).mail[0].error)
        self.assertEqual(self.app.activity_detail(key).mail[0].outputs, before)

        self.app.set_automatic_monitoring_paused(True)
        raw_path.write_bytes(original)
        obstruction.unlink()
        with service.delivery.connection() as db, db:
            db.execute("UPDATE intake SET retry_after='2000-01-01T00:00:00+00:00'")
        start = len(self.progress)
        self.app.set_automatic_monitoring_paused(False)
        self.assertEqual(self.terminal(start, "automatic").state, ExecutionState.COMPLETED)
        repaired = self.app.activity_detail(key).mail[0]
        self.assertIsNone(repaired.error)
        self.assertEqual(repaired.outputs[1], before[1])
        self.assertEqual(self.source.fetch_count, fetch_count)
        with service.delivery.connection() as db:
            self.assertIsNone(db.execute("SELECT error FROM intake").fetchone()[0])

    def test_waiting_operation_output_retry_records_raw_failure_and_recovers_locally(self):
        obstruction, key, _origin = self.pending_with_healthy_output(past_mail=True)
        service = self.app._context.execution.service
        plan = service.delivery.open_plans()[0]
        raw_path = Path(plan["raw_path"])
        original = raw_path.read_bytes()
        raw_path.write_bytes(b"damaged retained MIME")
        operation_id = key.removeprefix("operation:")
        self.assertEqual(service.retry_waiting_operation_outputs(operation_id), (0, 1))
        self.assertIn("damaged", self.app.activity_detail(key).mail[0].error)
        fetch_count = self.source.fetch_count
        raw_path.write_bytes(original)
        obstruction.unlink()
        self.assertEqual(service.retry_waiting_operation_outputs(operation_id), (1, 0))
        self.assertIsNone(self.app.activity_detail(key).mail[0].error)
        self.assertEqual(self.app.activity_detail(key).item.status, "completed")
        self.assertEqual(self.source.fetch_count, fetch_count)

    def test_paused_plan_resume_records_raw_failure_and_recovers_locally(self):
        obstruction, key, _origin = self.pending_with_healthy_output()
        service = self.app._context.execution.service
        plan = service.delivery.open_plans()[0]
        raw_path = Path(plan["raw_path"])
        original = raw_path.read_bytes()
        service.pause_plan(plan["id"])
        raw_path.unlink()
        self.assertEqual(service.resume_plan(plan["id"]), (0, 1))
        self.assertIn("local working copy", self.app.activity_detail(key).error)
        fetch_count = self.source.fetch_count
        raw_path.write_bytes(original)
        obstruction.unlink()
        self.assertEqual(service.resume_plan(plan["id"]), (1, 0))
        self.assertIsNone(self.app.activity_detail(key).error)
        self.assertEqual(self.source.fetch_count, fetch_count)

    def test_stopped_past_mail_retry_does_not_record_a_concurrent_preparation_error(self):
        _obstruction, key, origin = self.pending_with_healthy_output(past_mail=True)
        service = self.app._context.execution.service

        def stop_during_read(*_args, **_kwargs):
            self.app.stop_operation(key)
            raise RuntimeError("The retained directory went offline during stop")

        with patch.object(service.engine.spool, "read", side_effect=stop_during_read):
            self.assertEqual(self.retry(key, origin).state, ExecutionState.STOPPED)
        detail = self.app.activity_detail(key)
        self.assertEqual(detail.item.status, "stopped")
        self.assertIsNone(detail.mail[0].error)

    def test_stopped_check_resume_does_not_record_a_concurrent_preparation_error(self):
        _obstruction, key, _origin = self.pending_with_healthy_output()
        service = self.app._context.execution.service
        before = self.app.activity_detail(key).mail[0]

        def stop_during_read(*_args, **_kwargs):
            request = self.app._context.execution._check
            self.assertIsNotNone(request)
            self.assertTrue(self.app.stop_check(request.id))
            raise RuntimeError("The retained directory went offline during stop")

        with patch.object(service.engine.spool, "read", side_effect=stop_during_read):
            self.assertEqual(self.check().state, ExecutionState.STOPPED)
        after = self.app.activity_detail(key).mail[0]
        self.assertEqual(after, before)
        with service.delivery.connection() as db:
            self.assertIsNone(db.execute("SELECT error FROM intake").fetchone()[0])

    def test_shutdown_during_local_resume_does_not_record_a_concurrent_preparation_error(self):
        _obstruction, key, _origin = self.pending_with_healthy_output()
        service = self.app._context.execution.service
        before = self.app.activity_detail(key).mail[0]

        def shutdown_during_read(*_args, **_kwargs):
            service.request_shutdown()
            raise RuntimeError("The retained directory went offline during shutdown")

        with patch.object(service.engine.spool, "read", side_effect=shutdown_during_read):
            with self.assertRaises(ProcessingStopped):
                service.resume_open()
        self.assertEqual(self.app.activity_detail(key).mail[0], before)

    def test_run_not_active_during_selected_retry_is_not_persisted_as_a_local_failure(self):
        _obstruction, key, origin = self.pending_with_healthy_output()
        service = self.app._context.execution.service
        with (
            patch.object(service.engine, "execute", side_effect=RunNotActiveError("Run stopped")),
            self.assertLogs("mailarchive.application.execution", level="ERROR"),
        ):
            self.assertEqual(self.retry(key, origin).state, ExecutionState.FAILED)
        self.assertIsNone(self.app.activity_detail(key).mail[0].error)

    def test_local_resume_failure_is_reported_with_no_eligible_remote_account(self):
        self.block_output()
        self.assertEqual(self.check().state, ExecutionState.FAILED)
        fetch_count = self.source.fetch_count
        outcome = self.resume_disabled_local_work()
        self.assertEqual(
            (outcome.state, outcome.message), (ExecutionState.FAILED, "Mail check failed.")
        )
        self.assertEqual(self.source.fetch_count, fetch_count)
        self.assertEqual(
            self.app.activity_detail(self.app.current_jobs()[0].key).mail[0].outputs[0].status,
            "error",
        )

    def test_past_mail_output_failures_remain_failed_through_retries_then_repair(self):
        obstruction = self.block_output()
        start = len(self.progress)
        operation = self.app.apply_rule_to_past_mail(self.rule.id, None, None, "UTC")
        self.assertEqual(self.terminal(start, "operation").state, ExecutionState.FAILED)
        key = "operation:" + operation
        self.assertEqual(self.app.activity_detail(key).item.status, "waiting")
        start = len(self.progress)
        self.app.retry_activity(key)
        self.assertEqual(self.terminal(start, "operation").state, ExecutionState.FAILED)
        self.assertEqual(self.app.activity_detail(key).item.status, "waiting")
        fetch_count = self.source.fetch_count
        obstruction.unlink()
        start = len(self.progress)
        self.app.retry_activity(key)
        self.assertEqual(self.terminal(start, "operation").state, ExecutionState.COMPLETED)
        self.assertEqual(self.app.activity_detail(key).item.status, "completed")
        self.assertEqual(self.source.fetch_count, fetch_count)

    def test_local_resume_success_is_reported_with_no_eligible_remote_account(self):
        obstruction = self.block_output()
        self.assertEqual(self.check().state, ExecutionState.FAILED)
        obstruction.unlink()
        fetch_count = self.source.fetch_count
        self.assertEqual(self.resume_disabled_local_work().state, ExecutionState.COMPLETED)
        self.assertEqual(self.source.fetch_count, fetch_count)
        self.assertEqual(self.app.current_jobs(), ())
        self.assertEqual(len(list((obstruction / "saved").glob("*.eml"))), 1)


if __name__ == "__main__":
    unittest.main()
