"""A manual selection is the only retry boundary for its source scans and mail."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mailarchive.application.errors import WorkspaceError
from mailarchive.application.execution import ExecutionCoordinator
from mailarchive.application.source_port import RemoteMessage
from mailarchive.domain.configuration import Account, Mailbox, Rule, RuleTarget, Settings
from tests.test_restart_core import PagedRangeSource, Registry, raw_mail
from tests.workspace_fixture import WorkspaceStore, make_service


class ManualRetryBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mailbox = Mailbox("owner@example.org", ["INBOX"])
        self.account = Account(
            "Owner", "imap.example.org", self.mailbox.address, mailboxes=[self.mailbox]
        )
        self.obstruction = self.root / "offline"
        self.obstruction.write_text("offline")
        self.rule = Rule("Archive", targets=[RuleTarget(str(self.obstruction / "archive"))])
        self.settings = Settings(accounts=[self.account], rules=[self.rule])
        received = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.source = PagedRangeSource(
            {
                remote_id: RemoteMessage(remote_id, raw_mail(), received, "imap_internaldate")
                for remote_id in ("1", "2")
            }
        )
        self.state = WorkspaceStore(self.root / "profile" / "workspace.sqlite3")
        self.service = make_service(self.state, Registry(self.source))
        self.coordinator = ExecutionCoordinator(
            self.service, lambda: self.settings, self.state.operations
        )

    def _first_attempt(self) -> str:
        operation_id = self.service.prepare_range_operation(
            self.settings, {self.mailbox.id}, rule_id=self.rule.id
        )
        self.service.run_range_operation(operation_id)
        return operation_id

    def test_waiting_retry_resumes_failed_scan_and_offline_output(self) -> None:
        operation_id = self._first_attempt()
        operation = self.state.operations.manual_operation(operation_id)
        self.assertEqual(operation["status"], "waiting")
        self.assertEqual(self.source.enumerated, ["1"])
        self.assertEqual(
            self.state.operations.manual_operation_sources(operation_id)[0]["status"],
            "failed",
        )

        self.obstruction.unlink()
        self.assertTrue(self.coordinator.retry_activity("operation:" + operation_id))
        self.assertEqual(list(self.coordinator._manual), [operation_id])
        self.coordinator._manual.clear()
        self.service.run_range_operation(operation_id)

        self.assertEqual(self.source.enumerated, ["1", "2"])
        self.assertEqual(
            self.state.operations.manual_operation(operation_id)["status"], "completed"
        )
        self.assertEqual(len(list((self.obstruction / "archive").glob("*.eml"))), 2)

    def test_manual_mail_cannot_retry_outside_interrupted_parent(self) -> None:
        operation_id = self._first_attempt()
        plan_id = self.state.operations.manual_open_plans(operation_id)[0]["id"]
        with self.state.connection() as db, db:
            db.execute(
                "UPDATE manual_operation SET status='interrupted' WHERE id=?", (operation_id,)
            )
        self.obstruction.unlink()

        self.assertFalse(self.coordinator.retry_activity("mail:" + plan_id))
        with self.assertRaisesRegex(ValueError, "past-mail operation"):
            self.service.retry_activity("mail:" + plan_id)
        self.assertEqual(
            self.state.operations.manual_operation(operation_id)["status"], "interrupted"
        )
        self.assertEqual(list(self.root.glob("offline/archive/*.eml")), [])

    def test_direct_resume_cannot_publish_recovered_manual_child(self) -> None:
        operation_id = self._first_attempt()
        plan_id = self.state.operations.manual_open_plans(operation_id)[0]["id"]
        # A retry was active when the process ended. Recovery closes that attempt
        # while retaining its accepted raw mail for the parent operation's Retry.
        self.assertTrue(self.state.operations.claim_manual_operation(operation_id))
        self.state.recover()
        self.assertEqual(
            self.state.operations.manual_operation(operation_id)["status"], "interrupted"
        )
        self.obstruction.unlink()

        with self.assertRaisesRegex(ValueError, "past-mail operation"):
            self.service.resume_plan(plan_id)
        self.assertEqual(self.service.resume_open(), (0, 0))
        self.assertEqual(list((self.obstruction / "archive").glob("*.eml")), [])
        self.assertEqual(
            self.state.operations.manual_operation(operation_id)["status"], "interrupted"
        )

        self.service.run_range_operation(operation_id)
        self.assertEqual(
            self.state.operations.manual_operation(operation_id)["status"], "completed"
        )
        self.assertEqual(len(list((self.obstruction / "archive").glob("*.eml"))), 2)
        with self.state.connection() as db:
            statuses = [
                row[0]
                for row in db.execute(
                    "SELECT status FROM manual_operation_attempt WHERE operation_id=? "
                    "ORDER BY number",
                    (operation_id,),
                )
            ]
        self.assertEqual(statuses, ["failed", "interrupted", "completed"])

    def test_stopped_manual_plan_stays_paused_after_direct_resume_attempt(self) -> None:
        operation_id = self._first_attempt()
        plan_id = self.state.operations.manual_open_plans(operation_id)[0]["id"]
        self.state.operations.request_stop_manual_operation(operation_id)
        self.state.operations.finalize_stop_manual_operation(operation_id)
        self.assertEqual(self.state.delivery.work_plans()[0]["status"], "paused")

        with self.assertRaisesRegex(ValueError, "past-mail operation"):
            self.service.resume_plan(plan_id)
        with self.assertRaisesRegex(WorkspaceError, "past-mail operation"):
            self.state.delivery.resume_plan(plan_id)

        self.assertEqual(self.state.operations.manual_operation(operation_id)["status"], "stopped")
        self.assertEqual(self.state.delivery.work_plans()[0]["status"], "paused")
        self.assertEqual(list((self.obstruction / "archive").glob("*.eml")), [])

    def test_manual_child_pause_and_abort_leave_parent_retry_intact(self) -> None:
        operation_id = self._first_attempt()
        plan = self.state.operations.manual_open_plans(operation_id)[0]
        raw_path = Path(plan["raw_path"])

        for command in (self.service.pause_plan, self.service.abort_plan):
            with self.assertRaisesRegex(ValueError, "past-mail operation"):
                command(plan["id"])
        for command in (self.state.delivery.pause_plan, self.state.delivery.abort_plan):
            with self.assertRaisesRegex(WorkspaceError, "past-mail operation"):
                command(plan["id"])

        self.assertEqual(self.state.operations.manual_operation(operation_id)["status"], "waiting")
        self.assertEqual(self.state.delivery.work_plans()[0]["status"], "open")
        self.assertTrue(raw_path.exists())
        self.obstruction.unlink()
        self.service.run_range_operation(operation_id)
        self.assertEqual(
            self.state.operations.manual_operation(operation_id)["status"], "completed"
        )
        self.assertEqual(len(list((self.obstruction / "archive").glob("*.eml"))), 2)


if __name__ == "__main__":
    unittest.main()
