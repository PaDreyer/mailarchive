"""Typed activity views show outcomes and append-only publication attempts."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mailarchive.application.activity import ActivityQueries
from mailarchive.application.source_port import RemoteMessage
from mailarchive.domain.configuration import Account, Mailbox, Rule, RuleTarget, Settings
from mailarchive.infrastructure.activity_repository import SqliteActivityRepository
from tests.test_restart_core import FakeSource, Registry, raw_mail
from tests.workspace_fixture import WorkspaceStore, make_service


class ActivityQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.mailbox = Mailbox("owner@example.org", ["INBOX"])
        self.account = Account(
            "Owner", "imap.example.org", self.mailbox.address, mailboxes=[self.mailbox]
        )
        self.rule = Rule("Archive", targets=[RuleTarget(str(root / "archive"))])
        self.settings = Settings(accounts=[self.account], rules=[self.rule])
        self.source = FakeSource(
            {
                "1": RemoteMessage(
                    "1", raw_mail(), datetime(2026, 1, 1, tzinfo=timezone.utc), "imap_internaldate"
                )
            }
        )
        self.state = WorkspaceStore(root / "workspace.sqlite3")
        self.service = make_service(self.state, Registry(self.source))
        self.queries = ActivityQueries(SqliteActivityRepository(self.state.connection))

    def test_manual_operation_is_one_history_row_with_nested_output(self) -> None:
        self.service.run_range(self.settings, {self.mailbox.id}, rule_id=self.rule.id)

        self.assertEqual(self.queries.current(), ())
        page = self.queries.history(limit=10)
        self.assertEqual(len(page.items), 1)
        operation = page.items[0]
        self.assertEqual(operation.kind, "operation")
        self.assertEqual(operation.status, "completed")
        self.assertEqual(operation.completed_outputs, 1)
        detail = self.queries.detail(operation.key)
        self.assertEqual(len(detail.mail), 1)
        self.assertEqual(len(detail.mail[0].outputs), 1)
        output = detail.mail[0].outputs[0]
        self.assertTrue(output.can_open)
        self.assertEqual(output.status, "done")
        self.assertEqual(
            [(attempt.number, attempt.status) for attempt in output.attempts], [(1, "done")]
        )

    def test_empty_automatic_check_updates_health_without_history(self) -> None:
        self.service.run_once(self.settings)

        self.assertEqual(self.queries.current(), ())
        self.assertEqual(self.queries.history().items, ())
        scope = self.state.scope(self.mailbox.id, "INBOX")
        self.assertIsNotNone(scope["last_checked_at"])
        self.assertIsNone(scope["last_error"])

    def test_repeat_past_mail_reports_receipt_reuse_separately(self) -> None:
        self.service.run_range(self.settings, {self.mailbox.id}, rule_id=self.rule.id)
        self.service.run_range(self.settings, {self.mailbox.id}, rule_id=self.rule.id)

        repeated = self.queries.history().items[0]
        self.assertEqual(repeated.completed_outputs, 0)
        self.assertEqual(repeated.previously_archived_outputs, 1)
        self.assertIn("previously archived", repeated.summary)
        output = self.queries.detail(repeated.key).mail[0].outputs[0]
        self.assertTrue(output.previously_archived)
        self.assertEqual(output.attempts, ())

    def test_operation_mail_count_includes_nonaccepted_intake(self) -> None:
        operation_id = self.service.prepare_range_operation(
            self.settings, {self.mailbox.id}, rule_id=self.rule.id
        )
        self.assertTrue(self.state.claim_manual_operation(operation_id))
        operation = self.state.manual_operation(operation_id)
        run_id = self.state.start_run(
            self.mailbox.id,
            "manual",
            {"folders": ["INBOX"], "start_utc": None, "end_utc": None},
            self.settings,
            operation["config_revision"],
            operation_id,
        )
        intake_id = self.state.reserve(
            self.mailbox.id,
            "INBOX\0broken",
            run_id,
            automatic=False,
            scope_key="INBOX",
            remote_id="broken",
        )
        self.state.mark_intake_error(intake_id, "Download failed")

        item = next(item for item in self.queries.current() if item.key.endswith(operation_id))
        self.assertEqual(item.mail_count, 1)
        detail = self.queries.detail(item.key)
        self.assertEqual(len(detail.mail), 1)
        self.assertEqual(detail.mail[0].error, "Download failed")

    def test_manual_child_mail_cannot_retry_outside_parent(self) -> None:
        with patch(
            "mailarchive.infrastructure.output_files._atomic_write", side_effect=OSError("offline")
        ):
            self.service.run_range(self.settings, {self.mailbox.id}, rule_id=self.rule.id)
        parent = self.queries.current()[0]
        child_key = self.queries.detail(parent.key).mail[0].key
        self.assertFalse(self.queries.detail(child_key).item.can_retry)

    def test_automatic_mail_has_one_completed_result(self) -> None:
        self.mailbox.archive_existing_messages = True
        self.service.run_once(self.settings)

        page = self.queries.history()
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].kind, "mail")
        detail = self.queries.detail(page.items[0].key)
        self.assertEqual(detail.mail[0].status, "complete")
        self.assertEqual(len(detail.mail[0].outputs[0].attempts), 1)


if __name__ == "__main__":
    unittest.main()
