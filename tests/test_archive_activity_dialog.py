from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from mailarchive.application.activity import (
    ActivityDetail,
    ActivityItem,
    ActivityPage,
    MailResult,
    OperationAttempt,
    OutputAttempt,
    OutputResult,
    SourceResult,
)
from mailarchive.presentation.archive_activity_dialog import ArchiveActivityDialog, _status_label
from tests.tk_test_case import TkTestCase


class ActivityStatusTextTests(unittest.TestCase):
    def test_internal_states_have_clear_display_labels(self) -> None:
        self.assertEqual(_status_label("waiting"), "Waiting to retry")
        self.assertEqual(_status_label("working"), "Processing")
        self.assertEqual(_status_label("done"), "Completed")


class ArchiveActivityDialogTests(TkTestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output_path = Path(self.temporary.name) / "saved.eml"
        self.output_path.write_bytes(b"From: sender@example.com\n\nMail")
        self.operation = ActivityItem(
            key="operation:one",
            kind="operation",
            status="running",
            occurred_at="2026-10-01T10:00:00Z",
            finished_at=None,
            source_id=None,
            address=None,
            subject=None,
            rule_name="Invoices",
            summary="Apply Invoices to past mail",
            can_stop=True,
            mail_count=1,
            completed_outputs=1,
        )
        self.history = ActivityItem(
            key="mail:one",
            kind="mail",
            status="complete",
            occurred_at="2026-09-30T10:00:00Z",
            finished_at="2026-09-30T10:01:00Z",
            source_id="source",
            address="owner@example.com",
            subject="Invoice",
            rule_name="Invoices",
            summary="Invoice",
            completed_outputs=1,
        )
        self.output = OutputResult(
            output_id=1,
            status="done",
            final_path=str(self.output_path),
            requested_path=str(self.output_path),
            error=None,
            completed_at="2026-10-01T10:01:00Z",
            attempts=(),
        )
        mail = MailResult(
            key="mail:one",
            source_id="source",
            address="owner@example.com",
            subject="Invoice",
            status="complete",
            received_at=None,
            rule_name="Invoices",
            error=None,
            outputs=(self.output,),
        )
        self.application = MagicMock()
        self.application.current_jobs.return_value = (self.operation,)
        self.application.activity_page.return_value = ActivityPage((self.history,), None)
        self.application.activity_detail.side_effect = lambda key: ActivityDetail(
            self.operation if key == self.operation.key else self.history, (mail,)
        )
        self.open_output = MagicMock()
        self.dialog = ArchiveActivityDialog(self.root, self.application, self.open_output)
        self.addCleanup(self.dialog.destroy)

    def test_split_lists_show_actual_current_job_and_history(self) -> None:
        self.assertEqual(self.dialog.current_tree.get_children(), (self.operation.key,))
        self.assertEqual(self.dialog.history_tree.get_children(), (self.history.key,))
        self.assertEqual(str(self.dialog.more_button["state"]), "disabled")

    def test_stop_is_scoped_to_selected_operation(self) -> None:
        self.dialog.current_tree.selection_set(self.operation.key)
        self.dialog._select_current()
        with patch(
            "mailarchive.presentation.archive_activity_dialog.messagebox.askyesno",
            return_value=True,
        ):
            self.dialog.stop_selected()
        self.application.stop_operation.assert_called_once_with(self.operation.key)

    def test_only_selected_completed_output_can_be_opened(self) -> None:
        self.dialog.history_tree.selection_set(self.history.key)
        self.dialog._select_history()
        output_row = next(row for row in self.dialog.result_tree.get_children("mail:0"))
        self.dialog.result_tree.selection_set(output_row)
        self.dialog._select_result()
        self.dialog.open_selected()
        self.open_output.assert_called_once_with(self.output_path)

    def test_reused_receipt_is_labeled_previously_archived(self) -> None:
        reused = replace(self.output, previously_archived=True)
        item = replace(
            self.history,
            completed_outputs=0,
            previously_archived_outputs=1,
            summary="0 outputs saved, 1 previously archived, 0 failed",
        )
        mail = MailResult(
            "mail:one",
            "source",
            "owner@example.com",
            "Invoice",
            "complete",
            None,
            "Invoices",
            None,
            (reused,),
        )
        self.application.activity_page.return_value = ActivityPage((item,), None)
        self.application.activity_detail.return_value = ActivityDetail(item, (mail,))
        self.application.activity_detail.side_effect = None
        self.dialog.refresh()
        self.dialog.history_tree.selection_set(item.key)
        self.dialog._select_history()
        row = self.dialog.result_tree.get_children("mail:0")[0]
        self.assertEqual(self.dialog.result_tree.item(row, "text"), "Previously archived")
        self.assertEqual(self.dialog.result_tree.set(row, "status"), "Previously archived")
        archive_time = self.dialog.result_tree.get_children(row)[0]
        self.assertIn(reused.completed_at, self.dialog.result_tree.item(archive_time, "text"))
        self.assertIn("1 previously archived outputs", self.dialog.detail_summary.get())

    def test_history_uses_keyset_pagination(self) -> None:
        older = ActivityItem(
            key="mail:older",
            kind="mail",
            status="failed",
            occurred_at="2026-09-29T10:00:00Z",
            finished_at=None,
            source_id="source",
            address="owner@example.com",
            subject="Older",
            rule_name="Invoices",
            summary="Older failed mail",
            can_retry=True,
        )
        self.application.activity_page.side_effect = [
            ActivityPage((self.history,), (self.history.occurred_at, self.history.key)),
            ActivityPage((older,), None),
        ]
        self.dialog.refresh()
        self.dialog.load_more()
        self.assertEqual(self.dialog.history_tree.get_children(), (self.history.key, older.key))
        self.application.activity_page.assert_called_with(
            before=(self.history.occurred_at, self.history.key), limit=100
        )

    def test_operation_failure_source_and_retry_attempts_are_visible(self) -> None:
        source = SourceResult("source", "owner@example.com", "failed", "Access denied")
        attempt = OperationAttempt(
            1,
            "2026-10-02T09:00:00Z",
            "2026-10-02T09:01:00Z",
            "failed",
            "Mailbox scan failed",
            (source,),
        )
        output_attempt = OutputAttempt(
            1,
            "error",
            "",
            "2026-10-02T09:02:00Z",
            "2026-10-02T09:02:01Z",
            "Share unavailable",
        )
        output = replace(self.output, attempts=(output_attempt,))
        mail = MailResult(
            "mail:one",
            "source",
            "owner@example.com",
            "Invoice",
            "failed",
            None,
            "Invoices",
            "Share unavailable",
            (output,),
        )
        self.application.activity_detail.side_effect = lambda key: ActivityDetail(
            self.operation, (mail,), "Mailbox scan failed", (source,), (attempt,)
        )
        self.dialog.current_tree.selection_set(self.operation.key)
        self.dialog._select_current()
        self.assertIn("Mailbox scan failed", self.dialog.detail_summary.get())
        source_group = next(
            row
            for row in self.dialog.result_tree.get_children()
            if self.dialog.result_tree.item(row, "text") == "Selected mailboxes"
        )
        self.assertIn("source:0", self.dialog.result_tree.get_children(source_group))
        self.assertEqual(self.dialog.result_tree.set("source:0", "error"), "Access denied")
        self.assertEqual(self.dialog.result_tree.set("attempt:0", "error"), "Mailbox scan failed")
        self.assertEqual(
            self.dialog.result_tree.set("output:0:0:attempt:0", "error"),
            "Share unavailable",
        )


if __name__ == "__main__":
    unittest.main()
